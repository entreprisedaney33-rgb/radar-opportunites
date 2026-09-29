"""Sous-étape 4.0 : connecteur de recherche Reddit (sub × expression) par
l'API officielle, sur des fixtures JSON (aucun réseau). Sous-étape 3.10 :
un 429 persistant est laissé remonter (disjoncteur par passage) ; toute
autre panne n'arrête jamais la collecte des autres flux."""
from __future__ import annotations

import pytest
import requests

from app.adapters.http import TropDeRequetes
from app.adapters.reddit_recherche import AdaptateurRechercheReddit
from tests.conftest import FausseReponseReddit, listing_reddit, post_reddit

FIXTURE = listing_reddit(
    post_reddit("exemple1", titre="Titre exemple un", texte="On fait ça à la main chaque semaine."),
    post_reddit("exemple2", titre="Titre exemple deux", texte="Deuxième entrée de test."),
)


def test_inactif_sans_identifiants_ne_fait_aucun_appel():
    assert AdaptateurRechercheReddit("smallbusiness", "k", "manually").collecter(10) == []


def test_appelle_l_api_de_recherche_du_subreddit(reddit_faux):
    serveur, _ = reddit_faux
    serveur.reponses = [FausseReponseReddit(200, FIXTURE)]
    AdaptateurRechercheReddit("smallbusiness", "a_la_main_fr", "à la main").collecter(5)
    appel = serveur.appels_get[0]
    assert appel["url"] == "https://oauth.reddit.com/r/smallbusiness/search"
    assert appel["params"] == {
        "q": "à la main", "restrict_sr": 1, "sort": "new", "type": "link", "limit": 5, "raw_json": 1,
    }


def test_id_source_inchange():
    a = AdaptateurRechercheReddit("smallbusiness", "cle_test", "how do you handle")
    assert a.id_source == "reddit_recherche:smallbusiness:cle_test"


def test_parse_les_entrees_de_la_fixture(reddit_faux):
    serveur, _ = reddit_faux
    serveur.reponses = [FausseReponseReddit(200, FIXTURE)]
    signaux = AdaptateurRechercheReddit("smallbusiness", "a_la_main_fr", "à la main").collecter(10)
    assert len(signaux) == 2
    premier = signaux[0]
    assert premier.url == "https://www.reddit.com/r/smallbusiness/comments/exemple1/x/"
    assert "Titre exemple un" in premier.texte and "à la main" in premier.texte
    assert premier.type_flux == "douleur"
    assert premier.requete_origine == "à la main"
    assert "smallbusiness" in premier.flux_origine
    assert premier.date_publication is not None
    assert "API officielle Reddit" in premier.droits_collecte


def test_respecte_le_budget_appels(reddit_faux):
    serveur, _ = reddit_faux
    serveur.reponses = [FausseReponseReddit(200, FIXTURE)]
    assert len(AdaptateurRechercheReddit("smallbusiness", "k", "x").collecter(1)) == 1


def test_recherche_sans_resultat_renvoie_une_liste_vide(reddit_faux):
    assert AdaptateurRechercheReddit("msp", "manually_en", "manually").collecter(10) == []


def test_collecter_avec_engine_journalise_l_appel_sous_reddit_api(reddit_faux, engine_test):
    from datetime import datetime, timezone

    from app.storage import repo

    AdaptateurRechercheReddit("smallbusiness", "manually_en", "manually").collecter(10, engine=engine_test)
    lignes = repo.lister_appels_http_jour_utc(engine_test, datetime.now(timezone.utc).date())
    assert {l["flux_ou_fournisseur"] for l in lignes} == {
        "reddit_api:jeton", "reddit_api:recherche:smallbusiness:manually_en",
    }


def test_429_persistant_est_laisse_remonter_tel_quel(reddit_faux):
    serveur, _ = reddit_faux
    serveur.reponses = [FausseReponseReddit(429)]
    with pytest.raises(TropDeRequetes):
        AdaptateurRechercheReddit("smallbusiness", "manually_en", "manually").collecter(10)


def test_timeout_ne_plante_pas_et_renvoie_une_liste_vide(reddit_faux):
    serveur, _ = reddit_faux

    def _timeout(url, **kw):
        raise requests.Timeout("x")

    serveur.get = _timeout
    assert AdaptateurRechercheReddit("smallbusiness", "manually_en", "manually").collecter(10) == []
