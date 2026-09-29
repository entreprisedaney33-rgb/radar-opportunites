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
from app.enqueteur import disjoncteur
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
# Sous-étape 4.0 : API officielle (fixtures JSON, faux serveur `reddit_faux`).

from tests.conftest import FausseReponseReddit, listing_reddit, post_reddit  # noqa: E402

FIXTURE_REDDIT = listing_reddit(
    post_reddit("ex1", "smallbusiness", "Titre exemple un", "Résumé exemple un.", cree=1790000000),
    post_reddit("ex2", "Accounting", "Titre exemple deux", "Résumé exemple deux.", cree=1789900000),
)


def test_reddit_inactif_sans_identifiants_renvoie_vide_sans_appel():
    assert FournisseurReddit().rechercher("x", limite=10) == []


def test_reddit_recherche_sitewide_par_l_api(reddit_faux):
    serveur, _ = reddit_faux
    FournisseurReddit().rechercher("réconciliation factures", limite=10)
    appel = serveur.appels_get[0]
    assert appel["url"] == "https://oauth.reddit.com/search"
    assert appel["params"] == {
        "q": "réconciliation factures", "sort": "relevance", "type": "link", "limit": 10, "raw_json": 1,
    }


def test_reddit_parse_titre_et_extrait(reddit_faux):
    serveur, _ = reddit_faux
    serveur.reponses = [FausseReponseReddit(200, FIXTURE_REDDIT)]
    resultats = FournisseurReddit().rechercher("reconciliation", limite=10)
    assert len(resultats) == 2
    premier = resultats[0]
    assert premier.url == "https://www.reddit.com/r/smallbusiness/comments/ex1/x/"
    assert premier.titre == "Titre exemple un"
    assert "Résumé exemple un." in premier.extrait
    assert premier.fournisseur == "reddit"
    assert premier.horodatage_source == datetime.fromtimestamp(1790000000, tz=timezone.utc)


def test_reddit_respecte_la_limite(reddit_faux):
    serveur, _ = reddit_faux
    serveur.reponses = [FausseReponseReddit(200, FIXTURE_REDDIT)]
    assert len(FournisseurReddit().rechercher("reconciliation", limite=1)) == 1


def test_reddit_recherche_sans_resultat_renvoie_liste_vide(reddit_faux):
    assert FournisseurReddit().rechercher("rien", limite=10) == []


def test_reddit_429_persistant_ne_plante_pas(reddit_faux):
    serveur, _ = reddit_faux
    serveur.reponses = [FausseReponseReddit(429)]
    assert FournisseurReddit().rechercher("x", limite=10) == []


def test_reddit_avec_engine_journalise_l_appel(reddit_faux, engine_test):
    FournisseurReddit(engine_test).rechercher("réconciliation factures", limite=10)
    jour = datetime.now(timezone.utc).date()
    lignes = repo.lister_appels_http_jour_utc(engine_test, jour)
    assert {l["flux_ou_fournisseur"] for l in lignes} == {"reddit_api:jeton", "reddit_api:enqueteur"}


# --------------------------------------------- Reddit : disjoncteur (3.11)

def _simuler_429_persistant(serveur):
    serveur.reponses = [FausseReponseReddit(429)]


def test_reddit_sans_engine_jamais_de_disjoncteur(reddit_faux):
    serveur, _ = reddit_faux
    _simuler_429_persistant(serveur)
    f = FournisseurReddit()
    for _ in range(disjoncteur.SEUIL_ECHECS_CONSECUTIFS + 1):
        assert f.rechercher("x", limite=10) == []


