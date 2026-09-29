"""Connecteur de recherche Reddit (sous-étape 1.2 d'AMELIORATIONS.md) : un
flux = une combinaison (subreddit, expression du lexique de douleur).

Sous-étape 4.0 : passe par l'API officielle Reddit (`app.adapters.reddit_api`,
OAuth application, `oauth.reddit.com/r/<sub>/search`), plus jamais par le
flux Atom `search.rss`. Même dédoublonnage, même lexique, même
planificateur. Sans les identifiants d'environnement, l'adaptateur est
inactif (liste vide, dit dans les logs par `reddit_api.obtenir_client`).
"""
from __future__ import annotations

import logging

from app.adapters import reddit_api
from app.adapters.base import SignalBrut
from app.adapters.http import ErreurCollecte, TropDeRequetes

logger = logging.getLogger(__name__)


class AdaptateurRechercheReddit:
    def __init__(self, subreddit: str, expression_cle: str, expression_texte: str):
        self.subreddit = subreddit
        self.expression_cle = expression_cle
        self.expression_texte = expression_texte
        self.id_source = f"reddit_recherche:{subreddit}:{expression_cle}"

    def collecter(self, budget_appels: int, *, engine=None) -> list[SignalBrut]:
        client = reddit_api.obtenir_client()
        if client is None:
            return []
        try:
            listing = client.get_json(
                f"/r/{self.subreddit}/search",
                {"q": self.expression_texte, "restrict_sr": 1, "sort": "new", "type": "link",
                 "limit": max(1, min(budget_appels, 100))},
                engine=engine, contexte=f"reddit_api:recherche:{self.subreddit}:{self.expression_cle}",
            )
        except TropDeRequetes:
            # Laissé remonter : le disjoncteur par passage de
            # `app.pipeline.orchestrator._collecter` compte ces 429 (3.10).
            raise
        except ErreurCollecte as exc:
            # Une combinaison indisponible n'arrête jamais la collecte des autres.
            logger.warning("Recherche Reddit %s indisponible : %s", self.id_source, exc)
            return []
        return reddit_api.signaux_depuis_listing(
            listing, domaine=f"Reddit r/{self.subreddit} (recherche)",
            droits=f"API officielle Reddit (OAuth application), /r/{self.subreddit}/search, usage interne, lecture seule",
            flux_origine=f"Reddit r/{self.subreddit} — recherche « {self.expression_texte} »",
            requete_origine=self.expression_texte, limite=budget_appels,
        )
