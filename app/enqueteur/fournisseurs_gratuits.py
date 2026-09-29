"""Fournisseurs gratuits de l'Enquêteur (sous-étape 3.2 d'AMELIORATIONS.md) :
Algolia HN, Reddit, magasin interne. Les trois implémentent
`FournisseurRecherche` (`app.enqueteur.fournisseurs`) — appelés par le
pipeline réel depuis la sous-étape 3.4
(`app.pipeline.orchestrator._phase_enquete`), chacun activable/désactivable
via `RADAR_ENQUETEUR_ACTIF_<NOM>`
(`app.enqueteur.fournisseurs.RegistreFournisseurs.est_actif`). Dans la suite
de tests par défaut, Algolia HN et Reddit sont désactivés par un réglage
global (`tests/conftest.py`, garde-fou §0.2.5 : aucun réseau).

- Algolia HN réutilise le client HTTP et le gabarit d'URL du connecteur 1.3
  (`app.adapters.hn_recherche`) pour une REQUÊTE LIBRE générée par
  `app.enqueteur.gabarits` pour UNE opportunité donnée.
- Reddit (sous-étape 4.0) passe par l'API officielle (`app.adapters.reddit_api`),
  recherche site entier ; actif par défaut uniquement si les identifiants
  d'environnement sont présents. Disjoncteur 429 (sous-étape 3.11,
  `app.enqueteur.disjoncteur`) conservé, état persisté en base (absent si
  `engine` est `None`).
- Le magasin interne ne fait AUCUNE requête réseau : il compare la requête
  aux items `signal_concurrence` déjà stockés (sous-étape 1.1) avec la même
  mesure de similarité que le dédoublonnage
  (`app.pipeline.dedupe.similarite_lexicale`).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from urllib.parse import quote

from sqlalchemy.engine import Engine

from app import config as cfg
from app.adapters import reddit_api
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
    """Sous-étape 4.0 : recherche Reddit SITE ENTIER par l'API officielle
    (`app.adapters.reddit_api`, `oauth.reddit.com/search`, `type=link`), plus
    jamais par le flux Atom `search.rss`. L'Enquêteur enquête sur une
    opportunité donnée, pas sur un subreddit fixé à l'avance. Sans les
    identifiants d'environnement, le fournisseur n'est pas actif (voir
    `construire_registre_fournisseurs_gratuits`) et, s'il est appelé quand
    même, renvoie `[]` sans aucune requête."""

    nom = "reddit"
    sans_reseau = False  # vrai appel réseau -- consomme max_requetes_recherche_par_jour

    def __init__(self, engine: Engine | None = None):
        """`engine` : journalisation `journal_http` et disjoncteur (3.11) --
        `None` (tests unitaires directs) : ni l'un ni l'autre."""
        self.engine = engine

    def rechercher(self, requete: str, limite: int) -> list[ResultatRecherche]:
        client = reddit_api.obtenir_client()
        if client is None:
            return []
        if self.engine is not None:
            etat = _lire_etat_disjoncteur(self.engine, disjoncteur.NOM_REDDIT)
            if disjoncteur.est_en_pause(etat, datetime.now(timezone.utc)):
                logger.info(
                    "Enquêteur Reddit : disjoncteur en pause jusqu'à %s -- requête %r non tentée.",
                    etat.pause_jusqu_a, requete,
                )
                return []

        try:
            listing = client.get_json(
                "/search", {"q": requete, "sort": "relevance", "type": "link", "limit": max(1, min(limite, 100))},
                engine=self.engine, contexte="reddit_api:enqueteur",
            )
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

        signaux = reddit_api.signaux_depuis_listing(
            listing, domaine="reddit.com", droits="API officielle Reddit (OAuth application), /search, usage interne",
            flux_origine="reddit", requete_origine=requete, limite=limite,
        )
        return [
            ResultatRecherche(
                url=s.url,
                titre=s.texte.split(" — ", 1)[0],
                extrait=s.texte,
                horodatage_source=s.date_publication,
                fournisseur=self.nom,
            )
            for s in signaux
        ]


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
        # Sous-étape 4.0 : réactivé par défaut, via l'API officielle -- mais
        # seulement si les identifiants d'environnement sont présents (sans
        # eux, jamais actif : aucune requête gaspillée ni compteur consommé).
        # RADAR_ENQUETEUR_ACTIF_REDDIT=0/1 garde la priorité (mécanisme 3.1).
        DefinitionFournisseur(
            nom="reddit", fabrique=lambda: FournisseurReddit(engine), actif_par_defaut=reddit_api.configuree(),
        )
    )
    registre.enregistrer(
        DefinitionFournisseur(
            nom="magasin_interne",
            fabrique=lambda: FournisseurMagasinInterne(engine),
            actif_par_defaut=True,
        )
    )
    return registre
