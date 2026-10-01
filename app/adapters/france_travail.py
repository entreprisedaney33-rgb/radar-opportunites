"""Adaptateur vers l'API « Offres d'emploi v2 » de France Travail (V2.3, RADAR-V2.md).

Sources de ce code (lues le 2026-10-01) :
- documentation officielle de la ressource de recherche (Stoplight, publique) : `GET /v2/offres/search` sur
  `https://api.francetravail.io/partenaire/offresdemploi` ; `codeNAF` (format 99.99X, jusqu'à 200 valeurs),
  `minCreationDate` / `maxCreationDate` (`yyyy-MM-dd'T'HH:mm:ss'Z'`), `range=p-d` (150 offres au plus par
  page), `sort=1` (date de création décroissante) ; réponses 200 (tout), 206 (partiel, en-tête
  `Content-Range: offres p-d/t`), 204 (aucun résultat), 400, 500 ;
- jeton : OAuth2 `client_credentials` sur `https://entreprise.francetravail.fr/connexion/oauth2/access_token?realm=%2Fpartenaire`
  (adresse et scope relevés dans la documentation publique d'un tiers et dans l'usage connu de l'API ; la page
  officielle « Générer un access token » n'a pas pu être lue -- son contenu se charge en JavaScript). Le TEST DE
  FUMÉE RÉEL (jeton + une recherche) reste donc la seule vérification définitive : voir `tests_payants/fumee_offres.py`.

CE QU'IL FAUT SAVOIR :
- L'API ne sert que les offres ACTIVES : une offre pourvue ou retirée disparaît. « 90 jours d'historique » veut dire
  « offres encore en ligne créées depuis 90 jours », pas « toutes les offres publiées depuis 90 jours ». Pour avoir
  12 mois de recul il faut accumuler jour après jour à partir d'aujourd'hui.
- Plafond de résultats par requête : la documentation officielle annonce un premier index <= 3000 et un dernier
  <= 3149 ; un article tiers donne 1000 et 1149. Par prudence le code vise le plus petit plafond (1 150 résultats)
  et découpe la fenêtre de dates en deux tant qu'une requête annonce davantage. Aucune offre n'est perdue
  silencieusement : une fenêtre d'une heure encore trop pleine est marquée « tronquée ».
- Constats du test de fumée réel (2026-10-01) : le jeton marche avec l'adresse et le scope ci-dessus ; le filtre
  `codeNAF` est EXACT pour un code valide mais IGNORÉ EN SILENCE pour un code inconnu (99.99Z a renvoyé 29 353 offres) :
  `collecter_code` vérifie donc que chaque offre porte bien le code demandé et lève une erreur sinon ; l'index 1150-1299
  est accepté (le plafond « 1149 » de l'article tiers est faux) ; le plafond annoncé de 3149 n'a pas été testé jusqu'au bout.
- Débit : 10 appels par seconde annoncés ; on reste à 4 (espacement par hôte de `app.adapters.http`).
- Conditions d'utilisation : le contenu des offres ne doit pas être republié tel quel ; le radar ne s'en sert que pour
  des comptes et des agrégats (point à relire avant tout affichage d'une offre dans Jarvis, V2.7).
- Les identifiants ne viennent QUE de l'environnement (`RADAR_FT_CLIENT_ID`, `RADAR_FT_CLIENT_SECRET`) ou du fichier
  local hors dépôt `~/.config/radar-opportunites/env`. Ils ne sont jamais journalisés ni mis dans une erreur.
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from sqlalchemy.engine import Engine

from app.adapters import http

logger = logging.getLogger(__name__)

HOTE_API = "api.francetravail.io"
HOTE_JETON = "entreprise.francetravail.fr"
URL_RECHERCHE = f"https://{HOTE_API}/partenaire/offresdemploi/v2/offres/search"
URL_JETON = f"https://{HOTE_JETON}/connexion/oauth2/access_token?realm=%2Fpartenaire"
SCOPE_DEFAUT = "api_offresdemploiv2 o2dsoffre"
VARIABLE_CLIENT_ID = "RADAR_FT_CLIENT_ID"
VARIABLE_CLIENT_SECRET = "RADAR_FT_CLIENT_SECRET"
VARIABLE_SCOPE = "RADAR_FT_SCOPE"
FICHIER_ENV = Path.home() / ".config" / "radar-opportunites" / "env"
CONTEXTE_RECHERCHE = "france_travail:offres"
CONTEXTE_JETON = "france_travail:jeton"
PAR_PAGE = 150
PLAFOND_RESULTATS = 1150  # voir la docstring de module : le plus petit des deux plafonds annoncés
FENETRE_MIN = timedelta(hours=1)
MARGE_RENOUVELLEMENT_JETON_S = 60
FORMAT_DATE_API = "%Y-%m-%dT%H:%M:%SZ"

# 10 appels/s annoncés : on reste à 4/s, sur les deux hôtes.
http.DELAIS_MIN_PAR_HOTE_SECONDES.setdefault(HOTE_API, 0.25)
http.DELAIS_MIN_PAR_HOTE_SECONDES.setdefault(HOTE_JETON, 0.25)


class IdentifiantsAbsents(Exception):
    """Pas d'identifiants France Travail : message clair, jamais de repli ni de valeur par défaut."""


