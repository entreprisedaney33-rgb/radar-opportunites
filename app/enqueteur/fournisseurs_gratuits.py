"""Fournisseurs gratuits de l'Enquêteur (sous-étape 3.2 d'AMELIORATIONS.md) :
Algolia HN, Reddit, magasin interne. Les trois implémentent
`FournisseurRecherche` (`app.enqueteur.fournisseurs`) — appelés par le
pipeline réel depuis la sous-étape 3.4
(`app.pipeline.orchestrator._phase_enquete`). Algolia HN et magasin interne
sont actifs par défaut ; Reddit ne l'est plus depuis la sous-étape 3.11 (voir
plus bas) -- les trois restent activables/désactivables individuellement via
`RADAR_ENQUETEUR_ACTIF_<NOM>` (`app.enqueteur.fournisseurs.RegistreFournisseurs.est_actif`).
Dans la suite de tests par défaut, Algolia HN et Reddit (seuls à faire de
vrais appels réseau) sont en plus désactivés par un réglage global
(`tests/conftest.py`, garde-fou §0.2.5) — seul « magasin interne » (aucun
réseau) reste actif par défaut en test.

Algolia HN et Reddit réutilisent le client HTTP et les précautions de débit
des connecteurs de recherche du Scout (sous-étapes 1.2/1.3,
`app.adapters.reddit_recherche` / `app.adapters.hn_recherche` : même
`get_with_retry` — User-Agent stable, retry plafonné, backoff sur 429 — même
parseur), mais interrogent une REQUÊTE LIBRE générée par
`app.enqueteur.gabarits` pour UNE opportunité donnée, pas une expression fixe
du lexique de douleur pour tout le radar. Différence pour Reddit : l'Enquêteur
n'a pas de subreddit cible (contrairement au Scout, où le sub vient de
`app/sources.yaml`), donc la recherche porte sur Reddit entier
(`/search.rss`, pas `/r/<sub>/search.rss`) — format Atom du même service,
non re-vérifié manuellement séparément (celui de 1.2 l'a été).

Le magasin interne, lui, ne fait AUCUNE requête réseau : il compare la
requête aux items `signal_concurrence` déjà stockés (sous-étape 1.1) avec la
même mesure de similarité que le dédoublonnage
(`app.pipeline.dedupe.similarite_lexicale`).

Format Reddit site entier — vérifié par 2 requêtes réelles hors tests le
25/09/2026 (voir Journal de la sous-étape 3.3) : sans `type=link`, la
recherche `/search.rss` mélange des posts ET des résultats de communauté
(un `<entry>` dont le lien pointe vers la racine d'un subreddit, sans date
de publication — 3 des 25 premiers résultats pour la requête `manually`).
Avec `type=link`, les 25 résultats sont bien des posts, chacun avec son
`<published>`. `GABARIT_URL_REDDIT_SITEWIDE` inclut donc `type=link`.

Sous-étape 3.11 :
- `FournisseurReddit` est désormais `actif_par_defaut=False` (point 5 du
  texte de 3.11) -- tant que l'API officielle Reddit n'est pas en place
  (piste notée en §9 d'AMELIORATIONS.md à l'étape 3.9), le laisser actif par
  défaut ne fait qu'exposer le disjoncteur ci-dessous en conditions réelles.
  `RADAR_ENQUETEUR_ACTIF_REDDIT=1` (mécanisme déjà posé en 3.1,
  `RegistreFournisseurs.est_actif`) le réactive explicitement.
- Disjoncteur (`app.enqueteur.disjoncteur`) : après
  `disjoncteur.SEUIL_ECHECS_CONSECUTIFS` réponses 429 CONSÉCUTIVES (une par
  appel à `rechercher`, chacun ayant déjà épuisé ses tentatives internes --
  `TropDeRequetes`), Reddit est mis en pause pour
  `disjoncteur.DUREE_PAUSE_MINUTES` : `rechercher` renvoie `[]`
  immédiatement, sans même tenter l'appel HTTP, tant que la pause n'est pas
  écoulée. État persisté en base (jamais en mémoire du processus, voir la
  docstring de `app.enqueteur.disjoncteur`) -- absent si `engine` est `None`
  (comportement d'un fournisseur construit directement dans un test unitaire
  sans base : jamais de disjoncteur dans ce cas, comme pour la journalisation
  HTTP ci-dessus)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from urllib.parse import quote

import feedparser
from sqlalchemy.engine import Engine

from app import config as cfg
from app.adapters.hn_recherche import GABARIT_URL as GABARIT_URL_HN
from app.adapters.http import ErreurCollecte, TropDeRequetes, get_with_retry
from app.enqueteur import disjoncteur
from app.enqueteur.fournisseurs import (
    DefinitionFournisseur,
    RegistreFournisseurs,
    ResultatRecherche,
)
from app.pipeline.dedupe import similarite_lexicale
from app.storage import repo

logger = logging.getLogger(__name__)

GABARIT_URL_REDDIT_SITEWIDE = "https://www.reddit.com/search.rss?q={q}&sort=relevance&type=link"

_TAGS_HN = ("comment", "ask_hn")

_LONGUEUR_TITRE_REPLI = 120  # magasin interne : pas de champ "titre" distinct en base, voir docstring de classe


def _lire_etat_disjoncteur(engine: Engine, cle: str) -> disjoncteur.EtatDisjoncteur:
    ligne = repo.lire_disjoncteur_enqueteur(engine, cle)
    if ligne is None:
        return disjoncteur.ETAT_INITIAL
    return disjoncteur.EtatDisjoncteur(**ligne)


def _ecrire_etat_disjoncteur(engine: Engine, cle: str, etat: disjoncteur.EtatDisjoncteur) -> None:
    repo.ecrire_disjoncteur_enqueteur(
        engine, cle, echecs_consecutifs=etat.echecs_consecutifs, pause_jusqu_a=etat.pause_jusqu_a,
    )


class FournisseurAlgoliaHN:
    """Réutilise le gabarit d'URL et le client HTTP du connecteur de 1.3
    (`app.adapters.hn_recherche`), sur les deux mêmes tags (`comment` et
    `ask_hn`), pour une requête libre plutôt qu'une expression fixe du
    lexique."""

    nom = "algolia_hn"
    sans_reseau = False  # vrai appel réseau -- consomme max_requetes_recherche_par_jour

    def __init__(self, engine: Engine | None = None):
        """`engine` (sous-étape 3.7 d'AMELIORATIONS.md) : optionnel, `None`
        par défaut (comportement inchangé pour tout appelant existant, y
        compris les tests unitaires qui construisent ce fournisseur
        directement) -- fourni par `construire_registre_fournisseurs_gratuits`
        pour journaliser les appels réels dans `journal_http`."""
        self.engine = engine

    def rechercher(self, requete: str, limite: int) -> list[ResultatRecherche]:
        resultats: list[ResultatRecherche] = []
        for tag in _TAGS_HN:
            if len(resultats) >= limite:
                break
            url = GABARIT_URL_HN.format(q=quote(requete), tag=tag)
            journal = (
                {"engine": self.engine, "contexte": f"enqueteur_recherche:{self.nom}:{tag}"}
                if self.engine is not None else {}
            )
            try:
                resp = get_with_retry(url, **journal)
            except ErreurCollecte as exc:
                logger.warning("Enquêteur Algolia HN (%s) indisponible pour %r : %s", tag, requete, exc)
                continue
            try:
                donnees = resp.json()
            except ValueError:
                logger.warning("Enquêteur Algolia HN (%s) : réponse non JSON pour %r.", tag, requete)
                continue

            for hit in donnees.get("hits", []):
                if len(resultats) >= limite:
                    break
                object_id = hit.get("objectID")
                if not object_id:
                    continue
                if tag == "comment":
                    titre, extrait = hit.get("story_title") or "", hit.get("comment_text") or ""
                else:  # ask_hn
                    titre, extrait = hit.get("title") or "", hit.get("story_text") or ""
                if not titre and not extrait:
                    continue

                horodatage_source = None
                horodatage = hit.get("created_at")
                if horodatage:
                    horodatage_source = datetime.fromisoformat(horodatage.replace("Z", "+00:00")).astimezone(timezone.utc)

                resultats.append(
                    ResultatRecherche(
                        url=f"https://news.ycombinator.com/item?id={object_id}",
                        titre=titre or extrait[:_LONGUEUR_TITRE_REPLI],
                        extrait=extrait or titre,
                        horodatage_source=horodatage_source,
                        fournisseur=self.nom,
                    )
                )
        return resultats


class FournisseurReddit:
    """Réutilise le client HTTP et le parseur Atom du connecteur de 1.2
    (`app.adapters.reddit_recherche`, `feedparser` déjà employé pour les
    mêmes champs `link`/`title`/`summary`/`published_parsed`), mais sur la
    recherche Reddit SITE ENTIER : l'Enquêteur enquête sur une opportunité
    donnée, pas sur un subreddit fixé à l'avance par `app/sources.yaml`.
    `type=link` dans `GABARIT_URL_REDDIT_SITEWIDE` est nécessaire : sans lui,
    Reddit mélange des résultats de communauté (posts d'accueil de
    subreddits, sans date) parmi les vrais posts (vérifié le 25/09/2026,
    sous-étape 3.3)."""

    nom = "reddit"
    sans_reseau = False  # vrai appel réseau -- consomme max_requetes_recherche_par_jour

    def __init__(self, engine: Engine | None = None):
        """`engine` (sous-étape 3.7) : voir `FournisseurAlgoliaHN.__init__`.
        Sous-étape 3.11 : sert aussi de clé pour le disjoncteur (lecture/
        écriture en base) -- absent (`None`, cas des tests unitaires qui
        construisent ce fournisseur directement), le disjoncteur est
        simplement désactivé (jamais en pause, jamais mis à jour)."""
        self.engine = engine

    def rechercher(self, requete: str, limite: int) -> list[ResultatRecherche]:
        if self.engine is not None:
            etat = _lire_etat_disjoncteur(self.engine, disjoncteur.NOM_REDDIT)
            if disjoncteur.est_en_pause(etat, datetime.now(timezone.utc)):
                logger.info(
                    "Enquêteur Reddit : disjoncteur en pause jusqu'à %s -- requête %r non tentée.",
                    etat.pause_jusqu_a, requete,
                )
                return []

        url = GABARIT_URL_REDDIT_SITEWIDE.format(q=quote(requete))
        journal = (
            {"engine": self.engine, "contexte": f"enqueteur_recherche:{self.nom}"} if self.engine is not None else {}
        )
        try:
            resp = get_with_retry(url, **journal)
        except TropDeRequetes as exc:
            logger.warning("Enquêteur Reddit : 429 persistant pour %r : %s", requete, exc)
            if self.engine is not None:
                nouvel_etat = disjoncteur.apres_echec_429(
                    _lire_etat_disjoncteur(self.engine, disjoncteur.NOM_REDDIT), maintenant=datetime.now(timezone.utc),
                )
                _ecrire_etat_disjoncteur(self.engine, disjoncteur.NOM_REDDIT, nouvel_etat)
                if nouvel_etat.pause_jusqu_a is not None:
                    logger.warning(
                        "Enquêteur Reddit : %d échecs 429 consécutifs -- pause jusqu'à %s.",
                        disjoncteur.SEUIL_ECHECS_CONSECUTIFS, nouvel_etat.pause_jusqu_a,
                    )
            return []
        except ErreurCollecte as exc:
            logger.warning("Enquêteur Reddit indisponible pour %r : %s", requete, exc)
            return []

        if self.engine is not None:
            _ecrire_etat_disjoncteur(
                self.engine, disjoncteur.NOM_REDDIT,
                disjoncteur.apres_succes(_lire_etat_disjoncteur(self.engine, disjoncteur.NOM_REDDIT)),
            )

        flux = feedparser.parse(resp.content)
        resultats: list[ResultatRecherche] = []
        for entree in flux.entries[:limite]:
            url_entree = getattr(entree, "link", None)
            if not url_entree:
                continue
            titre = getattr(entree, "title", "") or ""
            extrait = getattr(entree, "summary", "") or ""
            horodatage_source = None
            if getattr(entree, "published_parsed", None):
                horodatage_source = datetime(*entree.published_parsed[:6], tzinfo=timezone.utc)
            resultats.append(
                ResultatRecherche(
                    url=url_entree,
                    titre=titre,
                    extrait=extrait or titre,
                    horodatage_source=horodatage_source,
                    fournisseur=self.nom,
                )
            )
        return resultats


class FournisseurMagasinInterne:
    """Aucune requête réseau : compare `requete` à chaque item
    `signal_concurrence` déjà stocké (sous-étape 1.1,
    `app.storage.repo.lister_signaux_concurrence`) avec
    `app.pipeline.dedupe.similarite_lexicale` (même mesure que le
    dédoublonnage), garde ceux au-dessus du seuil, trie par similarité
    décroissante. `sources` n'a pas de champ "titre" distinct (seulement
    `extrait` et `domaine`, voir `app/storage/schema.py`) : le titre renvoyé
    est un simple repli tronqué sur l'extrait, pas une vraie donnée
    supplémentaire.

    `sans_reseau = True` (sous-étape 3.6 d'AMELIORATIONS.md) : aucun appel
    réseau dans `rechercher` ci-dessous (une simple lecture en base), donc ce
    fournisseur ne doit jamais consommer `max_requetes_recherche_par_jour`
    (`app.enqueteur.enqueteur._rechercher_par_famille` lit cet attribut) --
    ce plafond protège des services tiers (Algolia HN, Reddit), pas une
    lecture locale. Les pages qu'il pointe restent, elles, fetchées sur le
    réseau par `app.enqueteur.fetch.collecter_preuves` comme n'importe quel
    autre résultat : seule la RECHERCHE est locale, pas la récupération de la
    page -- `max_fetchs_pages_par_jour` s'applique donc normalement."""

    nom = "magasin_interne"
    sans_reseau = True

    def __init__(self, engine: Engine, *, seuil: float | None = None):
        self.engine = engine
        self.seuil = seuil if seuil is not None else cfg.quotas()["seuil_similarite_magasin_interne"]

    def rechercher(self, requete: str, limite: int) -> list[ResultatRecherche]:
        candidats = repo.lister_signaux_concurrence(self.engine)

        notes: list[tuple[float, dict]] = []
        for candidat in candidats:
            similarite = similarite_lexicale(requete, candidat["extrait"])
            if similarite < self.seuil:
                continue
            notes.append((similarite, candidat))
        notes.sort(key=lambda paire: paire[0], reverse=True)

        return [
            ResultatRecherche(
                url=candidat["url_canonique"],
                titre=candidat["extrait"][:_LONGUEUR_TITRE_REPLI],
                extrait=candidat["extrait"],
                horodatage_source=candidat["date_publication"],
                fournisseur=self.nom,
            )
            for _similarite, candidat in notes[:limite]
        ]


def construire_registre_fournisseurs_gratuits(engine: Engine) -> RegistreFournisseurs:
    """Instancie le registre avec les 3 fournisseurs gratuits (point 4 de la
    sous-étape 3.2 — chacun reste activable/désactivable individuellement via
    `RADAR_ENQUETEUR_ACTIF_<NOM>`,
    `app.enqueteur.fournisseurs.RegistreFournisseurs.est_actif`). Algolia HN
    et magasin interne actifs par défaut ; Reddit ne l'est plus depuis la
    sous-étape 3.11 (point 5 -- tant que l'API officielle n'est pas en
    place). Appelée par `app.pipeline.orchestrator._phase_enquete` depuis la
    sous-étape 3.4 (une instance par passage, pas un singleton -- même choix
    que noté en 3.1)."""
    registre = RegistreFournisseurs()
    registre.enregistrer(
        DefinitionFournisseur(nom="algolia_hn", fabrique=lambda: FournisseurAlgoliaHN(engine), actif_par_defaut=True)
    )
    registre.enregistrer(
        # Sous-étape 3.11, point 5 : désactivé par défaut tant que l'API
        # officielle Reddit n'est pas en place -- RADAR_ENQUETEUR_ACTIF_REDDIT=1
        # le réactive explicitement (mécanisme déjà posé en 3.1).
        DefinitionFournisseur(nom="reddit", fabrique=lambda: FournisseurReddit(engine), actif_par_defaut=False)
    )
    registre.enregistrer(
        DefinitionFournisseur(
            nom="magasin_interne",
            fabrique=lambda: FournisseurMagasinInterne(engine),
            actif_par_defaut=True,
        )
    )
    return registre
