"""Collecte des offres d'emploi France Travail (V2.3, RADAR-V2.md).

    python -m app.cli offres [--jours N] [--code 69.20Z] [--max-requetes N]

Pour chaque code NAF retenu (non exclu) de `config/secteurs_tpe.yaml` : toutes les offres ACTIVES créées
depuis la fin de la collecte précédente (un jour de chevauchement, pour voir les offres mises à jour), ou
depuis 90 jours à la toute première collecte. Une collecte quotidienne suffit. Dédoublonnage par
identifiant d'offre ; rien n'est jamais supprimé.

Disjoncteur : après 3 codes consécutifs en échec la passe s'arrête ; sans identifiants elle s'arrête tout de suite
avec un message clair. Plafond de requêtes par passe (`RADAR_FT_MAX_REQUETES`, défaut 3 000) ; les codes non traités
reprennent à la passe suivante (les plus anciennement collectés passent d'abord).

Ce module n'est PAS branché au worker (v1 suspendue) : le branchement se décide à la mise en production (V2.8).
"""
from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy.engine import Engine

from app import referentiels
from app.adapters import france_travail as ft
from app.adapters.http import ErreurCollecte
from app.storage import repo

logger = logging.getLogger(__name__)

JOURS_INITIAL = 90
CHEVAUCHEMENT = timedelta(days=1)
MAX_REQUETES_DEFAUT = 3000
ECHECS_CONSECUTIFS_MAX = 3
VARIABLE_MAX_REQUETES = "RADAR_FT_MAX_REQUETES"


@dataclass
class ResumeCollecte:
    codes_prevus: int = 0
    codes_collectes: int = 0
    codes_en_echec: int = 0
    requetes: int = 0
    offres_lues: int = 0
    offres_nouvelles: int = 0
    offres_ignorees: int = 0  # offres inexploitables (sans identifiant ou sans date de création)
    fenetres_tronquees: int = 0
    arret: str | None = None
    echecs: list[str] = field(default_factory=list)


def _maintenant() -> datetime:
    return datetime.now(timezone.utc)


def max_requetes_par_passe() -> int:
    brut = os.environ.get(VARIABLE_MAX_REQUETES)
    try:
        valeur = int(brut) if brut else MAX_REQUETES_DEFAUT
    except ValueError:
        valeur = MAX_REQUETES_DEFAUT
    return max(1, valeur)


def fenetre_pour_code(derniere_fin: datetime | None, maintenant: datetime, *, jours: int | None = None) -> tuple[datetime, datetime]:
    """(début, fin) de la plage de dates de création à interroger. `jours` impose un recul fixe ; sinon on
    reprend à la fin de la collecte précédente (moins un jour de chevauchement), ou 90 jours à la première."""
    if jours is not None:
        return maintenant - timedelta(days=jours), maintenant
    if derniere_fin is None:
        return maintenant - timedelta(days=JOURS_INITIAL), maintenant
    return min(derniere_fin - CHEVAUCHEMENT, maintenant), maintenant


def codes_a_collecter(engine: Engine, *, code: str | None = None, codes: Sequence[str] | None = None) -> list[str]:
    """Codes NAF non exclus, les plus anciennement collectés (ou jamais) d'abord. `codes` (V2.8) : restreint à ces codes (la priorité
    est gérée par l'appelant, tranche par tranche)."""
    secteurs = referentiels.secteurs_tpe()
    codes_retenus = [s.code for s in secteurs.non_exclus()]
    if codes is not None:
        codes_retenus = [c for c in codes_retenus if c in set(codes)]
    if code is not None:
        if code not in codes_retenus:
            raise ValueError(f"Code NAF {code!r} inconnu ou exclu du référentiel")
        codes_retenus = [code]
    fins = repo.dernieres_fins_collecte_offres(engine, secteurs.naf_version)
    epoque = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(codes_retenus, key=lambda c: (fins.get(c, epoque), c))


def _fournisseur_de_jeton(engine: Engine):
    etat: dict[str, ft.Jeton] = {}

    def jeton() -> ft.Jeton:
        if "j" not in etat or not etat["j"].valide():
            etat["j"] = ft.obtenir_jeton(engine=engine)
        return etat["j"]

    return jeton


def collecter_offres(
    engine: Engine, *, jours: int | None = None, code: str | None = None, max_requetes: int | None = None,
    maintenant: datetime | None = None, fournisseur_de_jeton=None, codes: Sequence[str] | None = None,
) -> ResumeCollecte:
    secteurs = referentiels.secteurs_tpe()
    quand = maintenant or _maintenant()
    plafond_requetes = max_requetes if max_requetes is not None else max_requetes_par_passe()
    codes = codes_a_collecter(engine, code=code, codes=codes)
    fins = repo.dernieres_fins_collecte_offres(engine, secteurs.naf_version)
    jeton = fournisseur_de_jeton or _fournisseur_de_jeton(engine)
    resume = ResumeCollecte(codes_prevus=len(codes))
    echecs_consecutifs = 0
    for code_naf in codes:
        if resume.requetes >= plafond_requetes:
            resume.arret = f"plafond de {plafond_requetes} requêtes atteint (reprise à la prochaine passe)"
            break
        debut, fin = fenetre_pour_code(fins.get(code_naf), quand, jours=jours)
        try:
            collecte = ft.collecter_code(jeton, code_naf, debut, fin, engine=engine)
        except ft.IdentifiantsAbsents as exc:
            resume.arret = str(exc)
            break
        except ErreurCollecte as exc:
            echecs_consecutifs += 1
            resume.codes_en_echec += 1
            resume.echecs.append(f"{code_naf} : {exc}")
            logger.warning("Offres %s : échec (%s)", code_naf, exc)
            if echecs_consecutifs >= ECHECS_CONSECUTIFS_MAX:
                resume.arret = f"{ECHECS_CONSECUTIFS_MAX} échecs consécutifs : API indisponible, passe arrêtée"
                break
            continue
        echecs_consecutifs = 0
        lignes = [ligne for brute in collecte.offres if (ligne := ft.normaliser_offre(brute)) is not None]
        nouvelles, _connues = repo.enregistrer_offres(engine, lignes, naf_version=secteurs.naf_version, maintenant=quand)
        repo.enregistrer_collecte_offres(
            engine, code_naf=code_naf, naf_version=secteurs.naf_version, debut=debut, fin=fin, nb_offres=len(lignes),
            nb_nouvelles=nouvelles, requetes=collecte.requetes, fenetres_tronquees=collecte.fenetres_tronquees,
            horodatage=quand,
        )
        resume.codes_collectes += 1
        resume.requetes += collecte.requetes
        resume.offres_lues += len(lignes)
        resume.offres_nouvelles += nouvelles
        resume.offres_ignorees += len(collecte.offres) - len(lignes)
        resume.fenetres_tronquees += collecte.fenetres_tronquees
    return resume
