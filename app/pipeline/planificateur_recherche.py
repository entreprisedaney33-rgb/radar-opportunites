"""Planificateur de rotation pour les connecteurs de recherche (sous-étape
1.2 : Reddit ; généralisé en sous-étape 1.4 pour brancher Hacker News de la
même façon, `app/adapters/hn_recherche.py` — question laissée ouverte en 1.3).

Le produit combinaisons × expressions dépasse largement le raisonnable à
interroger en un seul passage (325 pour Reddit, 13 subs × 25 expressions ;
50 pour HN, 2 tags × 25 expressions) : le saturer à chaque passage
martèlerait le débit de ces services et les plafonds réseau (§3,
garde-fous). Ce module choisit, à chaque passage, les flux « dus » (jamais
visités, ou dont la dernière visite remonte à plus de `intervalle_heures`),
en commençant par les plus anciens — jamais visité étant toujours plus
prioritaire que déjà visité — jusqu'à `max_par_passage`.

Fonction pure : aucun accès réseau ni base ici. L'appelant
(`app/pipeline/orchestrator.py`) lit l'état réel (dernières visites) et
l'écrit après sélection (`app/storage/repo.py::lire_dernieres_visites_recherche`
/ `marquer_flux_recherche_visites`) — un appel séparé par connecteur (Reddit,
HN...), chacun avec ses propres quotas de rotation, mais le même mécanisme
générique ci-dessous.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class FluxRecherche:
    """`source` distingue le connecteur (`"reddit"` ou `"hn"` — voir
    `app/adapters/reddit_recherche.py` / `app/adapters/hn_recherche.py`).
    `parametre` est le sous-paramètre propre à ce connecteur : nom du
    subreddit pour Reddit, tag `comment`/`ask_hn` pour HN."""

    source: str
    parametre: str
    expression_cle: str
    expression_texte: str

    @property
    def id(self) -> str:
        return f"{self.source}_recherche:{self.parametre}:{self.expression_cle}"


def choisir_flux_a_visiter(
    flux: list[FluxRecherche],
    dernieres_visites: dict[str, datetime],
    *,
    maintenant: datetime,
    intervalle_heures: float,
    max_par_passage: int,
) -> list[FluxRecherche]:
    """`dernieres_visites` : id de flux -> horodatage de dernière visite (un
    flux absent du dictionnaire n'a jamais été visité). Renvoie les flux dus,
    triés du moins récemment visité au plus récemment visité (jamais visité
    en tête), tronqués à `max_par_passage`."""
    delta = timedelta(hours=intervalle_heures)
    epoque_jamais_visite = datetime.min.replace(tzinfo=maintenant.tzinfo)

    def derniere_visite(f: FluxRecherche) -> datetime:
        return dernieres_visites.get(f.id, epoque_jamais_visite)

    dus = [f for f in flux if maintenant - derniere_visite(f) >= delta]
    dus.sort(key=derniere_visite)
    return dus[:max_par_passage]
