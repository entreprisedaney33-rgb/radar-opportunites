"""Rafraîchissement des établissements (V2.2, RADAR-V2.md) : pour chaque secteur retenu de
`config/secteurs_tpe.yaml` et chaque département de la zone de `config/zone.yaml`, mesure le nombre
d'entreprises actives et collecte un échantillon de prospection (jusqu'à 500 établissements).

    python -m app.cli etablissements [--jours 30] [--max-requetes N] [--code 69.20Z] [--departement 33]

Mensuel : une paire (code NAF, département) mesurée depuis moins de `--jours` jours est sautée.
Le département « FR » (France métropolitaine) est mesuré en comptage seul, sans échantillon.
Un secteur exclu (`exclusion` du référentiel) n'est jamais collecté. Rien n'est jamais supprimé.

Disjoncteur : après `ECHECS_CONSECUTIFS_MAX` paires consécutives en échec (API en panne ou 429
persistant), la passe s'arrête et le dit dans son résumé -- jamais de mesure par repli.

Réessai lent (V2.8b) : après un tel arrêt, SIRENE est mis « en attente » pour `reessai_lent_minutes` (30 min,
`config/cycle_v2.yaml::etablissements`) : les passes de cette fenêtre ne font AUCUNE requête et le disent ; à
l'échéance, une seule paire sert de sonde (un échec relance l'attente, un succès rouvre la passe normale). État du
PROCESSUS (comme le limiteur par hôte de `app.adapters.http`) : un redémarrage du worker repart sans attente.
Constat du 2026-10-02 : SIRENE injoignable depuis Render (erreurs de connexion), 40 passes consommées pour rien.
Ce module n'est PAS branché au worker (v1 suspendue) : le branchement se décide à la mise en
production de la v2 (V2.8).
"""
from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy.engine import Engine

from app import referentiels
from app.adapters import recherche_entreprises as api
from app.adapters.http import ErreurCollecte
from app.storage import repo

logger = logging.getLogger(__name__)

DEPARTEMENT_FRANCE = "FR"
JOURS_RAFRAICHISSEMENT_DEFAUT = 30
MAX_REQUETES_DEFAUT = 2000
ECHECS_CONSECUTIFS_MAX = 3
VARIABLE_MAX_REQUETES = "RADAR_ETAB_MAX_REQUETES"
REESSAI_LENT_MINUTES_DEFAUT = 30

# V2.8b : réessai lent de SIRENE (état du processus, jamais en base). None = pas en attente.
_sirene_en_attente_jusqua: datetime | None = None
_sirene_dernier_echec: str | None = None


@dataclass
class ResumePasse:
    paires_prevues: int = 0
    paires_mesurees: int = 0
    paires_en_echec: int = 0
    requetes: int = 0
    prospects_nouveaux: int = 0
    prospects_deja_connus: int = 0
    arret: str | None = None  # None = passe terminée ; sinon motif lisible de l'arrêt anticipé
    echecs: list[str] = field(default_factory=list)


def _maintenant() -> datetime:
    return datetime.now(timezone.utc)


def reessai_lent_minutes() -> float:
    from app import config as cfg
    try:
        return float((cfg.cycle_v2().get("etablissements") or {}).get("reessai_lent_minutes", REESSAI_LENT_MINUTES_DEFAUT))
    except Exception:  # noqa: BLE001 -- configuration illisible : valeur par défaut, jamais d'arrêt
        return float(REESSAI_LENT_MINUTES_DEFAUT)


def sirene_en_attente(maintenant: datetime | None = None) -> datetime | None:
    """Échéance de l'attente de SIRENE si elle court encore, sinon None."""
    quand = maintenant or _maintenant()
    if _sirene_en_attente_jusqua is not None and quand < _sirene_en_attente_jusqua:
        return _sirene_en_attente_jusqua
    return None


def reinitialiser_reessai_lent() -> None:
    """Pour les tests (et un redémarrage) : plus d'attente, plus de sonde."""
    global _sirene_en_attente_jusqua, _sirene_dernier_echec
    _sirene_en_attente_jusqua, _sirene_dernier_echec = None, None


