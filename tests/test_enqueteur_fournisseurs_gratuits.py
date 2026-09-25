"""Sous-étape 3.2 : les 3 fournisseurs gratuits de l'Enquêteur — Algolia HN
et Reddit sur fixtures HTTP (mêmes formats déjà vérifiés en 1.3/1.2, aucun
réseau ici), magasin interne sur une base de test (aucun réseau, par
construction). Tous doivent respecter l'interface `FournisseurRecherche`
(`rechercher(requete, limite) -> list[ResultatRecherche]`, voir
`app.enqueteur.fournisseurs`)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine

from app import config as cfg
from app.adapters import http as http_module
from app.enqueteur.fournisseurs_gratuits import (
    FournisseurAlgoliaHN,
    FournisseurMagasinInterne,
    FournisseurReddit,
    construire_registre_fournisseurs_gratuits,
)
from app.storage import repo
from app.storage.db import migrer


@pytest.fixture
def engine_test(tmp_path):
    chemin = tmp_path / "test.db"
    moteur = create_engine(f"sqlite:///{chemin}", future=True, connect_args={"check_same_thread": False})
    migrer(moteur)
    return moteur


def _inserer_signal_concurrence(engine, *, url, domaine, extrait, requete_origine=None):
    from app.pipeline import dedupe

    repo.upsert_source(
        engine,
        url_canonique=dedupe.canonicaliser_url(url),
        domaine=domaine,
        date_publication=None,
        type_source="rss",
        extrait=extrait,
        empreinte=dedupe.empreinte_contenu(extrait),
        droits_collecte="test",
        flux_origine=domaine,
        requete_origine=requete_origine,
        etiquette="signal_concurrence",
    )


# --------------------------------------------------------------- Algolia HN

FIXTURE_HN_COMMENT = {
    "hits": [
        {
            "objectID": "111",
            "story_title": "Sujet A",
            "comment_text": "On perd des heures chaque semaine à réconcilier les factures à la main.",
            "created_at": "2026-09-25T17:49:45Z",
        },
        {
            "objectID": "112",
            "story_title": "Sujet B",
            "comment_text": "Deuxième commentaire.",
            "created_at": "2026-09-24T08:00:00Z",
        },
    ],
    "nbHits": 2,
}

FIXTURE_HN_ASK_HN = {
    "hits": [
        {"objectID": "221", "title": "Ask HN: outil de réconciliation ?", "story_text": "Existe-t-il un outil ?", "created_at": "2026-09-18T12:51:35Z"},
    ],
    "nbHits": 1,
}

FIXTURE_HN_VIDE = {"hits": [], "nbHits": 0}


class _FauxReponseJSON:
    def __init__(self, donnees: dict):
        self._donnees = donnees

    def json(self):
        return self._donnees


def test_sans_reseau_declare_uniquement_sur_le_magasin_interne():
    """Sous-étape 3.6, point 1 : seul le magasin interne (aucun appel réseau
    dans `rechercher`, une simple lecture en base) doit être exclu de
    `max_requetes_recherche_par_jour` (app.enqueteur.enqueteur._rechercher_par_famille,
    lu via `sans_reseau`) -- Algolia HN et Reddit font de vrais appels HTTP et
    doivent continuer à consommer ce compteur."""
    assert FournisseurMagasinInterne.sans_reseau is True
    assert FournisseurAlgoliaHN.sans_reseau is False
    assert FournisseurReddit.sans_reseau is False


def test_algolia_hn_interroge_les_deux_tags_et_fusionne(monkeypatch):
    urls_appelees: list[str] = []

    def _faux_get(url):
        urls_appelees.append(url)
        return _FauxReponseJSON(FIXTURE_HN_COMMENT if "tags=comment" in url else FIXTURE_HN_ASK_HN)

    monkeypatch.setattr("app.enqueteur.fournisseurs_gratuits.get_with_retry", _faux_get)

    fournisseur = FournisseurAlgoliaHN()
    resultats = fournisseur.rechercher("réconciliation factures", limite=10)

    assert len(urls_appelees) == 2
    assert "query=r%C3%A9conciliation%20factures" in urls_appelees[0]
    assert len(resultats) == 3
    assert all(r.fournisseur == "algolia_hn" for r in resultats)
    premier = resultats[0]
    assert premier.url == "https://news.ycombinator.com/item?id=111"
    assert premier.titre == "Sujet A"
    assert "réconcilier les factures" in premier.extrait
    assert premier.horodatage_source == datetime(2026, 9, 25, 17, 49, 45, tzinfo=timezone.utc)


def test_algolia_hn_respecte_la_limite_et_evite_le_second_appel_si_inutile(monkeypatch):
    urls_appelees: list[str] = []

    def _faux_get(url):
        urls_appelees.append(url)
        return _FauxReponseJSON(FIXTURE_HN_COMMENT)

    monkeypatch.setattr("app.enqueteur.fournisseurs_gratuits.get_with_retry", _faux_get)

    resultats = FournisseurAlgoliaHN().rechercher("peu importe", limite=1)

    assert len(resultats) == 1
    assert len(urls_appelees) == 1  # le tag comment seul suffit déjà à la limite : ask_hn jamais appelé


def test_algolia_hn_hit_sans_objectid_est_ignore(monkeypatch):
    fixture = {"hits": [{"comment_text": "sans identifiant", "created_at": "2026-09-25T00:00:00Z"}], "nbHits": 1}
    monkeypatch.setattr(
        "app.enqueteur.fournisseurs_gratuits.get_with_retry",
        lambda url: _FauxReponseJSON(fixture if "tags=comment" in url else FIXTURE_HN_VIDE),
    )
    assert FournisseurAlgoliaHN().rechercher("x", limite=10) == []


def test_algolia_hn_aucun_resultat_sur_les_deux_tags(monkeypatch):
    monkeypatch.setattr("app.enqueteur.fournisseurs_gratuits.get_with_retry", lambda url: _FauxReponseJSON(FIXTURE_HN_VIDE))
    assert FournisseurAlgoliaHN().rechercher("rien trouvé", limite=10) == []


def test_algolia_hn_reponse_non_json_sur_un_tag_n_empeche_pas_l_autre(monkeypatch):
    class _ReponseCassee:
        def json(self):
            raise json.JSONDecodeError("boom", "", 0)

    def _faux_get(url):
        if "tags=comment" in url:
            return _ReponseCassee()
        return _FauxReponseJSON(FIXTURE_HN_ASK_HN)

    monkeypatch.setattr("app.enqueteur.fournisseurs_gratuits.get_with_retry", _faux_get)

    resultats = FournisseurAlgoliaHN().rechercher("x", limite=10)
    assert len(resultats) == 1
    assert resultats[0].url == "https://news.ycombinator.com/item?id=221"


def test_algolia_hn_429_persistant_sur_un_tag_n_empeche_pas_l_autre(monkeypatch):
    appels = {"n": 0}

    def _faux_get(url):
        appels["n"] += 1
        if "tags=comment" in url:
            from app.adapters.http import ErreurCollecte

            raise ErreurCollecte("429 persistant (simulé)")
        return _FauxReponseJSON(FIXTURE_HN_ASK_HN)

    monkeypatch.setattr("app.enqueteur.fournisseurs_gratuits.get_with_retry", _faux_get)

    resultats = FournisseurAlgoliaHN().rechercher("x", limite=10)
    assert appels["n"] == 2
    assert len(resultats) == 1


def test_algolia_hn_avec_engine_journalise_chaque_tag(monkeypatch, engine_test):
    """Sous-étape 3.7, point 1 : `FournisseurAlgoliaHN(engine)` (comme le
    construit `construire_registre_fournisseurs_gratuits` en conditions
    réelles) journalise CHAQUE appel (un par tag) dans `journal_http`, avec
    un libellé distinct par tag -- sans `engine` (tous les autres tests de
    cette section), rien n'est journalisé."""

    class _FauxReponseComplete:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return FIXTURE_HN_VIDE

    monkeypatch.setattr(http_module.requests, "get", lambda *a, **kw: _FauxReponseComplete())

    FournisseurAlgoliaHN(engine_test).rechercher("réconciliation factures", limite=10)

    jour = datetime.now(timezone.utc).date()
    lignes = repo.lister_appels_http_jour_utc(engine_test, jour)
    assert {l["flux_ou_fournisseur"] for l in lignes} == {
        "enqueteur_recherche:algolia_hn:comment", "enqueteur_recherche:algolia_hn:ask_hn",
    }
    assert all(l["code_http"] == 200 and l["erreur"] is None for l in lignes)


