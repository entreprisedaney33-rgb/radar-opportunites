"""Adaptateur vers l'API Recherche d'entreprises (V2.2, RADAR-V2.md).

`https://recherche-entreprises.api.gouv.fr` : gratuite, SANS CLÉ (données SIRENE). Débit maximal annoncé
par la documentation officielle : 7 requêtes par seconde et par IP, réponse 429 + en-tête `Retry-After`
au-delà. Tous les appels passent par `app.adapters.http.get_with_retry` (limiteur par hôte, backoff
sur 429, journal HTTP) : jamais d'appel direct à `requests`.

CE QUE L'API PERMET (lu dans son OpenAPI le 2026-10-01, vérifié par des requêtes réelles) -- et ce
qu'elle ne permet PAS, à ne pas oublier en lisant les chiffres :
- `total_results` compte des ENTREPRISES (unités légales), pas des établissements.
- `activite_principale` (code NAF) ne filtre que l'unité légale (son activité principale) ;
  `departement` filtre sur les établissements. Le compte renvoyé est donc : « entreprises actives dont
  l'activité principale est X et qui ont AU MOINS UN établissement dans le département D ».
- `etat_administratif=A` filtre l'unité légale. Les `matching_etablissements` d'une entreprise active
  contiennent aussi des établissements fermés et d'autres activités : on les filtre ici, par le code
  (actif, même code NAF, même département).
- `total_results` est PLAFONNÉ À 10 000 par l'API (constaté le 2026-10-01 : 105 des 168 codes du
  référentiel renvoient exactement 10000 au niveau France). Un total >= 10000 est donc une borne basse,
  jamais un compte : `PageEntreprises.plafonne` le dit, et la mesure l'enregistre (`comptage_plafonne`).
- Une page = 25 entreprises au plus (`per_page`), jusqu'à 100 établissements connexes par entreprise.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from sqlalchemy.engine import Engine

from app.adapters import http

logger = logging.getLogger(__name__)

HOTE = "recherche-entreprises.api.gouv.fr"
URL_RECHERCHE = f"https://{HOTE}/search"
CONTEXTE_JOURNAL_HTTP = "recherche_entreprises:sirene"
PAR_PAGE_MAX = 25
CONNEXES_MAX = 100
PLAFOND_ECHANTILLON_DEFAUT = 500
PLAFOND_TOTAL_API = 10_000  # `total_results` ne dépasse jamais cette valeur côté API
PAGES_MAX_DEFAUT = 40  # garde-fou : 40 pages = 1 000 entreprises lues au plus par (code, département)

# 7 requêtes/s au maximum (documentation officielle) : on reste à 4/s, avec de la marge pour d'autres usages de l'IP.
http.DELAIS_MIN_PAR_HOTE_SECONDES.setdefault(HOTE, 0.25)


@dataclass(frozen=True)
class Prospect:
    siret: str
    siren: str | None
    raison_sociale: str
    code_naf: str
    adresse: str | None
    code_postal: str | None
    code_commune: str | None
    commune: str | None
    latitude: float | None
    longitude: float | None
    tranche_effectif_salarie: str | None
    categorie_entreprise: str | None
    est_siege: bool | None


@dataclass(frozen=True)
class PageEntreprises:
    url: str
    page: int
    total_resultats: int
    total_pages: int
    entreprises: list[dict[str, Any]]

    @property
    def plafonne(self) -> bool:
        """Vrai si le total est tronqué par l'API (borne basse, pas un compte)."""
        return self.total_resultats >= PLAFOND_TOTAL_API


@dataclass
class Echantillon:
    code_naf: str
    departement: str
    total_entreprises: int
    prospects: list[Prospect] = field(default_factory=list)
    pages_lues: int = 0
    total_plafonne: bool = False  # le total est une borne basse (voir PLAFOND_TOTAL_API)
    complet: bool = False  # vrai si TOUTES les entreprises du filtre ont été lues (aucune coupure)
    url_premiere_page: str = ""


def construire_url(
    code_naf: str, departement: str | None = None, *, page: int = 1, par_page: int = PAR_PAGE_MAX,
    connexes: int = CONNEXES_MAX, minimal: bool = True,
) -> str:
    """`departement=None` : France entière. Aucun secret dans l'URL (API sans clé)."""
    if not 1 <= par_page <= PAR_PAGE_MAX:
        raise ValueError(f"par_page doit être entre 1 et {PAR_PAGE_MAX}")
    if not 1 <= connexes <= CONNEXES_MAX:
        raise ValueError(f"connexes doit être entre 1 et {CONNEXES_MAX}")
    parametres: dict[str, Any] = {
        "activite_principale": code_naf, "etat_administratif": "A", "page": page, "per_page": par_page,
        "limite_matching_etablissements": connexes,
    }
    if departement is not None:
        parametres["departement"] = departement
    if minimal:
        parametres["minimal"] = "true"
        parametres["include"] = "matching_etablissements"
    return f"{URL_RECHERCHE}?{urlencode(parametres)}"


def _entier(valeur: Any, nom: str) -> int:
    if isinstance(valeur, bool) or not isinstance(valeur, int):
        raise http.ErreurCollecte(f"Réponse inattendue de l'API Recherche d'entreprises : {nom} absent ou non entier")
    return valeur


