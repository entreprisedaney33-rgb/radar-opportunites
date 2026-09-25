"""Sous-étape 3.3 (AMELIORATIONS.md) : `get_avec_limite_taille` — comme
`get_with_retry`, mais en flux, sans jamais charger la page entière en
mémoire au-delà du plafond. Sans réseau (client HTTP simulé).

Sous-étape 3.5 : `get_with_retry` accepte désormais des `headers`
supplémentaires (fusionnés avec `User-Agent`) -- voir en bas de fichier."""
from __future__ import annotations

import pytest
import requests

from app.adapters import http as http_module
from app.adapters.http import ErreurCollecte, PageTropGrande, USER_AGENT, get_avec_limite_taille, get_with_retry


class _FauxReponseStream:
    def __init__(self, morceaux, status_code=200):
        self._morceaux = morceaux
        self.status_code = status_code
        self.fermee = False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        yield from self._morceaux

    def close(self):
        self.fermee = True


def test_get_avec_limite_taille_page_ok(monkeypatch):
    reponse = _FauxReponseStream([b"a" * 10, b"b" * 10])
    monkeypatch.setattr(http_module.requests, "get", lambda *a, **kw: reponse)

    contenu = get_avec_limite_taille("https://exemple.invalid/page", max_octets=1000)

    assert contenu == b"a" * 10 + b"b" * 10


def test_get_avec_limite_taille_page_exactement_a_la_limite_passe(monkeypatch):
    reponse = _FauxReponseStream([b"a" * 500, b"b" * 500])
    monkeypatch.setattr(http_module.requests, "get", lambda *a, **kw: reponse)

    contenu = get_avec_limite_taille("https://exemple.invalid/page", max_octets=1000)

    assert len(contenu) == 1000


def test_get_avec_limite_taille_page_trop_grosse(monkeypatch):
    reponse = _FauxReponseStream([b"x" * 600, b"y" * 600])
    monkeypatch.setattr(http_module.requests, "get", lambda *a, **kw: reponse)

    with pytest.raises(PageTropGrande):
        get_avec_limite_taille("https://exemple.invalid/page", max_octets=1000)
    assert reponse.fermee  # le flux est fermé dès le dépassement, pas de lecture jusqu'au bout


def test_get_avec_limite_taille_page_introuvable(monkeypatch):
    class _Reponse404:
        status_code = 404

        def raise_for_status(self):
            raise requests.HTTPError("404")

        def iter_content(self, chunk_size):
            return iter(())

        def close(self):
            pass

    monkeypatch.setattr(http_module.requests, "get", lambda *a, **kw: _Reponse404())
    monkeypatch.setattr(http_module.time, "sleep", lambda *_a, **_kw: None)

    with pytest.raises(ErreurCollecte):
        get_avec_limite_taille("https://exemple.invalid/page", max_octets=1000, max_retries=2)


def test_get_avec_limite_taille_429_persistant(monkeypatch):
    class _Reponse429:
        status_code = 429

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            return iter(())

        def close(self):
            pass

    monkeypatch.setattr(http_module.requests, "get", lambda *a, **kw: _Reponse429())
    monkeypatch.setattr(http_module.time, "sleep", lambda *_a, **_kw: None)

    with pytest.raises(ErreurCollecte):
        get_avec_limite_taille("https://exemple.invalid/page", max_octets=1000, max_retries=2)


def test_get_avec_limite_taille_timeout(monkeypatch):
    def _get(*a, **kw):
        raise requests.Timeout("trop long")

    monkeypatch.setattr(http_module.requests, "get", _get)
    monkeypatch.setattr(http_module.time, "sleep", lambda *_a, **_kw: None)

    with pytest.raises(ErreurCollecte):
        get_avec_limite_taille("https://exemple.invalid/page", max_octets=1000, max_retries=2)


# ------------------------------------- get_with_retry : headers (3.5) -----

class _FauxReponseOK:
    status_code = 200

    def raise_for_status(self):
        pass


def test_get_with_retry_sans_headers_envoie_seulement_le_user_agent(monkeypatch):
    appels = []
    monkeypatch.setattr(
        http_module.requests, "get",
        lambda url, **kw: (appels.append(kw), _FauxReponseOK())[-1],
    )

    get_with_retry("https://exemple.invalid/page")

    assert appels[0]["headers"] == {"User-Agent": USER_AGENT}


def test_get_with_retry_fusionne_les_headers_fournis(monkeypatch):
    appels = []
    monkeypatch.setattr(
        http_module.requests, "get",
        lambda url, **kw: (appels.append(kw), _FauxReponseOK())[-1],
    )

    get_with_retry("https://exemple.invalid/page", headers={"X-Subscription-Token": "abc"})

    assert appels[0]["headers"] == {"User-Agent": USER_AGENT, "X-Subscription-Token": "abc"}


# ------------------------------- espacement proactif par hôte (3.6, point 4)

