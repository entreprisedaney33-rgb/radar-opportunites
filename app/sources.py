"""Sources typées (sous-étape 1.1 d'AMELIORATIONS.md).

Chaque flux déclare s'il produit des signaux de `douleur` (alimentent le
Scout) ou d'`offre` (concurrence — jamais transformés en opportunité, voir
`app/pipeline/orchestrator.py`). Chargeur à validation stricte : un type ou
un secteur inconnu est une ERREUR au chargement, jamais un défaut silencieux
(§2, garde-fous).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app import config as cfg

CHEMIN_PAR_DEFAUT = Path(__file__).resolve().parent / "sources.yaml"

TYPES_VALIDES = {"douleur", "offre"}

CHAMPS_REQUIS = {"id", "nom", "url", "type", "langue", "actif"}


class ConfigSourcesInvalide(Exception):
    pass


@dataclass(frozen=True)
class SourceConfig:
    id: str
    nom: str
    url: str
    type: str  # douleur|offre
    secteur_par_defaut: str | None
    langue: str
    actif: bool
    budget_appels_par_nuit: int


def _secteurs_valides() -> set[str]:
    groupes = cfg.secteurs().get("secteurs", {})
    return {secteur for liste in groupes.values() for secteur in liste}


def _valider_entree(brut: Any, secteurs_valides: set[str]) -> SourceConfig:
    if not isinstance(brut, dict):
        raise ConfigSourcesInvalide(f"Entrée de source invalide (attendu un objet) : {brut!r}")

    manquants = CHAMPS_REQUIS - brut.keys()
    if manquants:
        raise ConfigSourcesInvalide(
            f"Source {brut.get('id', '?')!r} : champ(s) manquant(s) {sorted(manquants)}."
        )

    if brut["type"] not in TYPES_VALIDES:
        raise ConfigSourcesInvalide(
            f"Source {brut['id']!r} : type {brut['type']!r} inconnu (attendu {sorted(TYPES_VALIDES)})."
        )

    secteur = brut.get("secteur_par_defaut")
    if secteur is not None and secteur not in secteurs_valides:
        raise ConfigSourcesInvalide(
            f"Source {brut['id']!r} : secteur_par_defaut {secteur!r} inconnu "
            f"(voir les catégories de config/secteurs.yaml)."
        )

    return SourceConfig(
        id=brut["id"],
        nom=brut["nom"],
        url=brut["url"],
        type=brut["type"],
        secteur_par_defaut=secteur,
        langue=brut["langue"],
        actif=bool(brut["actif"]),
        budget_appels_par_nuit=brut.get("budget_appels_par_nuit", 10),
    )


def charger_sources(chemin: Path | None = None) -> list[SourceConfig]:
    """Lecture + validation stricte. `chemin` permet aux tests de charger une
    fixture sans toucher au fichier réel ; sans argument, lit `app/sources.yaml`."""
    chemin = chemin or CHEMIN_PAR_DEFAUT
    brut = yaml.safe_load(chemin.read_text(encoding="utf-8")) or []
    if not isinstance(brut, list):
        raise ConfigSourcesInvalide(f"{chemin} : attendu une liste de sources, trouvé {type(brut).__name__}.")

    secteurs_valides = _secteurs_valides()
    sources: list[SourceConfig] = []
    ids_vus: set[str] = set()
    for entree in brut:
        source = _valider_entree(entree, secteurs_valides)
        if source.id in ids_vus:
            raise ConfigSourcesInvalide(f"Identifiant de source en double : {source.id!r}.")
        ids_vus.add(source.id)
        sources.append(source)
    return sources


@lru_cache(maxsize=1)
def sources() -> list[SourceConfig]:
    return charger_sources()


_PATRON_SUBREDDIT = re.compile(r"reddit\.com/r/([A-Za-z0-9_]+)/")


def subreddits_douleur(liste_sources: list[SourceConfig] | None = None) -> list[str]:
    """Sous-étape 1.2 : les subreddits éligibles au connecteur de recherche
    (`app/adapters/reddit_recherche.py`, sub × expression) sont exactement
    les sources Reddit déjà déclarées `douleur` ci-dessus — une seule liste
    de vérité, jamais une deuxième dupliquée pour la recherche. Renvoie les
    noms de subreddits, dans l'ordre de `app/sources.yaml`, dédoublonnés."""
    vus: list[str] = []
    for src in liste_sources if liste_sources is not None else sources():
        if src.type != "douleur":
            continue
        correspondance = _PATRON_SUBREDDIT.search(src.url)
        if correspondance and correspondance.group(1) not in vus:
            vus.append(correspondance.group(1))
    return vus
