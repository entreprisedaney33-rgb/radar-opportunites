"""Sous-étape 1.2 : parseur du connecteur de recherche Reddit, sur une
fixture Atom construite à la main (format vérifié manuellement le
25/09/2026, voir Journal 1.2 — aucun réseau dans ces tests), et réaction à
un 429 simulé (déjà géré par `get_with_retry`, ici on vérifie juste que
l'adaptateur ne plante jamais et ne boucle pas).

Sous-étape 3.10, point 5 : un 429 PERSISTANT (`TropDeRequetes`,
`app.adapters.http`) est désormais laissé remonter TEL QUEL (pas swallowed
en liste vide comme avant) -- nécessaire pour le disjoncteur par passage de
`app.pipeline.orchestrator._collecter`, qui compte les 429 consécutifs pour
mettre Reddit en pause. Une panne SANS rapport avec un 429 (timeout, autre
erreur réseau), elle, continue de ne jamais faire planter la collecte des
autres flux -- swallowed en liste vide comme avant."""
from __future__ import annotations

import pytest

from app.adapters import http as http_module
from app.adapters.http import TropDeRequetes
from app.adapters.reddit_recherche import AdaptateurRechercheReddit

FIXTURE_ATOM = b"""<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom">
<updated>2026-09-25T17:51:40+00:00</updated>
<title>smallbusiness: search results - manually</title>
<entry>
  <author><name>/u/exemple_test</name></author>
  <content type="html">&lt;div&gt;&lt;p&gt;On fait &#231;a &#224; la main chaque semaine, c'est fatigant.&lt;/p&gt;&lt;/div&gt;</content>
  <id>t3_exemple1</id>
  <link href="https://www.reddit.com/r/smallbusiness/comments/exemple1/titre_exemple/" />
  <updated>2026-09-25T06:25:31+00:00</updated>
  <published>2026-09-25T06:25:31+00:00</published>
  <title>Titre exemple un</title>
</entry>
<entry>
  <author><name>/u/exemple_test2</name></author>
  <content type="html">&lt;div&gt;&lt;p&gt;Deuxi&#232;me entr&#233;e de test.&lt;/p&gt;&lt;/div&gt;</content>
  <id>t3_exemple2</id>
  <link href="https://www.reddit.com/r/smallbusiness/comments/exemple2/titre_exemple_deux/" />
  <updated>2026-09-25T05:00:00+00:00</updated>
  <published>2026-09-25T05:00:00+00:00</published>
  <title>Titre exemple deux</title>
</entry>
</feed>"""

FIXTURE_ATOM_VIDE = b"""<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom">
<updated>2026-09-25T17:51:40+00:00</updated>
<title>msp: search results - manually</title>
</feed>"""


class _FauxReponse:
    def __init__(self, content: bytes):
        self.content = content


def test_url_construite_avec_le_gabarit_et_l_expression_encodee():
    adaptateur = AdaptateurRechercheReddit("smallbusiness", "cle_test", "how do you handle")
    assert adaptateur.url == (
        "https://www.reddit.com/r/smallbusiness/search.rss?q=how%20do%20you%20handle&restrict_sr=on&sort=new"
    )
    assert adaptateur.id_source == "reddit_recherche:smallbusiness:cle_test"


def test_parse_les_entrees_de_la_fixture(monkeypatch):
    monkeypatch.setattr(
        "app.adapters.reddit_recherche.get_with_retry",
        lambda url: _FauxReponse(FIXTURE_ATOM),
    )
    adaptateur = AdaptateurRechercheReddit("smallbusiness", "a_la_main_fr", "à la main")

    signaux = adaptateur.collecter(10)

    assert len(signaux) == 2
    premier = signaux[0]
    assert premier.url == "https://www.reddit.com/r/smallbusiness/comments/exemple1/titre_exemple/"
    assert "Titre exemple un" in premier.texte
    assert premier.type_flux == "douleur"
    assert premier.requete_origine == "à la main"
    assert "smallbusiness" in premier.flux_origine
    assert premier.date_publication is not None


