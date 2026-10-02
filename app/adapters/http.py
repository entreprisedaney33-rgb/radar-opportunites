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
# Sous-étape 3.10 (AMELIORATIONS.md) : 6.0 -> 12.0 -- l'audit du 26/09/2026
# (rapports/AUDIT_NUIT_2026-09-26.md, §7) a mesuré que ce délai ne tenait pas
# de façon fiable en conditions réelles (5,3 % des intervalles sous 6 s,
# minimum observé 0,20 s -- plusieurs chemins de code appelant Reddit
# indépendamment, chacun avec son propre suivi ponctuel), avec un taux de 429
# de 63,6 % toutes voies Reddit confondues ce jour-là. Doubler la marge ne
# corrige pas la cause (déjà partagée entre tous les appelants via ce même
# dict, voir la docstring de module), mais réduit le risque résiduel.
DELAIS_MIN_PAR_HOTE_SECONDES: dict[str, float] = {
    "www.reddit.com": 12.0,
    "reddit.com": 12.0,
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
    """`type_erreur` (V2.8b) : posé par `get_with_retry` sur l'échec final -- "timeout", "erreur_reseau" (avec leur détail
    "timeout:delai_connexion", "erreur_reseau:refus"... quand l'appelant le demande), "http_<code>", ou None (erreur hors HTTP)."""
    type_erreur: str | None = None


# V2.8b (RADAR-V2.md) : diagnostic Render -> SIRENE. Le journal ne disait qu'« erreur_reseau » ; ce classement dit laquelle.
TYPES_ERREUR_RESEAU = ("dns", "tls", "delai_connexion", "delai_lecture", "refus", "reinitialisation", "proxy", "autre")


def classer_erreur_reseau(exc: BaseException) -> str:
    """Type exact d'un échec de connexion (un des `TYPES_ERREUR_RESEAU`), en remontant la chaîne d'exceptions de
    requests -> urllib3 -> socket/ssl. Attention : dans urllib3 2.x, `NewConnectionError` HÉRITE de `ConnectTimeoutError`
    (compatibilité) : un délai de connexion n'est donc reconnu que par son type EXACT, jamais par `isinstance`."""
    import socket
    import ssl

    from urllib3 import exceptions as u3

    if isinstance(exc, requests.exceptions.ProxyError):
        return "proxy"
    if isinstance(exc, requests.exceptions.SSLError):
        return "tls"
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return "delai_connexion"
    if isinstance(exc, requests.exceptions.ReadTimeout):
        return "delai_lecture"
    vus: set[int] = set()
    pile: list[BaseException | None] = [exc]
    while pile:
        e = pile.pop()
        if e is None or id(e) in vus:
            continue
        vus.add(id(e))
        if isinstance(e, (socket.gaierror, u3.NameResolutionError)):
            return "dns"
        if isinstance(e, (ssl.SSLError, u3.SSLError)):
            return "tls"
        if isinstance(e, u3.ProxyError):
            return "proxy"
        if isinstance(e, ConnectionRefusedError):
            return "refus"
        if isinstance(e, ConnectionResetError):
            return "reinitialisation"
        if type(e) is u3.ConnectTimeoutError or isinstance(e, socket.timeout) and not isinstance(e, u3.HTTPError):
            return "delai_connexion"
        if isinstance(e, u3.ReadTimeoutError):
            return "delai_lecture"
        pile.extend([e.__cause__, e.__context__, getattr(e, "reason", None)])
        pile.extend(a for a in getattr(e, "args", ()) if isinstance(a, BaseException))
    # Dernier recours : le texte (certains environnements ne gardent pas la chaîne d'exceptions)
    texte = str(exc).lower()
    for motifs, type_ in ((("name or service not known", "failed to resolve", "nodename nor servname", "temporary failure in name resolution",
                            "getaddrinfo"), "dns"),
                          (("certificate", "ssl", "tls"), "tls"),
                          (("connection refused", "errno 111", "errno 61"), "refus"),
                          (("connection reset", "reset by peer", "errno 104", "errno 54"), "reinitialisation"),
                          (("connect timeout", "timed out"), "delai_connexion")):
        if any(m in texte for m in motifs):
            return type_
    return "autre"


class PageTropGrande(ErreurCollecte):
    """Levée par `get_avec_limite_taille` quand le corps dépasse `max_octets`
    — sous-classe d'`ErreurCollecte` pour qu'un appelant qui ne s'intéresse
    qu'à « ça a échoué » puisse continuer à attraper une seule exception,
    tout en gardant la possibilité de distinguer ce cas précis (sous-étape
    3.3 d'AMELIORATIONS.md, garde-fou « taille maximale de page »)."""


class TropDeRequetes(ErreurCollecte):
    """Sous-étape 3.10 : levée par `get_with_retry` quand TOUTES les tentatives
    ont reçu un 429 (jamais pour un timeout ou une autre erreur) -- même
    principe que `PageTropGrande` : sous-classe d'`ErreurCollecte` pour qu'un
    appelant qui attrape seulement `ErreurCollecte` continue de fonctionner
    à l'identique, tout en donnant à un appelant qui s'en soucie (la
    recherche Reddit du Scout, `app.adapters.reddit_recherche`) le moyen de
    distinguer « Reddit nous rejette » d'un problème réseau générique --
    sert le disjoncteur par passage de `app.pipeline.orchestrator._collecter`."""


def get_with_retry(
    url: str, *, max_retries: int = 3, base_delay: float = 2.0, timeout: float = 10.0,
    headers: dict[str, str] | None = None,
    engine: Engine | None = None, contexte: str | None = None, detailler_erreur_reseau: bool = False,
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
    absence (timeout, erreur réseau), le type d'échec.

    `detailler_erreur_reseau` (V2.8b, False par défaut : comportement inchangé pour tous les appelants existants) : le type
    d'échec journalisé porte le détail de `classer_erreur_reseau` ("erreur_reseau:dns", "timeout:delai_connexion"...), aussi
    écrit dans le log et dans le message de l'exception finale."""
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
            type_erreur = f"timeout:{classer_erreur_reseau(exc)}" if detailler_erreur_reseau else "timeout"
            dernier_code = None
            if detailler_erreur_reseau:
                logger.warning("Échec réseau [%s] pour %s (tentative %d/%d) : %s", type_erreur, hote, tentative, max_retries, exc)
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
            type_erreur = f"erreur_reseau:{classer_erreur_reseau(exc)}" if detailler_erreur_reseau else "erreur_reseau"
            dernier_code = None
            if detailler_erreur_reseau:
                logger.warning("Échec réseau [%s] pour %s (tentative %d/%d) : %s", type_erreur, hote, tentative, max_retries, exc)
            if tentative < max_retries:
                time.sleep(base_delay * (2 ** (tentative - 1)))
    _journaliser_appel_http(engine, contexte, hote, dernier_code, type_erreur, time.monotonic() - debut)
    if dernier_code == 429:
        # Sous-étape 3.10 : les `max_retries` tentatives ont TOUTES reçu un
        # 429 (jamais mélangé avec un timeout/une autre erreur, sinon
        # `dernier_code` porterait la trace de cette dernière tentative-là) --
        # exception dédiée pour que l'appelant puisse réagir spécifiquement
        # (voir `TropDeRequetes`), plutôt qu'une `ErreurCollecte` générique.
        erreur_finale: ErreurCollecte = TropDeRequetes(f"429 persistant après {max_retries} tentatives pour {url}")
        erreur_finale.type_erreur = "http_429"
        raise erreur_finale
    prefixe = f"[{type_erreur}] " if detailler_erreur_reseau and type_erreur else ""
    erreur_finale = ErreurCollecte(f"{prefixe}Échec après {max_retries} tentatives pour {url}: {derniere_erreur}")
    erreur_finale.type_erreur = type_erreur or (f"http_{dernier_code}" if dernier_code is not None else None)
    raise erreur_finale


def post_formulaire_with_retry(
    url: str, data: dict[str, str], *, max_retries: int = 3, base_delay: float = 2.0, timeout: float = 10.0,
    engine: Engine | None = None, contexte: str | None = None,
) -> requests.Response:
    """V2.3 : POST d'un formulaire (`application/x-www-form-urlencoded`), pour les jetons OAuth
    `client_credentials` (France Travail). Même espacement par hôte, même backoff sur 429/5xx et même
    journalisation `journal_http` que `get_with_retry` ; le CORPS de la requête (`client_secret`) n'est
    JAMAIS journalisé ni mis dans un message d'erreur -- seulement l'hôte et le code HTTP."""
    hote = urlsplit(url).netloc
    debut = time.monotonic()
    dernier_code: int | None = None
    type_erreur: str | None = None
    derniere_erreur: Exception | None = None
    for tentative in range(1, max_retries + 1):
        try:
            _attendre_espacement_hote(url)
            resp = requests.post(url, data=data, timeout=timeout, headers={"User-Agent": USER_AGENT})
            dernier_code = resp.status_code
            if resp.status_code == 429:
                time.sleep(base_delay * (2 ** (tentative - 1)))
                continue
            resp.raise_for_status()
            _journaliser_appel_http(engine, contexte, hote, dernier_code, None, time.monotonic() - debut)
            return resp
        except requests.Timeout as exc:
            derniere_erreur, type_erreur, dernier_code = exc, "timeout", None
        except requests.HTTPError as exc:
            derniere_erreur = exc
        except requests.RequestException as exc:
            derniere_erreur, type_erreur, dernier_code = exc, "erreur_reseau", None
        if tentative < max_retries:
            time.sleep(base_delay * (2 ** (tentative - 1)))
    _journaliser_appel_http(engine, contexte, hote, dernier_code, type_erreur, time.monotonic() - debut)
    if dernier_code == 429:
        raise TropDeRequetes(f"429 persistant après {max_retries} tentatives pour {hote}")
    # Message volontairement sans URL complète ni détail de la requête : le corps contient un secret.
    raise ErreurCollecte(f"Échec après {max_retries} tentatives pour {hote} (code {dernier_code or type_erreur})")


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
