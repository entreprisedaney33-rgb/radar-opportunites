"""Sous-étape 3.15 (AMELIORATIONS.md) : `config/domaines_exclus_concurrents.yaml`
+ son chargeur (`app.config.domaines_exclus_concurrents`) -- domaines qui ne
sont jamais des concurrents pour `app.enqueteur.concurrents.identifier_concurrents`."""
from app.config import domaines_exclus_concurrents


def test_domaines_exclus_concurrents_combine_les_deux_categories():
    domaines = domaines_exclus_concurrents()

    # Sources du radar (app/sources.yaml, connecteurs de recherche).
    for attendu in ("news.ycombinator.com", "techcrunch.com", "reddit.com", "producthunt.com"):
        assert attendu in domaines

    # Plateformes génériques.
    for attendu in (
        "anthropic.com", "platform.claude.com", "github.com",
        "medium.com", "substack.com", "youtube.com", "wikipedia.org",
    ):
        assert attendu in domaines


def test_domaines_exclus_concurrents_est_un_frozenset():
    assert isinstance(domaines_exclus_concurrents(), frozenset)