def test_reddit_trois_429_consecutifs_declenche_la_pause(reddit_faux, engine_test):
    serveur, _ = reddit_faux
    _simuler_429_persistant(serveur)
    f = FournisseurReddit(engine_test)

    for _ in range(disjoncteur.SEUIL_ECHECS_CONSECUTIFS - 1):
        assert f.rechercher("x", limite=10) == []
        assert repo.lire_disjoncteur_enqueteur(engine_test, disjoncteur.NOM_REDDIT)["pause_jusqu_a"] is None

    assert f.rechercher("x", limite=10) == []
    etat = repo.lire_disjoncteur_enqueteur(engine_test, disjoncteur.NOM_REDDIT)
    assert etat["echecs_consecutifs"] == 0
    assert etat["pause_jusqu_a"] is not None
    assert etat["pause_jusqu_a"] > datetime.now(timezone.utc)


def test_reddit_en_pause_ne_tente_meme_pas_l_appel_http(reddit_faux, engine_test):
    from datetime import timedelta

    serveur, _ = reddit_faux
    repo.ecrire_disjoncteur_enqueteur(
        engine_test, disjoncteur.NOM_REDDIT,
        echecs_consecutifs=0, pause_jusqu_a=datetime.now(timezone.utc) + timedelta(minutes=30),
    )
    assert FournisseurReddit(engine_test).rechercher("x", limite=10) == []
    assert serveur.appels_get == [] and serveur.appels_post == []


def test_reddit_un_succes_remet_le_compteur_a_zero(reddit_faux, engine_test):
    serveur, _ = reddit_faux
    _simuler_429_persistant(serveur)
    f = FournisseurReddit(engine_test)
    for _ in range(disjoncteur.SEUIL_ECHECS_CONSECUTIFS - 1):
        f.rechercher("x", limite=10)
    assert repo.lire_disjoncteur_enqueteur(engine_test, disjoncteur.NOM_REDDIT)["echecs_consecutifs"] == (
        disjoncteur.SEUIL_ECHECS_CONSECUTIFS - 1
    )
    serveur.appels_get.clear()
    serveur.reponses = [FausseReponseReddit(200, listing_reddit())]
    f.rechercher("succes", limite=10)
    assert repo.lire_disjoncteur_enqueteur(engine_test, disjoncteur.NOM_REDDIT) == {
        "echecs_consecutifs": 0, "pause_jusqu_a": None,
    }


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

def test_construire_registre_reddit_inactif_sans_identifiants(engine_test, monkeypatch):
    """Sous-étape 4.0 : sans identifiants d'environnement, Reddit reste inactif
    par défaut (aucune requête ni compteur gaspillés) ; Algolia HN et le
    magasin interne le sont toujours."""
    for nom in ("ALGOLIA_HN", "REDDIT", "MAGASIN_INTERNE"):
        monkeypatch.delenv(f"RADAR_ENQUETEUR_ACTIF_{nom}", raising=False)
    registre = construire_registre_fournisseurs_gratuits(engine_test)
    assert {f.nom for f in registre.fournisseurs_actifs()} == {"algolia_hn", "magasin_interne"}


def test_construire_registre_reddit_actif_par_defaut_avec_identifiants(engine_test, monkeypatch, reddit_faux):
    """Sous-étape 4.0 : réactivé PAR DÉFAUT dès que les trois variables sont
    posées."""
    for nom in ("ALGOLIA_HN", "REDDIT", "MAGASIN_INTERNE"):
        monkeypatch.delenv(f"RADAR_ENQUETEUR_ACTIF_{nom}", raising=False)
    registre = construire_registre_fournisseurs_gratuits(engine_test)
    assert {f.nom for f in registre.fournisseurs_actifs()} == {"algolia_hn", "reddit", "magasin_interne"}


def test_construire_registre_reddit_desactivable_meme_avec_identifiants(engine_test, monkeypatch, reddit_faux):
    monkeypatch.delenv("RADAR_ENQUETEUR_ACTIF_ALGOLIA_HN", raising=False)
    monkeypatch.setenv("RADAR_ENQUETEUR_ACTIF_REDDIT", "0")
    registre = construire_registre_fournisseurs_gratuits(engine_test)
    assert "reddit" not in {f.nom for f in registre.fournisseurs_actifs()}


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
