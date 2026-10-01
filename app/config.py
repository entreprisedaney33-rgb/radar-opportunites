"""Chargement de la configuration (YAML versionné + variables d'environnement).

Rien d'obligatoire ici ne doit planter par défaut : sans clé API, le système
tourne en mode démo (voir adapters/model_client.py et adapters/demo_adapter.py).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def _load_yaml(nom_fichier: str) -> dict[str, Any]:
    chemin = CONFIG_DIR / nom_fichier
    with chemin.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@lru_cache(maxsize=1)
def secteurs() -> dict[str, Any]:
    return _load_yaml("secteurs.yaml")


@lru_cache(maxsize=1)
def poids_scoring() -> dict[str, Any]:
    return _load_yaml("poids_scoring.yaml")


@lru_cache(maxsize=1)
def quotas() -> dict[str, Any]:
    return _load_yaml("quotas.yaml")


@lru_cache(maxsize=1)
def faisabilite() -> dict[str, Any]:
    """Sous-étape 4.1 : critères de « accessible en solo »
    (`config/faisabilite.yaml`), voir `app.faisabilite`."""
    return _load_yaml("faisabilite.yaml")


@lru_cache(maxsize=1)
def etiquetage() -> dict[str, Any]:
    """V2.4 : réglages de l'étiquetage des offres et de l'agrégation (`config/etiquetage.yaml`)."""
    return _load_yaml("etiquetage.yaml")


@lru_cache(maxsize=1)
def fiches() -> dict[str, Any]:
    """V2.5 : sélection, score v2, décision et rafraîchissement des fiches (`config/fiches.yaml`)."""
    return _load_yaml("fiches.yaml")


@lru_cache(maxsize=1)
def concurrence() -> dict[str, Any]:
    """V2.6 : requêtes, plafonds et marqueurs de la cartographie de la concurrence (`config/concurrence.yaml`)."""
    return _load_yaml("concurrence.yaml")


@lru_cache(maxsize=1)
def cycle_v2() -> dict[str, Any]:
    """V2.8 : cycle du worker (cartographie initiale, régime quotidien, interrupteur du pipeline v1) (`config/cycle_v2.yaml`)."""
    return _load_yaml("cycle_v2.yaml")


@lru_cache(maxsize=1)
def sources_autorisees() -> dict[str, Any]:
    return _load_yaml("sources_autorisees.yaml")


@lru_cache(maxsize=1)
def tarifs() -> dict[str, Any]:
    return _load_yaml("tarifs.yaml")


@lru_cache(maxsize=1)
def domaines_exclus_concurrents() -> frozenset[str]:
    """Sous-étape 3.15 : domaines qui ne sont jamais des concurrents
    (`config/domaines_exclus_concurrents.yaml`), utilisé par
    `app.enqueteur.concurrents.identifier_concurrents`. Les deux catégories
    du fichier (sources du radar, plateformes) sont combinées en un seul
    ensemble -- le code n'a jamais besoin de les distinguer."""
    brut = _load_yaml("domaines_exclus_concurrents.yaml")
    return frozenset(
        domaine.lower() for liste in brut.values() for domaine in liste
    )


@dataclass(frozen=True)
class Settings:
    database_url: str
    anthropic_api_key: str | None
    model_tri: str
    model_approfondi: str
    apify_token: str | None
    ui_password: str | None
    pause_all: bool

    @property
    def has_model_access(self) -> bool:
        return bool(self.anthropic_api_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        database_url=os.environ.get("DATABASE_URL", "sqlite:///./radar_local.db"),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        model_tri=os.environ.get("RADAR_MODEL_TRI", "claude-haiku-4-5-20251001"),
        model_approfondi=os.environ.get("RADAR_MODEL_APPROFONDI", "claude-sonnet-5"),
        apify_token=os.environ.get("APIFY_TOKEN") or None,
        ui_password=os.environ.get("RADAR_UI_PASSWORD") or None,
        pause_all=os.environ.get("RADAR_PAUSE_ALL", "").strip().lower() in {"1", "true", "yes"},
    )
