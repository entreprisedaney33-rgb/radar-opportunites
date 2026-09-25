"""`app.adapters.rss_adapter.AdaptateurRSS` — sans réseau (client HTTP
simulé). Sous-étape 3.7 (AMELIORATIONS.md) : `collecter` journalise l'appel
dans `journal_http` quand un `engine` est fourni (comme le fait
`app.pipeline.orchestrator._collecter` en conditions réelles), jamais sinon."""
from __future__ import annotations

from datetime import datetime, timezone

from app.adapters import http as http_module
from app.adapters.rss_adapter import AdaptateurRSS
from app.storage import repo

FIXTURE_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>Flux de test</title>
<item>
  <title>Titre exemple</title>
  <link>https://exemple.invalid/article-1</link>
  <description>Resume exemple.</description>
  <pubDate>Fri, 25 Sep 2026 06:25:31 GMT</pubDate>
</item>
</channel></rss>"""


class _FauxReponseComplete:
    status_code = 200
    content = FIXTURE_RSS

    def raise_for_status(self):
        pass


def test_collecter_sans_engine_ne_journalise_rien(monkeypatch, engine_test):
    monkeypatch.setattr(http_module.requests, "get", lambda *a, **kw: _FauxReponseComplete())
    adaptateur = AdaptateurRSS("mon_flux", "Mon Flux", "https://exemple.invalid/rss")

    signaux = adaptateur.collecter(10)

    assert len(signaux) == 1
    jour = datetime.now(timezone.utc).date()
    assert repo.lister_appels_http_jour_utc(engine_test, jour) == []


def test_collecter_avec_engine_journalise_l_appel(monkeypatch, engine_test):
    monkeypatch.setattr(http_module.requests, "get", lambda *a, **kw: _FauxReponseComplete())
    adaptateur = AdaptateurRSS("mon_flux", "Mon Flux", "https://exemple.invalid/rss")

    adaptateur.collecter(10, engine=engine_test)

    jour = datetime.now(timezone.utc).date()
    lignes = repo.lister_appels_http_jour_utc(engine_test, jour)
    assert lignes == [{"flux_ou_fournisseur": "mon_flux", "code_http": 200, "erreur": None}]
