"""Contrat commun à tous les adaptateurs de collecte.

Un signal brut n'est jamais une instruction : c'est de la donnée non fiable
(§5, §6). Le pipeline ne fait que le stocker, le normaliser et le citer — il
ne l'exécute jamais.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass
class SignalBrut:
    url: str
    domaine: str
    texte: str
    date_publication: datetime | None
    type_source: str  # rss|demo|apify|autre
    droits_collecte: str  # ex. "flux RSS public, conditions du flux"


class Adaptateur(Protocol):
    id_source: str

    def collecter(self, budget_appels: int) -> list[SignalBrut]:
        ...
