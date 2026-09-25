"""Sous-étape 1.3 : parseur du connecteur de recherche Hacker News (API
Algolia), sur des fixtures JSON construites à partir de vraies réponses
observées le 25/09/2026 (`query=manually&tags=comment` et
`query=spreadsheet&tags=ask_hn`, voir Journal 1.3 — aucun réseau dans ces
tests), et réaction à un 429 simulé (déjà géré par `get_with_retry`)."""
from __future__ import annotations

import json

import pytest

from app.adapters import http as http_module
from app.adapters.hn_recherche import AdaptateurRechercheHN

FIXTURE_COMMENT = {
    "hits": [
        {
            "author": "moring",
            "comment_text": "This is a very software-engineery point of view and totally false for the ordinary user.",
            "created_at": "2026-09-25T17:49:45Z",
            "objectID": "49847766",
            "story_id": 49839664,
            "story_title": "What About Rails?",
            "story_url": "https://jardo.dev/what-about-rails",
        },
        {
            "author": "quelquun",
            "comment_text": "Deuxième commentaire de test.",
            "created_at": "2026-09-24T08:00:00Z",
            "objectID": "49800001",
            "story_id": 49800000,
            "story_title": "Un autre sujet",
        },
    ],
    "nbHits": 2,
}

FIXTURE_ASK_HN = {
    "hits": [
        {
            "author": "Marten42",
            "created_at": "2026-09-18T12:51:35Z",
            "objectID": "49753697",
            "story_text": "Hi HN, I built a sales calculation tool for teams that still rely heavily on Excel.",
            "title": "Sales calculation tool for Excel price lists",
        },
    ],
    "nbHits": 1,
}

FIXTURE_VIDE = {"hits": [], "nbHits": 0}


class _FauxReponse:
    def __init__(self, donnees: dict):
        self._donnees = donnees

    def json(self):
        return self._donnees


def test_url_construite_avec_le_gabarit_le_tag_et_l_expression_encodee():
    adaptateur = AdaptateurRechercheHN("comment", "cle_test", "how do you handle")
    assert adaptateur.url == (
        "https://hn.algolia.com/api/v1/search_by_date?query=how%20do%20you%20handle&tags=comment"
    )
    assert adaptateur.id_source == "hn_recherche:comment:cle_test"


def test_tag_inconnu_leve_une_erreur():
    with pytest.raises(ValueError, match="Tag de recherche HN inconnu"):
        AdaptateurRechercheHN("frontpage", "cle_test", "manually")


def test_parse_les_hits_du_tag_comment(monkeypatch):
    monkeypatch.setattr(
        "app.adapters.hn_recherche.get_with_retry",
        lambda url: _FauxReponse(FIXTURE_COMMENT),
    )
    adaptateur = AdaptateurRechercheHN("comment", "manually_en", "manually")

    signaux = adaptateur.collecter(10)

    assert len(signaux) == 2
    premier = signaux[0]
    assert premier.url == "https://news.ycombinator.com/item?id=49847766"
    assert "What About Rails?" in premier.texte
    assert "software-engineery" in premier.texte
    assert premier.type_flux == "douleur"
    assert premier.type_source == "autre"
    assert premier.requete_origine == "manually"
    assert "commentaires" in premier.flux_origine
    assert premier.date_publication is not None
    assert premier.date_publication.isoformat() == "2026-09-25T17:49:45+00:00"


def test_parse_les_hits_du_tag_ask_hn(monkeypatch):
    monkeypatch.setattr(
        "app.adapters.hn_recherche.get_with_retry",
        lambda url: _FauxReponse(FIXTURE_ASK_HN),
    )
    adaptateur = AdaptateurRechercheHN("ask_hn", "spreadsheet_en", "spreadsheet")

    signaux = adaptateur.collecter(10)

    assert len(signaux) == 1
    premier = signaux[0]
    assert premier.url == "https://news.ycombinator.com/item?id=49753697"
    assert "Sales calculation tool for Excel price lists" in premier.texte
    assert "still rely heavily on Excel" in premier.texte
    assert "Ask HN" in premier.flux_origine