# ------------------------------------------------------------------ identifiants -----

def _lire_fichier_env() -> dict[str, str]:
    valeurs: dict[str, str] = {}
    if not FICHIER_ENV.exists():
        return valeurs
    for ligne in FICHIER_ENV.read_text(encoding="utf-8").splitlines():
        if "=" in ligne and not ligne.lstrip().startswith("#"):
            cle, _, valeur = ligne.partition("=")
            valeurs[cle.strip()] = valeur.strip()
    return valeurs


def lire_identifiants() -> tuple[str, str]:
    """(client_id, client_secret) : environnement d'abord, fichier local hors dépôt ensuite."""
    fichier = None
    resultat = []
    for variable in (VARIABLE_CLIENT_ID, VARIABLE_CLIENT_SECRET):
        valeur = os.environ.get(variable)
        if not valeur:
            fichier = _lire_fichier_env() if fichier is None else fichier
            valeur = fichier.get(variable)
        resultat.append(valeur)
    if not all(resultat):
        raise IdentifiantsAbsents(
            f"Identifiants France Travail absents : définir {VARIABLE_CLIENT_ID} et {VARIABLE_CLIENT_SECRET} "
            f"(environnement, ou fichier {FICHIER_ENV})."
        )
    return resultat[0], resultat[1]


# ------------------------------------------------------------------ jeton OAuth -----

@dataclass
class Jeton:
    valeur: str
    expire_a: float  # time.monotonic()

    def valide(self) -> bool:
        return time.monotonic() < self.expire_a - MARGE_RENOUVELLEMENT_JETON_S

    def __repr__(self) -> str:  # jamais la valeur du jeton dans un log ou une trace
        return "Jeton(<masqué>)"


def obtenir_jeton(*, engine: Engine | None = None) -> Jeton:
    client_id, client_secret = lire_identifiants()
    reponse = http.post_formulaire_with_retry(
        URL_JETON,
        {"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret,
         "scope": os.environ.get(VARIABLE_SCOPE) or SCOPE_DEFAUT},
        engine=engine, contexte=CONTEXTE_JETON,
    )
    try:
        corps = reponse.json()
        valeur, duree = corps["access_token"], float(corps.get("expires_in", 1500))
        if not isinstance(valeur, str) or not valeur:
            raise ValueError("access_token vide")
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise http.ErreurCollecte("Réponse du service de jeton France Travail inattendue (access_token absent)") from exc
    return Jeton(valeur=valeur, expire_a=time.monotonic() + duree)


# ------------------------------------------------------------------ recherche -----

@dataclass(frozen=True)
class PageOffres:
    offres: list[dict[str, Any]]
    total: int       # nombre total annoncé par l'API pour ce filtre (Content-Range), ou len(offres) si absent
    premier: int
    dernier: int


@dataclass
class Collecte:
    """Résultat de la collecte d'un code NAF sur une plage de dates."""
    code_naf: str
    offres: list[dict[str, Any]] = field(default_factory=list)
    requetes: int = 0
    fenetres_tronquees: int = 0  # fenêtres d'une heure encore au-delà du plafond : offres manquantes possibles


_RE_CONTENT_RANGE = re.compile(r"(\d+)\s*-\s*(\d+)\s*/\s*(\d+)")


def formater_date_api(quand: datetime) -> str:
    return quand.astimezone(timezone.utc).strftime(FORMAT_DATE_API)


def construire_url(code_naf: str, debut: datetime, fin: datetime, *, premier: int = 0, dernier: int = PAR_PAGE - 1) -> str:
    if dernier - premier + 1 > PAR_PAGE or premier < 0 or dernier < premier:
        raise ValueError(f"range invalide : {premier}-{dernier} (150 offres au plus par page)")
    parametres = {
        "codeNAF": code_naf, "minCreationDate": formater_date_api(debut), "maxCreationDate": formater_date_api(fin),
        "range": f"{premier}-{dernier}", "sort": 1,
    }
    return f"{URL_RECHERCHE}?{urlencode(parametres)}"


