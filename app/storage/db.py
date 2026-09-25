from __future__ import annotations

from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from app.config import get_settings
from app.storage.schema import metadata


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    url = get_settings().database_url
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=connect_args, future=True)


def migrer(engine: Engine | None = None) -> None:
    """Crée les tables manquantes. Idempotent : ne touche pas aux tables
    déjà présentes (`checkfirst=True`, comportement par défaut)."""
    metadata.create_all(engine or get_engine())