# ------------------------------------------------------------------ Reddit

FIXTURE_REDDIT_ATOM = b"""<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom">
<updated>2026-09-25T17:51:40+00:00</updated>
<title>search results - reconciliation</title>
<entry>
  <id>t3_ex1</id>
  <link href="https://www.reddit.com/r/smallbusiness/comments/ex1/titre/" />
  <published>2026-09-25T06:25:31+00:00</published>
  <title>Titre exemple un</title>
  <summary>R&#233;sum&#233; exemple un.</summary>
</entry>
<entry>
  <id>t3_ex2</id>
  <link href="https://www.reddit.com/r/Accounting/comments/ex2/titre_deux/" />
  <published>2026-09-24T05:00:00+00:00</published>
  <title>Titre exemple deux</title>
  <summary>R&#233;sum&#233; exemple deux.</summary>
</entry>
</feed>"""

FIXTURE_REDDIT_ATOM_VIDE = b"""<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom">
<updated>2026-09-25T17:51:40+00:00</updated>
<title>search results - vide</title>
</feed>"""


class _FauxReponseHTTP:
    def __init__(self, content: bytes):
        self.content = content


def test_reddit_url_construite_sitewide_sans_subreddit(monkeypatch):
    urls_appelees: list[str] = []

    def _faux_get(url):
        urls_appelees.append(url)
        return _FauxReponseHTTP(FIXTURE_REDDIT_ATOM_VIDE)

    monkeypatch.setattr("app.enqueteur.fournisseurs_gratuits.get_with_retry", _faux_get)

    FournisseurReddit().rechercher("réconciliation factures", limite=10)

    assert urls_appelees == ["https://www.reddit.com/search.rss?q=r%C3%A9conciliation%20factures&sort=relevance&type=link"]