def test_respecte_le_budget_appels(monkeypatch):
    monkeypatch.setattr(
        "app.adapters.hn_recherche.get_with_retry",
        lambda url: _FauxReponse(FIXTURE_COMMENT),
    )
    adaptateur = AdaptateurRechercheHN("comment", "manually_en", "manually")

    signaux = adaptateur.collecter(1)

    assert len(signaux) == 1


def test_hit_sans_objectid_est_ignore(monkeypatch):
    fixture = {"hits": [{"comment_text": "sans identifiant", "created_at": "2026-09-25T00:00:00Z"}], "nbHits": 1}
    monkeypatch.setattr(
        "app.adapters.hn_recherche.get_with_retry",
        lambda url: _FauxReponse(fixture),
    )
    adaptateur = AdaptateurRechercheHN("comment", "manually_en", "manually")

    assert adaptateur.collecter(10) == []


def test_recherche_sans_resultat_renvoie_une_liste_vide(monkeypatch):
    monkeypatch.setattr(
        "app.adapters.hn_recherche.get_with_retry",
        lambda url: _FauxReponse(FIXTURE_VIDE),
    )
    adaptateur = AdaptateurRechercheHN("ask_hn", "manually_en", "manually")

    assert adaptateur.collecter(10) == []


def test_reponse_non_json_ne_plante_pas(monkeypatch):
    class _ReponseCassee:
        def json(self):
            raise json.JSONDecodeError("boom", "", 0)

    monkeypatch.setattr(
        "app.adapters.hn_recherche.get_with_retry",
        lambda url: _ReponseCassee(),
    )
    adaptateur = AdaptateurRechercheHN("comment", "manually_en", "manually")

    assert adaptateur.collecter(10) == []


def test_collecter_avec_engine_journalise_l_appel(monkeypatch, engine_test):
    """Sous-étape 3.7, point 1 : passé un `engine` (comme le fait
    `app.pipeline.orchestrator._collecter` en conditions réelles),
    `collecter` journalise CHAQUE appel de recherche (un par tag valide,
    `comment` puis `ask_hn` -- voir `AdaptateurRechercheHN`, un seul flux
    logique par instance) dans `journal_http`, avec `id_source` comme
    libellé."""
    from datetime import datetime, timezone

    from app.storage import repo

    class _FauxReponseComplete:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return FIXTURE_VIDE

    monkeypatch.setattr(http_module.requests, "get", lambda *a, **kw: _FauxReponseComplete())
    adaptateur = AdaptateurRechercheHN("comment", "manually_en", "manually")

    adaptateur.collecter(10, engine=engine_test)

    jour = datetime.now(timezone.utc).date()
    lignes = repo.lister_appels_http_jour_utc(engine_test, jour)
    assert lignes == [{"flux_ou_fournisseur": "hn_recherche:comment:manually_en", "code_http": 200, "erreur": None}]


def test_429_persistant_ne_plante_pas_et_renvoie_une_liste_vide(monkeypatch):
    """`get_with_retry` retente déjà avec backoff sur un 429 (§3 : attente,
    jamais une boucle infinie — plafonné à `max_retries`) ; ici on vérifie
    que l'adaptateur, au bout de ce plafond, se contente de renvoyer une
    liste vide plutôt que de laisser l'exception remonter et interrompre la
    collecte des autres flux."""

    class Reponse429:
        status_code = 429

        def raise_for_status(self):
            pass

    monkeypatch.setattr(http_module.requests, "get", lambda *a, **kw: Reponse429())
    monkeypatch.setattr(http_module.time, "sleep", lambda *_a, **_kw: None)  # test rapide, pas de vraie attente

    adaptateur = AdaptateurRechercheHN("comment", "manually_en", "manually")

    assert adaptateur.collecter(10) == []
