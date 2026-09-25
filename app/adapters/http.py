"""Appel HTTP avec timeout, retry plafonné et backoff — utilisé par tous les
adaptateurs réseau, jamais d'appel direct à `requests` ailleurs.

Sous-étape 3.6 (AMELIORATIONS.md), point 4 : espacement PROACTIF par hôte, en
plus du backoff réactif sur 429/5xx (inchangé, voir plus bas) — le limiteur
ATTEND, il ne rejette jamais. Un seul état en mémoire du PROCESSUS (pas par
run, pas en base : le radar tourne dans un seul Background Worker continu,
voir render.yaml), partagé par TOUTE l'application — la collecte du Scout
(`app.adapters.rss_adapter`/`reddit_recherche`/`hn_recherche`) et l'Enquêteur
(`app.enqueteur.fournisseurs_gratuits`, `app.enqueteur.fetch`) passent tous
par `get_with_retry`/`get_avec_limite_taille` ci-dessous, donc par le MÊME
compteur par hôte : deux appels vers `www.reddit.com`, peu importe lequel des
deux sous-systèmes les déclenche, sont toujours espacés d'au moins
`DELAIS_MIN_PAR_HOTE_SECONDES["www.reddit.com"]`. Un hôte différent n'attend
jamais à cause d'un autre : chaque hôte a son propre compteur indépendant.

Sous-étape 3.7 : `engine`/`contexte`, optionnels sur les deux fonctions
ci-dessous -- quand les deux sont fournis, l'issue de l'appel (code HTTP, ou
"timeout"/"erreur_reseau" en son absence, et durée totale) est journalisée
dans `journal_http` (`app/storage/repo.py::enregistrer_appel_http`), jamais
le contenu de la page ni l'URL complète. Sans eux (comportement par défaut,
inchangé) : aucune écriture en base -- c'est le cas de tout appel direct dans
un test unitaire sans base. Une panne de la journalisation elle-même (base
injoignable) n'affecte JAMAIS l'appel HTTP lui-même : elle est absorbée,
journalisée en `logger`, rien de plus -- l'observabilité ne doit jamais
devenir un nouveau point de panne de la collecte."""
from __future__ import annotations

import logging
import time
from urllib.parse import urlsplit

import requests
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)


USER_AGENT = "radar-opportunites-ia/0.1 (labo-ia)"

# Délais minimaux PROACTIFS entre deux appels vers le même hôte (secondes).
# reddit.com et www.reddit.com : mêmes précautions de débit que www.reddit.com,
# au cas où une URL relative ou un futur gabarit omettrait le "www.".
DELAIS_MIN_PAR_HOTE_SECONDES: dict[str, float] = {
    "www.reddit.com": 6.0,
    "reddit.com": 6.0,
    "hn.algolia.com": 1.0,
}
# Tout autre hôte (notamment les domaines arbitraires fetchés par
# l'Enquêteur : résultats de recherche, pages de tarification, robots.txt de
# ces domaines) — valeur de départ, non mesurée en conditions réelles.
DELAI_MIN_PAR_DEFAUT_SECONDES = 2.0

# État du processus : dernier appel RÉEL (juste avant `requests.get`) par
# hôte, en secondes `time.monotonic()` — jamais persisté, jamais partagé
# entre processus (un seul Background Worker tourne, voir render.yaml).
_dernier_appel_par_hote: dict[str, float] = {}


def _attendre_espacement_hote(url: str) -> None:
    """Bloque jusqu'à ce que le délai minimal de l'hôte de `url` soit écoulé
    depuis le dernier appel réel vers ce même hôte — jamais de rejet, jamais
    d'exception. Premier appel vers un hôte : aucune attente."""
    hote = urlsplit(url).netloc
    delai_min = DELAIS_MIN_PAR_HOTE_SECONDES.get(hote, DELAI_MIN_PAR_DEFAUT_SECONDES)
    dernier = _dernier_appel_par_hote.get(hote)
    maintenant = time.monotonic()
    if dernier is not None:
        attente = delai_min - (maintenant - dernier)
        if attente > 0:
            time.sleep(attente)
            maintenant = time.monotonic()
    _dernier_appel_par_hote[hote] = maintenant