def max_requetes_par_passe() -> int:
    brut = os.environ.get(VARIABLE_MAX_REQUETES)
    try:
        valeur = int(brut) if brut else MAX_REQUETES_DEFAUT
    except ValueError:
        valeur = MAX_REQUETES_DEFAUT
    return max(1, valeur)


def paires_a_rafraichir(
    engine: Engine, *, jours: int = JOURS_RAFRAICHISSEMENT_DEFAUT, code: str | None = None,
    departement: str | None = None, maintenant: datetime | None = None, codes: Sequence[str] | None = None,
) -> list[tuple[str, str]]:
    """Paires (code NAF, département) jamais mesurées ou mesurées il y a plus de `jours` jours, secteurs
    non exclus seulement. Ordre : les plus anciennes (ou jamais mesurées) d'abord, puis par code."""
    secteurs = referentiels.secteurs_tpe()
    zone = referentiels.zone()
    quand = maintenant or _maintenant()
    seuil = quand - timedelta(days=jours)
    derniers = repo.derniers_comptages_etablissements(engine, secteurs.naf_version)
    departements = zone.departements_zone() + (DEPARTEMENT_FRANCE,)
    if departement is not None:
        if departement not in departements:
            raise ValueError(f"Département {departement!r} hors zone ({', '.join(departements)})")
        departements = (departement,)
    codes_retenus = [s.code for s in secteurs.non_exclus()]
    if codes is not None:  # V2.8 : restreint à ces codes (la priorité est gérée par l'appelant, tranche par tranche)
        codes_retenus = [c for c in codes_retenus if c in set(codes)]
    if code is not None:
        if code not in codes_retenus:
            raise ValueError(f"Code NAF {code!r} inconnu ou exclu du référentiel")
        codes_retenus = [code]
    a_faire: list[tuple[datetime, str, str]] = []
    for c in codes_retenus:
        for d in departements:
            ligne = derniers.get((c, d))
            horodatage = ligne["horodatage"] if ligne else None
            if horodatage is not None and horodatage.tzinfo is None:
                horodatage = horodatage.replace(tzinfo=timezone.utc)
            if horodatage is None or horodatage < seuil:
                a_faire.append((horodatage or datetime.min.replace(tzinfo=timezone.utc), c, d))
    a_faire.sort()
    return [(c, d) for _, c, d in a_faire]


def rafraichir_paire(
    engine: Engine, code_naf: str, departement: str, *, plafond: int = api.PLAFOND_ECHANTILLON_DEFAUT,
    maintenant: datetime | None = None,
) -> tuple[int, int, int]:
    """Mesure UNE paire et l'écrit. Renvoie (requêtes, prospects nouveaux, prospects déjà connus).
    Lève `ErreurCollecte` si l'API échoue : rien n'est alors écrit pour cette paire."""
    secteurs = referentiels.secteurs_tpe()
    zone = referentiels.zone()
    quand = maintenant or _maintenant()
    if departement == DEPARTEMENT_FRANCE:
        page = api.compter_entreprises_actives(code_naf, None, engine=engine)
        repo.enregistrer_comptage_etablissements(
            engine, code_naf=code_naf, naf_version=secteurs.naf_version, departement=departement,
            nb_entreprises_actives=page.total_resultats, comptage_plafonne=page.plafonne,
            nb_etablissements_listes=None, echantillon_complet=None,
            plafond_echantillon=None, requetes=1, source_url=page.url, horodatage=quand,
        )
        return 1, 0, 0
    echantillon = api.collecter_echantillon(code_naf, departement, plafond=plafond, engine=engine)
    lignes = []
    for p in echantillon.prospects:
        distance = None
        if p.latitude is not None and p.longitude is not None:
            distance = round(api.distance_km(zone.centre.latitude, zone.centre.longitude, p.latitude, p.longitude), 2)
        lignes.append({
            "siret": p.siret, "code_naf": p.code_naf, "departement": departement, "siren": p.siren,
            "raison_sociale": p.raison_sociale, "adresse": p.adresse, "code_postal": p.code_postal,
            "code_commune": p.code_commune, "commune": p.commune, "latitude": p.latitude, "longitude": p.longitude,
            "distance_centre_km": distance, "tranche_effectif_salarie": p.tranche_effectif_salarie,
            "categorie_entreprise": p.categorie_entreprise, "est_siege": p.est_siege,
        })
    nouveaux, connus = repo.enregistrer_prospects(engine, lignes, naf_version=secteurs.naf_version, maintenant=quand)
    repo.enregistrer_comptage_etablissements(
        engine, code_naf=code_naf, naf_version=secteurs.naf_version, departement=departement,
        nb_entreprises_actives=echantillon.total_entreprises, comptage_plafonne=echantillon.total_plafonne,
        nb_etablissements_listes=len(echantillon.prospects),
        echantillon_complet=echantillon.complet, plafond_echantillon=plafond,
        requetes=echantillon.pages_lues, source_url=echantillon.url_premiere_page, horodatage=quand,
    )
    return echantillon.pages_lues, nouveaux, connus


