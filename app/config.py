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
def sources_autorisees() -> dict[str, Any]:
    return _load_yaml("sources_autorisees.yaml")


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