def test_reddit_parse_titre_et_extrait_separement(monkeypatch):
    monkeypatch.setattr("app.enqueteur.fournisseurs_gratuits.get_with_retry", lambda url: _FauxReponseHTTP(FIXTURE_REDDIT_ATOM))

    resultats = FournisseurReddit().rechercher("reconciliation", limite=10)

    assert len(resultats) == 2
    premier = resultats[0]
    assert premier.url == "https://www.reddit.com/r/smallbusiness/comments/ex1/titre/"
    assert premier.titre == "Titre exemple un"
    assert premier.extrait == "Résumé exemple un."
    assert premier.fournisseur == "reddit"
    assert premier.horodatage_source == datetime(2026, 9, 25, 6, 25, 31, tzinfo=timezone.utc)


def test_reddit_respecte_la_limite(monkeypatch):
    monkeypatch.setattr("app.enqueteur.fournisseurs_gratuits.get_with_retry", lambda url: _FauxReponseHTTP(FIXTURE_REDDIT_ATOM))
    assert len(FournisseurReddit().rechercher("reconciliation", limite=1)) == 1


def test_reddit_recherche_sans_resultat_renvoie_liste_vide(monkeypatch):
    monkeypatch.setattr("app.enqueteur.fournisseurs_gratuits.get_with_retry", lambda url: _FauxReponseHTTP(FIXTURE_REDDIT_ATOM_VIDE))
    assert FournisseurReddit().rechercher("rien", limite=10) == []


def test_reddit_429_persistant_ne_plante_pas(monkeypatch):
    class Reponse429:
        status_code = 429

        def raise_for_status(self):
            pass

    monkeypatch.setattr(http_module.requests, "get", lambda *a, **kw: Reponse429())
    monkeypatch.setattr(http_module.time, "sleep", lambda *_a, **_kw: None)

    assert FournisseurReddit().rechercher("x", limite=10) == []


def test_reddit_avec_engine_journalise_l_appel(monkeypatch, engine_test):
    """Sous-étape 3.7, point 1 : voir `test_algolia_hn_avec_engine_journalise_chaque_tag`."""

    class _FauxReponseComplete:
        status_code = 200
        content = FIXTURE_REDDIT_ATOM_VIDE

        def raise_for_status(self):
            pass

    monkeypatch.setattr(http_module.requests, "get", lambda *a, **kw: _FauxReponseComplete())

    FournisseurReddit(engine_test).rechercher("réconciliation factures", limite=10)

    jour = datetime.now(timezone.utc).date()
    lignes = repo.lister_appels_http_jour_utc(engine_test, jour)
    assert lignes == [{"flux_ou_fournisseur": "enqueteur_recherche:reddit", "code_http": 200, "erreur": None}]