def rafraichir_etablissements(
    engine: Engine, *, jours: int = JOURS_RAFRAICHISSEMENT_DEFAUT, max_requetes: int | None = None,
    code: str | None = None, departement: str | None = None, plafond: int = api.PLAFOND_ECHANTILLON_DEFAUT,
    codes: Sequence[str] | None = None,
) -> ResumePasse:
    """Une passe : traite les paires à rafraîchir tant que le plafond de requêtes n'est pas atteint. Une
    passe interrompue reprend où elle s'est arrêtée à la suivante (les paires déjà mesurées sont sautées).
    V2.8b : pendant l'attente du réessai lent, aucune requête ; à l'échéance, une seule paire sonde l'API."""
    global _sirene_en_attente_jusqua, _sirene_dernier_echec
    plafond_requetes = max_requetes if max_requetes is not None else max_requetes_par_passe()
    paires = paires_a_rafraichir(engine, jours=jours, code=code, departement=departement, codes=codes)
    resume = ResumePasse(paires_prevues=len(paires))
    if not paires:
        return resume
    maintenant = _maintenant()
    echeance = sirene_en_attente(maintenant)
    if echeance is not None:
        resume.arret = (f"SIRENE en attente (réessai lent) jusqu'à {echeance.strftime('%H:%M')} UTC, aucune requête ; "
                        f"dernier échec : {_sirene_dernier_echec or '?'}")
        return resume
    sonde = _sirene_en_attente_jusqua is not None  # l'attente vient d'expirer : une seule paire pour vérifier que l'API répond
    echecs_max = 1 if sonde else ECHECS_CONSECUTIFS_MAX
    echecs_consecutifs = 0
    for code_naf, dep in paires:
        if resume.requetes >= plafond_requetes:
            resume.arret = f"plafond de {plafond_requetes} requêtes atteint (reprise à la prochaine passe)"
            break
        try:
            requetes, nouveaux, connus = rafraichir_paire(engine, code_naf, dep, plafond=plafond)
        except ErreurCollecte as exc:
            echecs_consecutifs += 1
            resume.paires_en_echec += 1
            resume.echecs.append(f"{code_naf}/{dep} : {exc}")
            logger.warning("Établissements %s/%s : échec (%s)", code_naf, dep, exc)
            if echecs_consecutifs >= echecs_max:
                minutes = reessai_lent_minutes()
                _sirene_en_attente_jusqua = _maintenant() + timedelta(minutes=minutes)
                _sirene_dernier_echec = getattr(exc, "type_erreur", None) or type(exc).__name__
                motif = "sonde du réessai lent en échec" if sonde else f"{ECHECS_CONSECUTIFS_MAX} échecs consécutifs : API indisponible"
                resume.arret = (f"{motif} ({_sirene_dernier_echec}), passe arrêtée ; prochain essai à "
                                f"{_sirene_en_attente_jusqua.strftime('%H:%M')} UTC (réessai lent, {minutes:.0f} min)")
                logger.warning("[établissements] %s", resume.arret)
                break
            continue
        if sonde:
            logger.info("[établissements] sonde du réessai lent réussie : SIRENE répond de nouveau, passe normale rouverte.")
            sonde, echecs_max = False, ECHECS_CONSECUTIFS_MAX
            _sirene_en_attente_jusqua, _sirene_dernier_echec = None, None
        echecs_consecutifs = 0
        resume.paires_mesurees += 1
        resume.requetes += requetes
        resume.prospects_nouveaux += nouveaux
        resume.prospects_deja_connus += connus
    return resume