def rechercher(
    jeton: Jeton, code_naf: str, debut: datetime, fin: datetime, *, premier: int = 0, dernier: int = PAR_PAGE - 1,
    engine: Engine | None = None,
) -> PageOffres:
    """UNE requête. 204 = aucune offre ; 200/206 = résultats, total lu dans `Content-Range`."""
    reponse = http.get_with_retry(
        construire_url(code_naf, debut, fin, premier=premier, dernier=dernier),
        headers={"Authorization": f"Bearer {jeton.valeur}", "Accept": "application/json"},
        engine=engine, contexte=CONTEXTE_RECHERCHE,
    )
    if reponse.status_code == 204:
        return PageOffres(offres=[], total=0, premier=premier, dernier=premier)
    try:
        corps = reponse.json()
    except ValueError as exc:
        raise http.ErreurCollecte("Réponse non JSON de l'API Offres d'emploi") from exc
    offres = corps.get("resultats") if isinstance(corps, dict) else None
    if not isinstance(offres, list):
        raise http.ErreurCollecte("Réponse inattendue de l'API Offres d'emploi : `resultats` absent")
    total = len(offres) + premier
    brut = (getattr(reponse, "headers", None) or {}).get("Content-Range")
    if brut:
        m = _RE_CONTENT_RANGE.search(brut)
        if m:
            total = int(m.group(3))
    return PageOffres(offres=offres, total=total, premier=premier, dernier=premier + max(len(offres) - 1, 0))


def _verifier_filtre_naf(page: PageOffres, code_naf: str) -> None:
    """L'API ignore en silence un code NAF inconnu (elle renvoie alors TOUTES les offres) : si une offre porte un
    autre code que celui demandé, on s'arrête au lieu de ranger des milliers d'offres sous un faux code."""
    for offre in page.offres:
        if isinstance(offre, dict) and offre.get("codeNAF") != code_naf:
            raise http.ErreurCollecte(
                f"Filtre codeNAF ignoré par l'API pour {code_naf} (offre d'un autre code reçue) : collecte refusée"
            )


def collecter_code(
    jeton_fournisseur, code_naf: str, debut: datetime, fin: datetime, *, plafond: int = PLAFOND_RESULTATS,
    engine: Engine | None = None, resultat: Collecte | None = None,
) -> Collecte:
    """Toutes les offres actives d'un code NAF créées entre `debut` et `fin`. `jeton_fournisseur()` renvoie un
    jeton valide (renouvelé au besoin). Si le total annoncé dépasse `plafond`, la fenêtre est coupée en deux
    (récursivement) ; en deçà d'une heure, les `plafond` premières offres sont gardées et la fenêtre marquée tronquée."""
    resultat = resultat or Collecte(code_naf=code_naf)
    premiere = rechercher(jeton_fournisseur(), code_naf, debut, fin, engine=engine)
    resultat.requetes += 1
    _verifier_filtre_naf(premiere, code_naf)
    if premiere.total == 0:
        return resultat
    if premiere.total > plafond and (fin - debut) > FENETRE_MIN:
        milieu = debut + (fin - debut) / 2
        collecter_code(jeton_fournisseur, code_naf, debut, milieu, plafond=plafond, engine=engine, resultat=resultat)
        collecter_code(jeton_fournisseur, code_naf, milieu + timedelta(seconds=1), fin, plafond=plafond, engine=engine, resultat=resultat)
        return resultat
    a_lire = min(premiere.total, plafond)
    if premiere.total > plafond:
        resultat.fenetres_tronquees += 1
        logger.warning("Fenêtre %s %s..%s tronquée : %d offres annoncées, %d lues.", code_naf, debut, fin, premiere.total, plafond)
    resultat.offres.extend(premiere.offres[:a_lire])  # jamais au-delà du plafond, même si la page est plus grande
    suivant = min(len(premiere.offres), a_lire)
    while suivant < a_lire:
        dernier = min(suivant + PAR_PAGE - 1, a_lire - 1)
        page = rechercher(jeton_fournisseur(), code_naf, debut, fin, premier=suivant, dernier=dernier, engine=engine)
        resultat.requetes += 1
        _verifier_filtre_naf(page, code_naf)
        if not page.offres:
            break  # l'API n'en renvoie plus : on s'arrête, jamais de boucle infinie
        resultat.offres.extend(page.offres)
        suivant += len(page.offres)
    return resultat