def test_respecte_le_budget_appels(monkeypatch):
    monkeypatch.setattr(
        "app.adapters.reddit_recherche.get_with_retry",
        lambda url: _FauxReponse(FIXTURE_ATOM),
    )
    adaptateur = AdaptateurRechercheReddit("smallbusiness", "a_la_main_fr", "à la main")

    signaux = adaptateur.collecter(1)

    assert len(signaux) == 1


def test_recherche_sans_resultat_renvoie_une_liste_vide(monkeypatch):
    monkeypatch.setattr(
        "app.adapters.reddit_recherche.get_with_retry",
        lambda url: _FauxReponse(FIXTURE_ATOM_VIDE),
    )
    adaptateur = AdaptateurRechercheReddit("msp", "manually_en", "manually")

    assert adaptateur.collecter(10) == []


def test_collecter_avec_engine_journalise_l_appel(monkeypatch, engine_test):
    """Sous-étape 3.7, point 1 : passé un `engine` (comme le fait
    `app.pipeline.orchestrator._collecter` en conditions réelles),
    `collecter` journalise l'appel dans `journal_http` avec `id_source`
    comme libellé de flux -- sans `engine` (tous les autres tests de ce
    fichier), rien n'est journalisé (comportement inchangé)."""
    from datetime import datetime, timezone

    from app.storage import repo

    class _FauxReponseHTTPComplete:
        status_code = 200
        content = FIXTURE_ATOM_VIDE

        def raise_for_status(self):
            pass

    monkeypatch.setattr(http_module.requests, "get", lambda *a, **kw: _FauxReponseHTTPComplete())
    adaptateur = AdaptateurRechercheReddit("smallbusiness", "manually_en", "manually")

    adaptateur.collecter(10, engine=engine_test)

    jour = datetime.now(timezone.utc).date()
    lignes = repo.lister_appels_http_jour_utc(engine_test, jour)
    assert lignes == [{
        "flux_ou_fournisseur": "reddit_recherche:smallbusiness:manually_en", "code_http": 200, "erreur": None,
    }]


def test_429_persistant_est_laisse_remonter_tel_quel(monkeypatch):
    """Sous-étape 3.10, point 5 : `get_with_retry` retente déjà avec backoff
    sur un 429 (§3 : attente, jamais une boucle infinie — plafonné à
    `max_retries`) ; au bout de ce plafond, il lève désormais `TropDeRequetes`
    -- l'adaptateur la laisse remonter TELLE QUELLE (ne la swallow plus en
    liste vide) pour que `app.pipeline.orchestrator._collecter` puisse
    compter les 429 consécutifs et mettre Reddit en pause pour le passage."""

    class Reponse429:
        status_code = 429

        def raise_for_status(self):
            pass

    monkeypatch.setattr(http_module.requests, "get", lambda *a, **kw: Reponse429())
    monkeypatch.setattr(http_module.time, "sleep", lambda *_a, **_kw: None)  # test rapide, pas de vraie attente

    adaptateur = AdaptateurRechercheReddit("smallbusiness", "manually_en", "manually")

    with pytest.raises(TropDeRequetes):
        adaptateur.collecter(10)


def test_timeout_ne_plante_pas_et_renvoie_une_liste_vide(monkeypatch):
    """Une panne SANS rapport avec un 429 (ici un timeout, jamais un vrai
    code HTTP reçu) reste swallowed en liste vide, exactement comme avant --
    seul le 429 persistant (ci-dessus) est traité différemment depuis la
    sous-étape 3.10."""
    import requests

    monkeypatch.setattr(http_module.requests, "get", lambda *a, **kw: (_ for _ in ()).throw(requests.Timeout("x")))
    monkeypatch.setattr(http_module.time, "sleep", lambda *_a, **_kw: None)

    adaptateur = AdaptateurRechercheReddit("smallbusiness", "manually_en", "manually")

    assert adaptateur.collecter(10) == []