# ------------------------------------------------------------- Magasin interne

def test_magasin_interne_filtre_sous_le_seuil_et_trie_par_similarite(engine_test):
    _inserer_signal_concurrence(
        engine_test, url="https://exemple.invalid/a", domaine="Show HN",
        extrait="Nouvel outil de réconciliation automatique de factures pour PME, fini le rapprochement bancaire à la main.",
    )
    _inserer_signal_concurrence(
        engine_test, url="https://exemple.invalid/b", domaine="Show HN",
        extrait="Réconciliation de factures et rapprochement bancaire, en un clic, pour les PME.",
    )
    _inserer_signal_concurrence(
        engine_test, url="https://exemple.invalid/c", domaine="TechCrunch",
        extrait="Une startup lève 10 millions pour son jeu vidéo mobile.",
    )

    fournisseur = FournisseurMagasinInterne(engine_test, seuil=0.15)
    resultats = fournisseur.rechercher("réconciliation factures rapprochement bancaire PME", limite=10)

    urls = [r.url for r in resultats]
    assert "https://exemple.invalid/c" not in urls  # hors sujet, sous le seuil
    assert urls[0] == "https://exemple.invalid/b"  # texte le plus proche de la requête en premier
    assert all(r.fournisseur == "magasin_interne" for r in resultats)


def test_magasin_interne_respecte_la_limite(engine_test):
    for i in range(5):
        _inserer_signal_concurrence(
            engine_test, url=f"https://exemple.invalid/{i}", domaine="Show HN",
            extrait="Réconciliation de factures et rapprochement bancaire pour PME.",
        )
    resultats = FournisseurMagasinInterne(engine_test, seuil=0.1).rechercher("réconciliation factures rapprochement", limite=2)
    assert len(resultats) == 2


def test_magasin_interne_sans_candidat_renvoie_liste_vide(engine_test):
    assert FournisseurMagasinInterne(engine_test, seuil=0.1).rechercher("x", limite=10) == []


def test_magasin_interne_ignore_les_sources_qui_ne_sont_pas_signal_concurrence(engine_test):
    from app.pipeline import dedupe

    repo.upsert_source(
        engine_test, url_canonique="https://exemple.invalid/douleur", domaine="Reddit r/smallbusiness",
        date_publication=None, type_source="rss",
        extrait="Réconciliation de factures et rapprochement bancaire pour PME.",
        empreinte=dedupe.empreinte_contenu("douleur"), droits_collecte="test",
        # pas d'etiquette : c'est un signal `douleur` normal, jamais un `signal_concurrence`
    )
    assert FournisseurMagasinInterne(engine_test, seuil=0.05).rechercher("réconciliation factures", limite=10) == []


def test_magasin_interne_seuil_par_defaut_vient_de_la_config(engine_test):
    assert FournisseurMagasinInterne(engine_test).seuil == cfg.quotas()["seuil_similarite_magasin_interne"]


# ------------------------------------------------------------------ registre

def test_construire_registre_enregistre_les_3_fournisseurs_actifs_par_defaut(engine_test, monkeypatch):
    for nom in ("ALGOLIA_HN", "REDDIT", "MAGASIN_INTERNE"):
        monkeypatch.delenv(f"RADAR_ENQUETEUR_ACTIF_{nom}", raising=False)

    registre = construire_registre_fournisseurs_gratuits(engine_test)
    actifs = registre.fournisseurs_actifs()

    assert {f.nom for f in actifs} == {"algolia_hn", "reddit", "magasin_interne"}


def test_construire_registre_chaque_fournisseur_reste_desactivable_individuellement(engine_test, monkeypatch):
    # Sous-étape 3.4 : la suite par défaut désactive déjà Algolia HN
    # (tests/conftest.py, garde-fou §0.2.5) -- remis à son défaut
    # (`actif_par_defaut=True`) pour isoler ce que ce test vérifie vraiment :
    # désactiver REDDIT seul n'affecte aucun autre fournisseur.
    monkeypatch.delenv("RADAR_ENQUETEUR_ACTIF_ALGOLIA_HN", raising=False)
    monkeypatch.setenv("RADAR_ENQUETEUR_ACTIF_REDDIT", "0")
    registre = construire_registre_fournisseurs_gratuits(engine_test)
    actifs = registre.fournisseurs_actifs()
    assert {f.nom for f in actifs} == {"algolia_hn", "magasin_interne"}