def _journaliser_appel_http(
    engine: Engine | None, contexte: str | None, hote: str, code_http: int | None,
    erreur: str | None, duree_secondes: float,
) -> None:
    """Sous-étape 3.7 : no-op sans `engine`/`contexte` (aucun des deux
    n'implique l'autre en pratique -- voir la docstring de module). Import
    différé de `app.storage.repo` pour ne pas alourdir le chargement de ce
    module dans les nombreux appelants qui ne journalisent rien."""
    if engine is None or contexte is None:
        return
    try:
        from app.storage import repo

        repo.enregistrer_appel_http(
            engine, hote=hote, flux_ou_fournisseur=contexte, code_http=code_http,
            erreur=erreur, duree_ms=round(duree_secondes * 1000, 1),
        )
    except Exception:
        logger.warning("Journalisation HTTP échouée pour %s (%s) -- appel non affecté.", contexte, hote, exc_info=True)


class ErreurCollecte(Exception):
    pass


class PageTropGrande(ErreurCollecte):
    """Levée par `get_avec_limite_taille` quand le corps dépasse `max_octets`
    — sous-classe d'`ErreurCollecte` pour qu'un appelant qui ne s'intéresse
    qu'à « ça a échoué » puisse continuer à attraper une seule exception,
    tout en gardant la possibilité de distinguer ce cas précis (sous-étape
    3.3 d'AMELIORATIONS.md, garde-fou « taille maximale de page »)."""


def get_with_retry(
    url: str, *, max_retries: int = 3, base_delay: float = 2.0, timeout: float = 10.0,
    headers: dict[str, str] | None = None,
    engine: Engine | None = None, contexte: str | None = None,
) -> requests.Response:
    """`headers` (sous-étape 3.5 d'AMELIORATIONS.md) : en-têtes supplémentaires
    fusionnés avec `User-Agent` -- nécessaire pour un fournisseur qui
    s'authentifie par en-tête (ex. `X-Subscription-Token` d'un moteur de
    recherche payant, `app.enqueteur.fournisseur_payant`). `None` par défaut,
    comportement strictement inchangé pour tout appelant existant.

    `engine`/`contexte` (sous-étape 3.7) : voir la docstring de module --
    journalise une seule ligne dans `journal_http` pour CET appel logique
    (tentatives et backoff compris), avec le dernier code HTTP obtenu (429
    compris, s'il persiste jusqu'à épuisement des tentatives) ou, en son
    absence (timeout, erreur réseau), le type d'échec."""
    en_tete = {"User-Agent": USER_AGENT}
    if headers:
        en_tete.update(headers)
    hote = urlsplit(url).netloc
    debut = time.monotonic()
    dernier_code: int | None = None
    type_erreur: str | None = None
    derniere_erreur: Exception | None = None
    for tentative in range(1, max_retries + 1):
        try:
            _attendre_espacement_hote(url)
            resp = requests.get(url, timeout=timeout, headers=en_tete)
            dernier_code = resp.status_code
            if resp.status_code == 429:
                delai = base_delay * (2 ** (tentative - 1))
                logger.warning("429 reçu pour %s, backoff %.1fs (tentative %d/%d)", url, delai, tentative, max_retries)
                time.sleep(delai)
                continue
            resp.raise_for_status()
            _journaliser_appel_http(engine, contexte, hote, dernier_code, None, time.monotonic() - debut)
            return resp
        except requests.Timeout as exc:
            derniere_erreur = exc
            type_erreur = "timeout"
            dernier_code = None
            if tentative < max_retries:
                time.sleep(base_delay * (2 ** (tentative - 1)))
        except requests.HTTPError as exc:
            # `dernier_code` déjà posé ci-dessus (avant `raise_for_status`) :
            # un vrai code HTTP a été reçu, ce n'est pas une "erreur réseau".
            derniere_erreur = exc
            if tentative < max_retries:
                time.sleep(base_delay * (2 ** (tentative - 1)))
        except requests.RequestException as exc:
            derniere_erreur = exc
            type_erreur = "erreur_reseau"
            dernier_code = None
            if tentative < max_retries:
                time.sleep(base_delay * (2 ** (tentative - 1)))
    _journaliser_appel_http(engine, contexte, hote, dernier_code, type_erreur, time.monotonic() - debut)
    raise ErreurCollecte(f"Échec après {max_retries} tentatives pour {url}: {derniere_erreur}")