# ------------------------------------------------------------------ normalisation -----

_RE_MONTANT = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:euros?|€)", re.IGNORECASE)
_RE_MOIS = re.compile(r"sur\s+(\d+(?:[.,]\d+)?)\s+mois", re.IGNORECASE)
HEURES_PAR_AN_TEMPS_PLEIN = 1820  # 35 h x 52 semaines : approximation pour un taux horaire


def _nombre(texte: str) -> float:
    return float(texte.replace(",", "."))


def salaire_annuel_eur(libelle: str | None) -> tuple[float | None, float | None]:
    """(minimum, maximum) annuels BRUTS approximatifs d'après le libellé de salaire de France Travail
    (« Mensuel de 1923.00 Euros sur 12 mois », « Annuel de 27000 Euros à 32000 Euros », « Horaire de 12.5 Euros »).
    `(None, None)` si le libellé est absent, en cachet, ou illisible : jamais deviné. Le libellé brut reste stocké."""
    if not libelle:
        return None, None
    texte = libelle.strip()
    montants = [_nombre(m) for m in _RE_MONTANT.findall(texte)]
    if not montants:
        return None, None
    bas = texte.lower()
    mois = _RE_MOIS.search(texte)
    nb_mois = _nombre(mois.group(1)) if mois else 12.0
    if bas.startswith("mensuel"):
        facteur = nb_mois
    elif bas.startswith("annuel"):
        facteur = 1.0
    elif bas.startswith("horaire"):
        facteur = HEURES_PAR_AN_TEMPS_PLEIN
    else:
        return None, None  # cachet, autre : pas de conversion honnête
    annuels = [round(m * facteur, 2) for m in montants[:2]]
    if not all(500 <= a <= 1_000_000 for a in annuels):
        return None, None  # montant aberrant (faute de saisie) : écarté plutôt que gardé
    return min(annuels), max(annuels)


def _texte(valeur: Any) -> str | None:
    return valeur.strip() if isinstance(valeur, str) and valeur.strip() else None


def _date(valeur: Any) -> datetime | None:
    if not isinstance(valeur, str):
        return None
    try:
        return datetime.fromisoformat(valeur.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def departement_depuis_commune(commune: str | None) -> str | None:
    """Département d'après le code commune INSEE (2 chiffres, 2A/2B, 3 chiffres pour les DOM)."""
    if not commune or len(commune) < 5:
        return None
    return commune[:3] if commune.startswith("97") else commune[:2]


def normaliser_offre(brute: dict[str, Any]) -> dict[str, Any] | None:
    """Offre brute de l'API -> ligne de la table `offres_emploi`, ou None si elle est inexploitable (sans
    identifiant ou sans date de création). Le texte de l'offre n'est jamais interprété ici."""
    if not isinstance(brute, dict):
        return None
    id_offre = _texte(brute.get("id"))
    cree = _date(brute.get("dateCreation"))
    if not id_offre or cree is None:
        return None
    lieu = brute.get("lieuTravail") if isinstance(brute.get("lieuTravail"), dict) else {}
    salaire = brute.get("salaire") if isinstance(brute.get("salaire"), dict) else {}
    entreprise = brute.get("entreprise") if isinstance(brute.get("entreprise"), dict) else {}
    libelle_salaire = _texte(salaire.get("libelle"))
    bas, haut = salaire_annuel_eur(libelle_salaire)
    commune = _texte(lieu.get("commune"))

    def _flottant(v: Any) -> float | None:
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    return {
        "id_offre": id_offre, "intitule": _texte(brute.get("intitule")) or "(sans intitulé)",
        "description": _texte(brute.get("description")), "code_naf": _texte(brute.get("codeNAF")),
        "rome_code": _texte(brute.get("romeCode")), "type_contrat": _texte(brute.get("typeContrat")),
        "commune": commune, "code_postal": _texte(lieu.get("codePostal")),
        "departement": departement_depuis_commune(commune),
        "latitude": _flottant(lieu.get("latitude")), "longitude": _flottant(lieu.get("longitude")),
        "salaire_libelle": libelle_salaire, "salaire_annuel_min_eur": bas, "salaire_annuel_max_eur": haut,
        "entreprise_nom": _texte(entreprise.get("nom")), "tranche_effectif_etab": _texte(brute.get("trancheEffectifEtab")),
        "date_creation": cree, "date_actualisation": _date(brute.get("dateActualisation")),
    }
