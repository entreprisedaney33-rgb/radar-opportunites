"""Connecteur de recherche Hacker News via l'API Algolia (sous-étape 1.3
d'AMELIORATIONS.md) : un flux = une combinaison (tag, expression du lexique
de douleur, `app/lexique_douleur.py`). Deux tags interrogés par expression —
les « deux flux logiques » demandés par le plan : `comment` (commentaires)
et `ask_hn` (posts Ask HN).

Format vérifié manuellement le 25/09/2026 (hors tests, voir Journal 1.3),
trois requêtes réelles vers `hn.algolia.com` (deux avec résultats, une sans) :
JSON, aucune clé nécessaire.
- `GET https://hn.algolia.com/api/v1/search_by_date?query=<expr>&tags=comment`
  → objets avec `comment_text` (texte du commentaire), `story_title` (titre
  de la discussion), `objectID`, `created_at` (UTC, suffixe `Z`).
- même URL avec `tags=ask_hn` → objets avec `title` (titre du post),
  `story_text` (corps, peut être vide), `objectID`, `created_at`.
- une recherche sans résultat renvoie `{"hits": [], "nbHits": 0}` en HTTP
  200, jamais une erreur.
Dans les deux cas, l'URL canonique du signal est
`https://news.ycombinator.com/item?id=<objectID>` (mène directement au
commentaire ou au post).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from urllib.parse import quote

from app.adapters.base import SignalBrut
from app.adapters.http import ErreurCollecte, get_with_retry

logger = logging.getLogger(__name__)

TAGS_VALIDES = {"comment", "ask_hn"}

NOMS_TAGS = {"comment": "commentaires", "ask_hn": "Ask HN"}

GABARIT_URL = "https://hn.algolia.com/api/v1/search_by_date?query={q}&tags={tag}"


class AdaptateurRechercheHN:
    def __init__(self, tag: str, expression_cle: str, expression_texte: str):
        if tag not in TAGS_VALIDES:
            raise ValueError(f"Tag de recherche HN inconnu : {tag!r} (attendu {sorted(TAGS_VALIDES)}).")
        self.tag = tag
        self.expression_cle = expression_cle
        self.expression_texte = expression_texte
        self.id_source = f"hn_recherche:{tag}:{expression_cle}"
        self.url = GABARIT_URL.format(q=quote(expression_texte), tag=tag)

    def collecter(self, budget_appels: int) -> list[SignalBrut]:
        try:
            resp = get_with_retry(self.url)
        except ErreurCollecte as exc:
            # Même politique que AdaptateurRechercheReddit : une combinaison
            # indisponible n'arrête jamais la collecte des autres flux — voir
            # app/pipeline/orchestrator.py::_collecter.
            logger.warning("Recherche HN %s indisponible : %s", self.id_source, exc)
            return []

        try:
            donnees = resp.json()
        except ValueError:
            logger.warning("Recherche HN %s : réponse non JSON.", self.id_source)
            return []

        signaux: list[SignalBrut] = []
        for hit in donnees.get("hits", [])[:budget_appels]:
            object_id = hit.get("objectID")
            if not object_id:
                continue
            if self.tag == "comment":
                titre = hit.get("story_title") or ""
                texte = hit.get("comment_text") or ""
            else:  # ask_hn
                titre = hit.get("title") or ""
                texte = hit.get("story_text") or ""

            date_publication = None
            horodatage = hit.get("created_at")
            if horodatage:
                date_publication = datetime.fromisoformat(horodatage.replace("Z", "+00:00")).astimezone(timezone.utc)

            signaux.append(
                SignalBrut(
                    url=f"https://news.ycombinator.com/item?id={object_id}",
                    domaine=f"Hacker News ({NOMS_TAGS[self.tag]}, recherche)",
                    texte=f"{titre} — {texte}".strip(" —"),
                    date_publication=date_publication,
                    type_source="autre",  # JSON via l'API Algolia, ni rss ni demo ni apify
                    droits_collecte=f"recherche Hacker News publique via l'API Algolia ({self.url}), conditions de l'API",
                    flux_origine=f"Hacker News — recherche « {self.expression_texte} » ({NOMS_TAGS[self.tag]})",
                    type_flux="douleur",
                    requete_origine=self.expression_texte,
                )
            )
        return signaux