def get_avec_limite_taille(
    url: str, *, max_octets: int, max_retries: int = 3, base_delay: float = 2.0, timeout: float = 10.0,
    engine: Engine | None = None, contexte: str | None = None,
) -> bytes:
    """Comme `get_with_retry`, mais en flux (`stream=True`) : la lecture
    s'arrête et lève `PageTropGrande` dès que le corps dépasse `max_octets`,
    sans jamais charger la page entière en mémoire — protège contre une page
    piège de taille excessive (sous-étape 3.3 d'AMELIORATIONS.md, garde-fou
    « taille maximale de page »). Mêmes conditions d'échec que
    `get_with_retry` sinon (429 avec backoff, retry plafonné sur les autres
    erreurs). `engine`/`contexte` (sous-étape 3.7) : même journalisation que
    `get_with_retry` -- une page coupée pour dépassement de taille journalise
    le code HTTP réellement reçu (`raise_for_status` avait déjà réussi),
    jamais une "erreur réseau"."""
    hote = urlsplit(url).netloc
    debut = time.monotonic()
    dernier_code: int | None = None
    type_erreur: str | None = None
    derniere_erreur: Exception | None = None
    for tentative in range(1, max_retries + 1):
        try:
            _attendre_espacement_hote(url)
            resp = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT}, stream=True)
            dernier_code = resp.status_code
            if resp.status_code == 429:
                delai = base_delay * (2 ** (tentative - 1))
                logger.warning("429 reçu pour %s, backoff %.1fs (tentative %d/%d)", url, delai, tentative, max_retries)
                resp.close()
                time.sleep(delai)
                continue
            resp.raise_for_status()
            morceaux: list[bytes] = []
            taille = 0
            for morceau in resp.iter_content(chunk_size=8192):
                taille += len(morceau)
                if taille > max_octets:
                    resp.close()
                    _journaliser_appel_http(engine, contexte, hote, dernier_code, None, time.monotonic() - debut)
                    raise PageTropGrande(f"Page au-delà de {max_octets} octets : {url}")
                morceaux.append(morceau)
            _journaliser_appel_http(engine, contexte, hote, dernier_code, None, time.monotonic() - debut)
            return b"".join(morceaux)
        except requests.Timeout as exc:
            derniere_erreur = exc
            type_erreur = "timeout"
            dernier_code = None
            if tentative < max_retries:
                time.sleep(base_delay * (2 ** (tentative - 1)))
        except requests.HTTPError as exc:
            derniere_erreur = exc
            if tentative < max_retries:
                time.sleep(base_delay * (2 ** (tentative - 1)))
        except requests.RequestException as exc:
            derniere_erreur = exc
            type_erreur = "erreur_reseau"
            dernier_code = None
            if tentative < max_retries:
                time.sleep(base_delay * (2 ** (tentative - 1)))
    _journaliser_appel_http(engine, contexte, hote, dernier_code, type_erreur, time.monotonic() - debut)
    raise ErreurCollecte(f"Échec après {max_retries} tentatives pour {url}: {derniere_erreur}")