class _HorlogeSimulee:
    """Horloge `time.monotonic()` déterministe, avancée par `time.sleep`
    plutôt que d'attendre réellement -- aucun réseau, aucune vraie seconde
    dépensée, comportement du limiteur pleinement observable."""

    def __init__(self, depart: float = 1_000.0):
        self.t = depart
        self.dodormis: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, secondes: float) -> None:
        self.dodormis.append(secondes)
        self.t += secondes


def _installer_horloge_simulee(monkeypatch) -> _HorlogeSimulee:
    horloge = _HorlogeSimulee()
    monkeypatch.setattr(http_module.time, "monotonic", horloge.monotonic)
    monkeypatch.setattr(http_module.time, "sleep", horloge.sleep)
    monkeypatch.setattr(http_module.requests, "get", lambda *a, **kw: _FauxReponseOK())
    return horloge


def test_deux_appels_reddit_consecutifs_sont_espaces_d_au_moins_6s(monkeypatch):
    horloge = _installer_horloge_simulee(monkeypatch)

    get_with_retry("https://www.reddit.com/r/smallbusiness/search.rss?q=x")
    get_with_retry("https://www.reddit.com/r/smallbusiness/search.rss?q=y")

    # Premier appel : aucun historique pour cet hôte -> aucune attente.
    # Deuxième appel, même hôte, horloge inchangée entre les deux -> attente
    # complète du délai minimal Reddit.
    assert horloge.dodormis == [6.0]


def test_deux_hotes_differents_ne_se_bloquent_pas_entre_eux(monkeypatch):
    horloge = _installer_horloge_simulee(monkeypatch)

    get_with_retry("https://www.reddit.com/r/smallbusiness/search.rss?q=x")
    get_with_retry("https://hn.algolia.com/api/v1/search_by_date?query=x&tags=comment")

    # Chacun de ces deux hôtes n'a encore jamais été appelé -> aucune attente,
    # même si l'horloge n'a pas avancé entre les deux appels.
    assert horloge.dodormis == []


def test_hn_algolia_espace_de_1s_get_avec_limite_taille_defaut_de_2s(monkeypatch):
    """Le délai minimal dépend de l'hôte, appliqué aussi par
    `get_avec_limite_taille` (pas seulement `get_with_retry`) -- ici
    hn.algolia.com (1 s) puis un hôte non listé (2 s, valeur par défaut)."""
    horloge = _HorlogeSimulee()
    monkeypatch.setattr(http_module.time, "monotonic", horloge.monotonic)
    monkeypatch.setattr(http_module.time, "sleep", horloge.sleep)
    monkeypatch.setattr(
        http_module.requests, "get",
        lambda *a, **kw: _FauxReponseStream([b"contenu"]),
    )

    get_avec_limite_taille("https://hn.algolia.com/api/v1/search_by_date?query=x", max_octets=1000)
    get_avec_limite_taille("https://hn.algolia.com/api/v1/search_by_date?query=y", max_octets=1000)
    get_avec_limite_taille("https://exemple-quelconque.invalid/pricing", max_octets=1000)

    assert horloge.dodormis == [1.0]  # 2e appel hn.algolia.com : 1 s. 3e appel : hôte jamais vu, 0 attente.


def test_backoff_429_reactif_conserve_en_plus_de_l_espacement_proactif(monkeypatch):
    """Le backoff existant sur 429 (base_delay avec doublement) s'ajoute à
    l'espacement proactif, il ne le remplace pas -- vérifié sur Reddit (6 s
    minimum) avec un backoff plus court (1 s) : les deux attentes doivent
    apparaître, pas seulement l'une des deux."""
    horloge = _HorlogeSimulee()
    monkeypatch.setattr(http_module.time, "monotonic", horloge.monotonic)
    monkeypatch.setattr(http_module.time, "sleep", horloge.sleep)

    reponses = iter([_FauxReponse429ThenOK.PREMIERE, _FauxReponse429ThenOK.SECONDE])
    monkeypatch.setattr(http_module.requests, "get", lambda *a, **kw: next(reponses))

    get_with_retry("https://www.reddit.com/search.rss?q=x", base_delay=1.0)

    # 1er essai : aucune attente proactive (hôte jamais vu), puis backoff
    # réactif de 1 s (base_delay * 2**0) sur le 429. 2e essai : espacement
    # proactif Reddit (6 s au total depuis le 1er essai, 1 s déjà écoulée via
    # le backoff -> 5 s de plus), avant le succès -- les deux attentes sont
    # bien distinctes et s'additionnent, aucune n'annule l'autre.
    assert horloge.dodormis == [1.0, 5.0]


class _FauxReponse429ThenOK:
    class _Reponse429:
        status_code = 429

        def raise_for_status(self):
            pass

    class _Reponse200:
        status_code = 200

        def raise_for_status(self):
            pass

    PREMIERE = _Reponse429()
    SECONDE = _Reponse200()