def lire_page(
    code_naf: str, departement: str | None = None, *, page: int = 1, par_page: int = PAR_PAGE_MAX,
    connexes: int = CONNEXES_MAX, engine: Engine | None = None,
) -> PageEntreprises:
    """Une requête HTTP. Lève `ErreurCollecte` (ou `TropDeRequetes`) si l'API échoue ou répond hors format."""
    url = construire_url(code_naf, departement, page=page, par_page=par_page, connexes=connexes)
    reponse = http.get_with_retry(url, engine=engine, contexte=CONTEXTE_JOURNAL_HTTP)
    try:
        corps = reponse.json()
    except ValueError as exc:
        raise http.ErreurCollecte(f"Réponse non JSON de l'API Recherche d'entreprises : {exc}") from exc
    if not isinstance(corps, dict) or not isinstance(corps.get("results"), list):
        raise http.ErreurCollecte("Réponse inattendue de l'API Recherche d'entreprises : `results` absent")
    return PageEntreprises(
        url=url, page=page, total_resultats=_entier(corps.get("total_results"), "total_results"),
        total_pages=_entier(corps.get("total_pages"), "total_pages"), entreprises=corps["results"],
    )


def compter_entreprises_actives(code_naf: str, departement: str | None = None, *, engine: Engine | None = None) -> PageEntreprises:
    """Comptage seul : une requête, une seule entreprise en retour (le chiffre utile est `total_resultats`)."""
    return lire_page(code_naf, departement, page=1, par_page=1, connexes=1, engine=engine)


def _decimal(valeur: Any) -> float | None:
    try:
        return float(valeur)
    except (TypeError, ValueError):
        return None


def _texte(valeur: Any) -> str | None:
    return valeur.strip() if isinstance(valeur, str) and valeur.strip() else None


def extraire_prospects(page: PageEntreprises, code_naf: str, departement: str) -> list[Prospect]:
    """Établissements ACTIFS, de CE code NAF, dans CE département (code commune INSEE commençant par le code
    du département), dédoublonnés par SIRET. Une entrée sans SIRET ou sans nom est ignorée, jamais devinée."""
    vus: set[str] = set()
    prospects: list[Prospect] = []
    for entreprise in page.entreprises:
        if not isinstance(entreprise, dict):
            continue
        nom = _texte(entreprise.get("nom_complet")) or _texte(entreprise.get("nom_raison_sociale"))
        for etab in entreprise.get("matching_etablissements") or []:
            if not isinstance(etab, dict):
                continue
            siret = _texte(etab.get("siret"))
            commune = _texte(etab.get("commune"))
            if (
                not siret or not nom or siret in vus
                or etab.get("etat_administratif") != "A"
                or etab.get("activite_principale") != code_naf
                or not (commune or "").startswith(departement)
            ):
                continue
            vus.add(siret)
            tranche = _texte(etab.get("tranche_effectif_salarie"))
            prospects.append(Prospect(
                siret=siret, siren=_texte(entreprise.get("siren")), raison_sociale=nom, code_naf=code_naf,
                adresse=_texte(etab.get("adresse")), code_postal=_texte(etab.get("code_postal")),
                code_commune=commune, commune=_texte(etab.get("libelle_commune")),
                latitude=_decimal(etab.get("latitude")), longitude=_decimal(etab.get("longitude")),
                tranche_effectif_salarie=tranche,
                categorie_entreprise=_texte(entreprise.get("categorie_entreprise")),
                est_siege=etab.get("est_siege") if isinstance(etab.get("est_siege"), bool) else None,
            ))
    return prospects


def collecter_echantillon(
    code_naf: str, departement: str, *, plafond: int = PLAFOND_ECHANTILLON_DEFAUT, pages_max: int = PAGES_MAX_DEFAUT,
    engine: Engine | None = None,
) -> Echantillon:
    """Échantillon de prospection d'un (code NAF, département) : pages lues jusqu'à `plafond` établissements,
    ou fin des résultats, ou `pages_max`. `complet` n'est vrai que si toutes les entreprises ont été lues et
    que le plafond n'a pas coupé la liste."""
    echantillon = Echantillon(code_naf=code_naf, departement=departement, total_entreprises=0)
    vus: set[str] = set()
    page_courante = 1
    while True:
        page = lire_page(code_naf, departement, page=page_courante, engine=engine)
        echantillon.pages_lues += 1
        if page_courante == 1:
            echantillon.total_entreprises = page.total_resultats
            echantillon.total_plafonne = page.plafonne
            echantillon.url_premiere_page = page.url
        for prospect in extraire_prospects(page, code_naf, departement):
            if prospect.siret not in vus:
                vus.add(prospect.siret)
                echantillon.prospects.append(prospect)
        if len(echantillon.prospects) >= plafond:
            echantillon.complet = False
            del echantillon.prospects[plafond:]
            return echantillon
        if page_courante >= page.total_pages:
            echantillon.complet = True
            return echantillon
        if page_courante >= pages_max:
            logger.warning("Échantillon %s/%s coupé à %d pages (garde-fou).", code_naf, departement, pages_max)
            echantillon.complet = False
            return echantillon
        page_courante += 1


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance à vol d'oiseau (formule de haversine), en kilomètres."""
    rayon_terre = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * rayon_terre * math.asin(math.sqrt(a))
