"""Connecteur de recherche Reddit (sous-étape 1.2 d'AMELIORATIONS.md) : un
flux = une combinaison (subreddit, expression du lexique de douleur).

Format vérifié manuellement le 25/09/2026 (hors tests, voir Journal 1.2) :
`https://www.reddit.com/r/<sub>/search.rss?q=<expression>&restrict_sr=on&sort=new`
renvoie un flux Atom (`application/atom+xml`), pas du RSS 2.0 malgré
l'extension `.rss` — sans incidence : `feedparser` normalise les deux vers
les mêmes champs (`link`, `title`, `summary`, `published_parsed`), déjà
utilisés par `AdaptateurRSS`. Une recherche sans résultat renvoie un flux
Atom valide à zéro entrée (HTTP 200), jamais une erreur.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from urllib.parse import quote

import feedparser

from app.adapters.base import SignalBrut
from app.adapters.http import ErreurCollecte, get_with_retry

logger = logging.getLogger(__name__)

GABARIT_URL = "https://www.reddit.com/r/{sub}/search.rss?q={q}&restrict_sr=on&sort=new"


class AdaptateurRechercheReddit:
    def __init__(self, subreddit: str, expression_cle: str, expression_texte: str):
        self.subreddit = subreddit
        self.expression_cle = expression_cle
        self.expression_texte = expression_texte
        self.id_source = f"reddit_recherche:{subreddit}:{expression_cle}"
        self.url = GABARIT_URL.format(sub=subreddit, q=quote(expression_texte))

    def collecter(self, budget_appels: int) -> list[SignalBrut]:
        try:
            resp = get_with_retry(self.url)
        except ErreurCollecte as exc:
            # Même politique que AdaptateurRSS : une combinaison
            # indisponible (429 compris, déjà retenté avec backoff par
            # get_with_retry) n'arrête jamais la collecte des autres flux —
            # voir app/pipeline/orchestrator.py::_collecter.
            logger.warning("Recherche Reddit %s indisponible : %s", self.id_source, exc)
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
                    domaine=f"Reddit r/{self.subreddit} (recherche)",
                    texte=f"{titre} — {resume}".strip(" —"),
                    date_publication=date_publication,
                    type_source="rss",
                    droits_collecte=f"recherche Reddit publique ({self.url}), conditions du flux",
                    flux_origine=f"Reddit r/{self.subreddit} — recherche « {self.expression_texte} »",
                    type_flux="douleur",  # les subs interrogées sont déjà toutes `douleur` (app/sources.py)
                    requete_origine=self.expression_texte,
                )
            )
        return signaux
