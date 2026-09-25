"""Adaptateur RSS — source publique, sans clé, sans contournement de CGU.

Chaque entrée du flux devient un `SignalBrut` avec son URL d'origine réelle
(jamais réécrite), utilisée telle quelle comme preuve traçable.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import feedparser

from app.adapters.base import SignalBrut
from app.adapters.http import ErreurCollecte, get_with_retry

logger = logging.getLogger(__name__)


class AdaptateurRSS:
    def __init__(self, id_source: str, nom: str, url: str):
        self.id_source = id_source
        self.nom = nom
        self.url = url

    def collecter(self, budget_appels: int, *, engine=None) -> list[SignalBrut]:
        """`engine` (sous-étape 3.7 d'AMELIORATIONS.md) : optionnel, `None`
        par défaut -- fourni par le pipeline réel (`app.pipeline.orchestrator._collecter`)
        pour journaliser l'appel dans `journal_http`, jamais par un appel
        direct dans un test unitaire."""
        journal = {"engine": engine, "contexte": self.id_source} if engine is not None else {}
        try:
            resp = get_with_retry(self.url, **journal)
        except ErreurCollecte as exc:
            logger.warning("Source %s indisponible : %s", self.id_source, exc)
            return []

        flux = feedparser.parse(resp.content)
        signaux: list[SignalBrut] = []
        for entree in flux.entries[:budget_appels]:
            url_entree = getattr(entree, "link", None)
            if not url_entree:
                continue
            titre = getattr(entree, "title", "") or ""
            resume = getattr(entree, "summary", "") or ""
            date_publication = None
            if getattr(entree, "published_parsed", None):
                date_publication = datetime(*entree.published_parsed[:6], tzinfo=timezone.utc)
            signaux.append(
                SignalBrut(
                    url=url_entree,
                    domaine=self.nom,
                    texte=f"{titre} — {resume}".strip(" —"),
                    date_publication=date_publication,
                    type_source="rss",
                    droits_collecte=f"flux RSS public ({self.url}), conditions du flux",
                    flux_origine=self.nom,
                )
            )
        return signaux
