"""Client de l'API officielle Reddit (sous-étape 4.0 d'AMELIORATIONS.md).

Remplace définitivement les flux RSS/Atom de Reddit (`.rss`, `search.rss`) :
plus aucun code du radar n'interroge Reddit autrement que par ce client.

- Authentification : OAuth « application seule » (`client_credentials`) —
  jeton demandé sur `https://www.reddit.com/api/v1/access_token`
  (authentification HTTP Basic client_id:client_secret), appels de données sur
  `https://oauth.reddit.com`. Le jeton est renouvelé AVANT son expiration
  (marge `MARGE_RENOUVELLEMENT_JETON_SECONDES`) ; un 401 en cours de route
  force un renouvellement puis une seule nouvelle tentative.
- Identifiants : UNIQUEMENT dans les variables d'environnement
  `RADAR_REDDIT_CLIENT_ID`, `RADAR_REDDIT_CLIENT_SECRET`,
  `RADAR_REDDIT_USER_AGENT` (jamais dans le code, un fichier versionné ou un
  test — le dépôt de déploiement est public). Sans elles, l'API est
  INACTIVE : `obtenir_client()` renvoie `None` et le dit une fois dans les
  logs ; aucun appel n'est jamais tenté.
- Débit : plafond volontaire de `PLAFOND_REQUETES_PAR_MINUTE` (30, bien
  sous les 100/min du palier gratuit) sur une fenêtre glissante d'une
  minute, PLUS le respect des en-têtes `X-Ratelimit-Remaining` /
  `X-Ratelimit-Reset` renvoyés par Reddit : quand il ne reste plus rien, on
  attend le renouvellement de la fenêtre au lieu de forcer. Un 429 attend
  (`Retry-After` ou `X-Ratelimit-Reset`, plafonné) puis réessaie une fois ;
  s'il persiste : `TropDeRequetes`, comme avant (le disjoncteur par passage
  de l'orchestrateur et celui de l'Enquêteur continuent de fonctionner).
- Journalisation : chaque appel écrit une ligne dans `journal_http` avec un
  contexte préfixé `reddit_api:` (`reddit_api:jeton`, `reddit_api:new:<sub>`,
  `reddit_api:recherche:<sub>:<expression>`, `reddit_api:enqueteur`), ce qui
  distingue l'API des anciens flux (`app.metriques`, bloc `reddit_api`).
- Un contenu supprimé ou retiré (`[removed]`, `[deleted]`,
  `removed_by_category`) n'est jamais transformé en signal.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Callable

import requests
from sqlalchemy.engine import Engine

from app.adapters.base import SignalBrut
from app.adapters.http import ErreurCollecte, TropDeRequetes, _journaliser_appel_http

logger = logging.getLogger(__name__)

URL_JETON = "https://www.reddit.com/api/v1/access_token"
URL_BASE_OAUTH = "https://oauth.reddit.com"
URL_PUBLIQUE = "https://www.reddit.com"
HOTE_JETON = "www.reddit.com"
HOTE_OAUTH = "oauth.reddit.com"

PLAFOND_REQUETES_PAR_MINUTE = 30
MARGE_RENOUVELLEMENT_JETON_SECONDES = 120
ATTENTE_429_MAX_SECONDES = 60.0
TIMEOUT_SECONDES = 15.0
LONGUEUR_MAX_TEXTE = 3000  # caractères de `selftext` gardés dans un signal

VARIABLES_ENV = ("RADAR_REDDIT_CLIENT_ID", "RADAR_REDDIT_CLIENT_SECRET", "RADAR_REDDIT_USER_AGENT")


class ClientRedditAPI:
    """Un seul exemplaire par processus (voir `obtenir_client`) : le jeton et
    la fenêtre de débit sont partagés entre la collecte du Scout et
    l'Enquêteur, exactement comme l'espacement par hôte de
    `app.adapters.http`. `http`, `horloge`, `dormir` et `maintenant` sont
    injectables pour les tests (aucun réseau, aucune vraie attente)."""

    def __init__(
        self, client_id: str, client_secret: str, user_agent: str, *,
        http=requests, horloge: Callable[[], float] = time.monotonic,
        dormir: Callable[[float], None] = time.sleep,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self.user_agent = user_agent
        self._http = http
        self._horloge = horloge
        self._dormir = dormir
        self._jeton: str | None = None
        self._jeton_expire_a: float = 0.0  # horloge monotone
        self._appels_recents: deque[float] = deque()
        self._bloque_jusqu_a: float = 0.0  # X-Ratelimit : plus rien avant cet instant
        self._verrou = threading.Lock()

    # -- jeton ---------------------------------------------------------------
    def _jeton_valide(self) -> bool:
        return self._jeton is not None and self._horloge() < self._jeton_expire_a - MARGE_RENOUVELLEMENT_JETON_SECONDES

    def _renouveler_jeton(self, engine: Engine | None) -> None:
        debut = self._horloge()
        self._attendre_creneau()
        try:
            resp = self._http.post(
                URL_JETON, auth=(self._client_id, self._client_secret),
                data={"grant_type": "client_credentials"},
                headers={"User-Agent": self.user_agent}, timeout=TIMEOUT_SECONDES,
            )
        except requests.RequestException as exc:
            _journaliser_appel_http(engine, "reddit_api:jeton", HOTE_JETON, None, "erreur_reseau", self._horloge() - debut)
            raise ErreurCollecte(f"Jeton Reddit : erreur réseau ({type(exc).__name__}).") from exc
        _journaliser_appel_http(engine, "reddit_api:jeton", HOTE_JETON, resp.status_code, None, self._horloge() - debut)
        if resp.status_code != 200:
            raise ErreurCollecte(f"Jeton Reddit refusé (HTTP {resp.status_code}).")
        try:
            corps = resp.json()
            jeton = corps["access_token"]
            duree = float(corps.get("expires_in", 3600))
        except (ValueError, KeyError, TypeError) as exc:
            raise ErreurCollecte("Jeton Reddit : réponse illisible.") from exc
        self._jeton = jeton
        self._jeton_expire_a = self._horloge() + duree

    # -- débit ---------------------------------------------------------------
    def _attendre_creneau(self) -> None:
        """Bloque (jamais d'exception) jusqu'à ce qu'une requête soit permise :
        fenêtre glissante de 60 s à `PLAFOND_REQUETES_PAR_MINUTE`, et pause
        demandée par les en-têtes X-Ratelimit."""
        maintenant = self._horloge()
        if self._bloque_jusqu_a > maintenant:
            self._dormir(self._bloque_jusqu_a - maintenant)
            maintenant = self._horloge()
        while self._appels_recents and maintenant - self._appels_recents[0] >= 60.0:
            self._appels_recents.popleft()
        if len(self._appels_recents) >= PLAFOND_REQUETES_PAR_MINUTE:
            attente = 60.0 - (maintenant - self._appels_recents[0])
            if attente > 0:
                self._dormir(attente)
                maintenant = self._horloge()
            while self._appels_recents and maintenant - self._appels_recents[0] >= 60.0:
                self._appels_recents.popleft()
        self._appels_recents.append(maintenant)

    def _lire_en_tetes_debit(self, resp) -> float | None:
        """Renvoie le délai (s) avant le renouvellement de la fenêtre Reddit
        si l'on est à court de requêtes, sinon `None`. Pose au passage
        `_bloque_jusqu_a` quand `X-Ratelimit-Remaining` est épuisé."""
        try:
            restant = float(resp.headers.get("X-Ratelimit-Remaining"))
            reset = float(resp.headers.get("X-Ratelimit-Reset"))
        except (TypeError, ValueError):
            return None
        if restant < 1:
            self._bloque_jusqu_a = self._horloge() + max(reset, 0.0)
            return reset
        return None

    # -- appel de données ----------------------------------------------------
    def get_json(self, chemin: str, params: dict, *, engine: Engine | None = None, contexte: str) -> dict:
        """GET sur `oauth.reddit.com<chemin>` ; renvoie le JSON décodé.
        `TropDeRequetes` si 429 persistant ; `ErreurCollecte` sinon."""
        with self._verrou:
            return self._get_json(chemin, params, engine, contexte)

    def _get_json(self, chemin: str, params: dict, engine: Engine | None, contexte: str) -> dict:
        jeton_renouvele_sur_401 = False
        tentatives_429 = 0
        while True:
            if not self._jeton_valide():
                self._renouveler_jeton(engine)
            debut = self._horloge()
            self._attendre_creneau()
            try:
                resp = self._http.get(
                    URL_BASE_OAUTH + chemin, params={**params, "raw_json": 1},
                    headers={"Authorization": f"bearer {self._jeton}", "User-Agent": self.user_agent},
                    timeout=TIMEOUT_SECONDES,
                )
            except requests.Timeout as exc:
                _journaliser_appel_http(engine, contexte, HOTE_OAUTH, None, "timeout", self._horloge() - debut)
                raise ErreurCollecte(f"Reddit API : timeout ({contexte}).") from exc
            except requests.RequestException as exc:
                _journaliser_appel_http(engine, contexte, HOTE_OAUTH, None, "erreur_reseau", self._horloge() - debut)
                raise ErreurCollecte(f"Reddit API : erreur réseau ({contexte}).") from exc

            attente_fenetre = self._lire_en_tetes_debit(resp)
            code = resp.status_code
            _journaliser_appel_http(engine, contexte, HOTE_OAUTH, code, None, self._horloge() - debut)

            if code == 401 and not jeton_renouvele_sur_401:
                jeton_renouvele_sur_401 = True
                self._jeton = None
                continue
            if code == 429:
                if tentatives_429 >= 1:
                    raise TropDeRequetes(f"Reddit API : 429 persistant ({contexte}).")
                tentatives_429 += 1
                try:
                    attente = float(resp.headers.get("Retry-After") or attente_fenetre or 10.0)
                except (TypeError, ValueError):
                    attente = 10.0
                attente = min(max(attente, 1.0), ATTENTE_429_MAX_SECONDES)
                logger.warning("Reddit API : 429 (%s), attente %.0f s avant une seule nouvelle tentative.", contexte, attente)
                self._dormir(attente)
                continue
            if code != 200:
                raise ErreurCollecte(f"Reddit API : HTTP {code} ({contexte}).")
            try:
                return resp.json()
            except ValueError as exc:
                raise ErreurCollecte(f"Reddit API : réponse non JSON ({contexte}).") from exc


# -- accès au client unique -------------------------------------------------
_client: ClientRedditAPI | None = None
_cle_client: tuple | None = None
_inactivite_deja_signalee = False


def _identifiants() -> tuple[str, str, str] | None:
    valeurs = tuple((os.environ.get(v) or "").strip() for v in VARIABLES_ENV)
    if not all(valeurs):
        return None
    return valeurs  # type: ignore[return-value]


def configuree() -> bool:
    """Vrai si les trois variables d'environnement sont renseignées."""
    return _identifiants() is not None


def obtenir_client() -> ClientRedditAPI | None:
    """Le client partagé du processus, ou `None` (API inactive : dit UNE fois
    dans les logs, par processus, quelles variables manquent — jamais leurs
    valeurs)."""
    global _client, _cle_client, _inactivite_deja_signalee
    ids = _identifiants()
    if ids is None:
        if not _inactivite_deja_signalee:
            manquantes = [v for v in VARIABLES_ENV if not (os.environ.get(v) or "").strip()]
            logger.warning(
                "API Reddit INACTIVE : variable(s) d'environnement absente(s) %s -- aucune collecte ni "
                "recherche Reddit ce passage (les flux RSS de Reddit ne sont plus jamais utilisés).",
                ", ".join(manquantes),
            )
            _inactivite_deja_signalee = True
        return None
    if "(by /u/" not in ids[2]:
        logger.warning("RADAR_REDDIT_USER_AGENT devrait finir par « (by /u/<pseudo>) » (règle de Reddit).")
    if _client is None or _cle_client != ids:
        _client = ClientRedditAPI(*ids)
        _cle_client = ids
    return _client


def reinitialiser_pour_tests() -> None:
    global _client, _cle_client, _inactivite_deja_signalee
    _client = None
    _cle_client = None
    _inactivite_deja_signalee = False


# -- conversion d'une liste de posts en signaux ------------------------------
def _publication_utilisable(donnees: dict) -> bool:
    if donnees.get("removed_by_category") or donnees.get("stickied"):
        return False
    corps = (donnees.get("selftext") or "").strip()
    return corps not in ("[removed]", "[deleted]") and bool((donnees.get("title") or "").strip())


def signaux_depuis_listing(
    json_listing: dict, *, domaine: str, droits: str, flux_origine: str, requete_origine: str | None, limite: int,
) -> list[SignalBrut]:
    signaux: list[SignalBrut] = []
    try:
        enfants = json_listing["data"]["children"]
    except (KeyError, TypeError):
        return []
    for enfant in enfants:
        if len(signaux) >= limite:
            break
        donnees = enfant.get("data") if isinstance(enfant, dict) else None
        if not isinstance(donnees, dict) or not _publication_utilisable(donnees):
            continue
        permalien = donnees.get("permalink")
        if not permalien:
            continue
        titre = donnees["title"].strip()
        corps = (donnees.get("selftext") or "").strip()[:LONGUEUR_MAX_TEXTE]
        date_publication = None
        if isinstance(donnees.get("created_utc"), (int, float)):
            date_publication = datetime.fromtimestamp(donnees["created_utc"], tz=timezone.utc)
        signaux.append(
            SignalBrut(
                url=URL_PUBLIQUE + permalien,
                domaine=domaine,
                texte=f"{titre} — {corps}".strip(" —"),
                date_publication=date_publication,
                type_source="reddit_api",
                droits_collecte=droits,
                flux_origine=flux_origine,
                type_flux="douleur",
                requete_origine=requete_origine,
            )
        )
    return signaux


class AdaptateurRedditNouveaux:
    """Remplace le flux RSS `/r/<sub>/.rss` : les derniers posts d'un
    subreddit (`/r/<sub>/new`)."""

    def __init__(self, id_source: str, nom: str, url_source: str):
        self.id_source = id_source
        self.nom = nom
        self.subreddit = url_source.split("/r/")[1].split("/")[0]

    def collecter(self, budget_appels: int, *, engine: Engine | None = None) -> list[SignalBrut]:
        client = obtenir_client()
        if client is None:
            return []
        try:
            listing = client.get_json(
                f"/r/{self.subreddit}/new", {"limit": max(1, min(budget_appels, 100))},
                engine=engine, contexte=f"reddit_api:new:{self.subreddit}",
            )
        except TropDeRequetes:
            raise
        except ErreurCollecte as exc:
            logger.warning("Reddit API %s indisponible : %s", self.id_source, exc)
            return []
        return signaux_depuis_listing(
            listing, domaine=f"Reddit r/{self.subreddit}",
            droits=f"API officielle Reddit (OAuth application), /r/{self.subreddit}/new, usage interne, lecture seule",
            flux_origine=self.nom, requete_origine=None, limite=budget_appels,
        )

