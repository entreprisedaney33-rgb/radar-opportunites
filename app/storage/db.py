from __future__ import annotations

from functools import lru_cache

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

from app.config import get_settings
from app.storage.schema import metadata


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    url = get_settings().database_url
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=connect_args, future=True)


# Colonnes ajoutées à une table déjà existante en production (migration
# additive, sous-étape 0.7 : `metadata.create_all` ne fait jamais d'ALTER sur
# une table déjà créée, seulement du CREATE TABLE IF NOT EXISTS — il faut
# donc les ajouter nous-mêmes, une fois, si elles manquent). Type SQL par
# colonne (sous-étape 3.10 : avant cette sous-étape, toutes les colonnes
# additives étaient du texte, un seul type VARCHAR suffisait -- `sortie_tronquee`
# est la première colonne additive booléenne, elle a besoin de son propre
# type pour être cohérente entre SQLite (tolérant, affinité seulement) et
# PostgreSQL (strict) — sans ça, un `INSERT` d'un booléen SQLAlchemy contre
# une colonne PostgreSQL VARCHAR échouerait en production, invisible dans la
# suite de tests par défaut, qui tourne sur SQLite.
_COLONNES_ADDITIVES: dict[str, dict[str, str]] = {
    "usage_events": {
        "role": "VARCHAR", "opportunity_id": "VARCHAR",
        "issue": "VARCHAR", "sortie_tronquee": "BOOLEAN",
    },
    "sources": {"flux_origine": "VARCHAR", "requete_origine": "VARCHAR", "etiquette": "VARCHAR"},
    "opportunities": {"secteur_provenance": "VARCHAR", "secteur_citation": "VARCHAR"},
}


def _appliquer_migrations_additives(engine: Engine) -> None:
    inspecteur = inspect(engine)
    for table, colonnes in _COLONNES_ADDITIVES.items():
        if table not in inspecteur.get_table_names():
            continue  # table toute neuve : déjà créée avec ces colonnes par create_all
        colonnes_existantes = {c["name"] for c in inspecteur.get_columns(table)}
        for colonne, type_sql in colonnes.items():
            if colonne in colonnes_existantes:
                continue
            with engine.begin() as cx:
                cx.execute(text(f"ALTER TABLE {table} ADD COLUMN {colonne} {type_sql}"))


def migrer(engine: Engine | None = None) -> None:
    """Crée les tables manquantes (idempotent : `checkfirst=True`, comportement
    par défaut — ne touche pas aux tables déjà présentes), puis applique les
    migrations additives de colonnes (idempotent aussi : vérifie d'abord ce
    qui existe déjà)."""
    moteur = engine or get_engine()
    metadata.create_all(moteur)
    _appliquer_migrations_additives(moteur)
