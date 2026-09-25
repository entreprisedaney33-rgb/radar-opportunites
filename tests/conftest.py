from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from app import config as cfg
from app.storage.db import migrer


@pytest.fixture(autouse=True)
def _cache_config_propre(monkeypatch):
    """Chaque test repart d'une configuration propre : `get_settings()` est
    en cache (lru_cache), donc un test qui change une variable d'env sans
    vider ce cache verrait l'ancienne valeur."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("RADAR_PAUSE_ALL", raising=False)
    cfg.get_settings.cache_clear()
    yield
    cfg.get_settings.cache_clear()


@pytest.fixture
def engine_test(tmp_path):
    # Fichier sqlite dédié au test (pas ":memory:" : chaque connexion du pool
    # SQLAlchemy ouvrirait sinon sa propre base vide).
    chemin = tmp_path / "test.db"
    moteur = create_engine(f"sqlite:///{chemin}", future=True, connect_args={"check_same_thread": False})
    migrer(moteur)
    return moteur
