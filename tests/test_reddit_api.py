"""Sous-étape 4.0 : client de l'API officielle Reddit. Aucun réseau : un faux
serveur (`tests/conftest.py::reddit_faux`) et une horloge simulée."""
from __future__ import annotations

import logging

import pytest
import requests

from app.adapters import reddit_api
from app.adapters.http import ErreurCollecte, TropDeRequetes
from tests.conftest import FausseReponseReddit, listing_reddit, post_reddit


def _client(reddit_faux):
    client = reddit_api.obtenir_client()
    assert client is not None
    return client


def test_inactif_sans_identifiants_et_le_dit_une_seule_fois(caplog):
    with caplog.at_level(logging.WARNING, logger="app.adapters.reddit_api"):
        assert reddit_api.obtenir_client() is None
        assert reddit_api.obtenir_client() is None
    messages = [r.getMessage() for r in caplog.records if "INACTIVE" in r.getMessage()]
    assert len(messages) == 1
    assert "RADAR_REDDIT_CLIENT_ID" in messages[0]
    assert reddit_api.configuree() is False


def test_inactif_si_une_seule_variable_manque(monkeypatch):
    monkeypatch.setenv("RADAR_REDDIT_CLIENT_ID", "x")
    monkeypatch.setenv("RADAR_REDDIT_CLIENT_SECRET", "y")
    assert reddit_api.obtenir_client() is None  # pas de user-agent


def test_le_message_d_inactivite_ne_contient_jamais_de_valeur(monkeypatch, caplog):
    monkeypatch.setenv("RADAR_REDDIT_CLIENT_ID", "valeur_secrete_visible")
    with caplog.at_level(logging.WARNING, logger="app.adapters.reddit_api"):
        reddit_api.obtenir_client()
    assert "valeur_secrete_visible" not in caplog.text


def test_jeton_demande_en_client_credentials_avec_basic_et_user_agent(reddit_faux):
    serveur, _ = reddit_faux
    _client(reddit_faux).get_json("/r/x/new", {}, contexte="reddit_api:new:x")
    post = serveur.appels_post[0]
    assert post["url"] == "https://www.reddit.com/api/v1/access_token"
    assert post["auth"] == ("id_factice", "secret_factice")
    assert post["data"] == {"grant_type": "client_credentials"}
    assert post["headers"]["User-Agent"] == "radar-test/1.0 (by /u/factice)"
    appel = serveur.appels_get[0]
    assert appel["url"] == "https://oauth.reddit.com/r/x/new"
    assert appel["headers"]["Authorization"] == "bearer jeton1"
    assert appel["params"]["raw_json"] == 1


def test_le_jeton_est_reutilise_puis_renouvele_avant_expiration(reddit_faux):
    serveur, _ = reddit_faux
    client = _client(reddit_faux)
    client.get_json("/a", {}, contexte="c")
    client.get_json("/b", {}, contexte="c")
    assert len(serveur.appels_post) == 1  # même jeton
    # 86400 s de vie : à 86400 - 60 s on est dans la marge de renouvellement (120 s).
    client.temps["t"] += 86400 - 60
    client.get_json("/c", {}, contexte="c")
    assert len(serveur.appels_post) == 2
    assert serveur.appels_get[-1]["headers"]["Authorization"] == "bearer jeton2"


def test_un_401_force_un_nouveau_jeton_puis_une_seule_nouvelle_tentative(reddit_faux):
    serveur, _ = reddit_faux
    serveur.reponses = [FausseReponseReddit(401), FausseReponseReddit(200, listing_reddit())]
    _client(reddit_faux).get_json("/a", {}, contexte="c")
    assert len(serveur.appels_post) == 2
    assert len(serveur.appels_get) == 2


def test_un_401_persistant_est_une_erreur_de_collecte(reddit_faux):
    serveur, _ = reddit_faux
    serveur.reponses = [FausseReponseReddit(401)]
    with pytest.raises(ErreurCollecte):
        _client(reddit_faux).get_json("/a", {}, contexte="c")
    assert len(serveur.appels_get) == 2


def test_jeton_refuse_est_une_erreur_de_collecte(reddit_faux):
    serveur, _ = reddit_faux
    serveur.reponse_jeton = lambda n: FausseReponseReddit(401)
    with pytest.raises(ErreurCollecte):
        _client(reddit_faux).get_json("/a", {}, contexte="c")
    assert serveur.appels_get == []


def test_plafond_de_30_requetes_par_minute(reddit_faux):
    serveur, attentes = reddit_faux
    client = _client(reddit_faux)
    # 29 appels de données + 1 jeton = 30 requêtes : la fenêtre est pleine, sans attente.
    for _ in range(reddit_api.PLAFOND_REQUETES_PAR_MINUTE - 1):
        client.get_json("/a", {}, contexte="c")
    assert attentes == []
    client.get_json("/a", {}, contexte="c")  # la 31e requête attend le glissement de la fenêtre
    assert len(attentes) == 1 and 0 < attentes[0] <= 60.0


def test_le_jeton_compte_dans_le_plafond(reddit_faux):
    """Le jeton est une requête vers Reddit : il occupe un créneau de la
    fenêtre, jamais un appel « gratuit »."""
    serveur, attentes = reddit_faux
    client = _client(reddit_faux)
    for _ in range(reddit_api.PLAFOND_REQUETES_PAR_MINUTE - 1):
        client.get_json("/a", {}, contexte="c")
    assert len(client._appels_recents) == reddit_api.PLAFOND_REQUETES_PAR_MINUTE


def test_les_en_tetes_ratelimit_epuises_font_attendre_le_reset(reddit_faux):
    serveur, attentes = reddit_faux
    serveur.reponses = [
        FausseReponseReddit(200, listing_reddit(), {"X-Ratelimit-Remaining": "0.0", "X-Ratelimit-Reset": "42"}),
        FausseReponseReddit(200, listing_reddit()),
    ]
    client = _client(reddit_faux)
    client.get_json("/a", {}, contexte="c")
    assert attentes == []
    client.get_json("/a", {}, contexte="c")
    assert attentes == [42.0]


def test_en_tetes_ratelimit_avec_du_reste_ne_font_pas_attendre(reddit_faux):
    serveur, attentes = reddit_faux
    serveur.reponses = [FausseReponseReddit(200, listing_reddit(), {"X-Ratelimit-Remaining": "97.0", "X-Ratelimit-Reset": "300"})]
    client = _client(reddit_faux)
    client.get_json("/a", {}, contexte="c")
    client.get_json("/a", {}, contexte="c")
    assert attentes == []


def test_429_attend_retry_after_puis_reessaie_une_fois(reddit_faux):
    serveur, attentes = reddit_faux
    serveur.reponses = [FausseReponseReddit(429, None, {"Retry-After": "7"}), FausseReponseReddit(200, listing_reddit())]
    _client(reddit_faux).get_json("/a", {}, contexte="c")
    assert attentes == [7.0]
    assert len(serveur.appels_get) == 2


def test_429_persistant_leve_trop_de_requetes(reddit_faux):
    serveur, _ = reddit_faux
    serveur.reponses = [FausseReponseReddit(429)]
    with pytest.raises(TropDeRequetes):
        _client(reddit_faux).get_json("/a", {}, contexte="c")
    assert len(serveur.appels_get) == 2  # une seule nouvelle tentative, jamais une boucle


def test_l_attente_sur_429_est_plafonnee(reddit_faux):
    serveur, attentes = reddit_faux
    serveur.reponses = [FausseReponseReddit(429, None, {"Retry-After": "9999"}), FausseReponseReddit(200, listing_reddit())]
    _client(reddit_faux).get_json("/a", {}, contexte="c")
    assert attentes == [reddit_api.ATTENTE_429_MAX_SECONDES]


def test_autre_code_http_et_reponse_non_json_sont_des_erreurs_de_collecte(reddit_faux):
    serveur, _ = reddit_faux
    serveur.reponses = [FausseReponseReddit(500)]
    with pytest.raises(ErreurCollecte):
        _client(reddit_faux).get_json("/a", {}, contexte="c")
    serveur.reponses = [FausseReponseReddit(200, ValueError("pas du json"))]
    with pytest.raises(ErreurCollecte):
        _client(reddit_faux).get_json("/a", {}, contexte="c")


def test_timeout_et_erreur_reseau_sont_des_erreurs_de_collecte(reddit_faux):
    serveur, _ = reddit_faux

    def _timeout(url, **kw):
        raise requests.Timeout("x")

    serveur.get = _timeout
    with pytest.raises(ErreurCollecte):
        _client(reddit_faux).get_json("/a", {}, contexte="c")


def test_chaque_appel_est_journalise_avec_le_prefixe_reddit_api(reddit_faux, engine_test):
    from datetime import datetime, timezone

    from app.storage import repo

    _client(reddit_faux).get_json("/r/x/new", {}, engine=engine_test, contexte="reddit_api:new:x")
    lignes = repo.lister_appels_http_jour_utc(engine_test, datetime.now(timezone.utc).date())
    assert {l["flux_ou_fournisseur"] for l in lignes} == {"reddit_api:jeton", "reddit_api:new:x"}
    assert all(l["code_http"] == 200 for l in lignes)


def test_signaux_ignorent_supprimes_retires_epingles_et_sans_lien():
    listing = listing_reddit(
        post_reddit("ok", titre="Vrai post", texte="Je perds des heures."),
        post_reddit("s", texte="[removed]"),
        post_reddit("d", texte="[deleted]"),
        post_reddit("r", removed_by_category="moderator"),
        post_reddit("e", stickied=True),
        post_reddit("v", titre="   "),
        {**post_reddit("p"), "permalink": ""},
    )
    signaux = reddit_api.signaux_depuis_listing(
        listing, domaine="Reddit r/x", droits="d", flux_origine="f", requete_origine=None, limite=10,
    )
    assert [s.url for s in signaux] == ["https://www.reddit.com/r/smallbusiness/comments/ok/x/"]
    assert signaux[0].texte == "Vrai post — Je perds des heures."
    assert signaux[0].type_source == "reddit_api"
    assert signaux[0].date_publication is not None


def test_signaux_tolerent_un_json_inattendu():
    for corps in ({}, {"data": None}, {"data": {"children": [None, 3, {"data": "x"}]}}):
        assert reddit_api.signaux_depuis_listing(
            corps, domaine="d", droits="d", flux_origine="f", requete_origine=None, limite=10,
        ) == []


def test_texte_tronque_a_la_longueur_maximale():
    listing = listing_reddit(post_reddit("g", texte="x" * 10000))
    [signal] = reddit_api.signaux_depuis_listing(
        listing, domaine="d", droits="d", flux_origine="f", requete_origine=None, limite=10,
    )
    assert len(signal.texte) <= len("Titre — ") + reddit_api.LONGUEUR_MAX_TEXTE
