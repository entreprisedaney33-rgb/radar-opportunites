from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from app import config as cfg
from app.adapters import http as http_module
from app.adapters import reddit_api
from app.storage.db import migrer


@pytest.fixture(autouse=True)
def _cache_config_propre(monkeypatch):
    """Chaque test repart d'une configuration propre : `get_settings()` est
    en cache (lru_cache), donc un test qui change une variable d'env sans
    vider ce cache verrait l'ancienne valeur."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("RADAR_PAUSE_ALL", raising=False)
    # Sous-étape 3.4 (AMELIORATIONS.md) : depuis cette sous-étape, l'Enquêteur
    # est réellement branché dans `executer_run`/`executer_continu` -- ses
    # fournisseurs Algolia HN et Reddit (app/enqueteur/fournisseurs_gratuits.py)
    # font de VRAIS appels réseau et sont actifs par défaut. Désactivés ici
    # pour TOUTE la suite par défaut (garde-fou §0.2.5 : aucun appel réseau) —
    # via le mécanisme d'activation déjà posé en 3.1
    # (`RegistreFournisseurs.est_actif`, variable `RADAR_ENQUETEUR_ACTIF_<NOM>`).
    # Un test dédié qui veut exercer ces fournisseurs pour de vrai (avec une
    # réponse HTTP simulée, même pattern que `app/adapters/hn_recherche.py`
    # ailleurs) les réactive explicitement avec `monkeypatch.setenv(...,"1")`.
    # Le fournisseur "magasin_interne" (aucun réseau, lecture base seule)
    # reste actif par défaut.
    monkeypatch.setenv("RADAR_ENQUETEUR_ACTIF_ALGOLIA_HN", "0")
    monkeypatch.setenv("RADAR_ENQUETEUR_ACTIF_REDDIT", "0")
    # Sous-étape 3.5 : même esprit pour le futur fournisseur payant (Brave
    # Search, app/enqueteur/fournisseur_payant.py) -- désactivé par défaut
    # dans son propre code (`actif_par_defaut=False`), mais purgé ici aussi
    # pour qu'aucune variable laissée par une session précédente ne puisse
    # l'activer par accident pendant la suite de tests ; et sa clé n'est
    # jamais lue depuis l'environnement réel du poste qui lance les tests.
    monkeypatch.setenv("RADAR_ENQUETEUR_ACTIF_BRAVE_SEARCH", "0")
    monkeypatch.delenv("RADAR_BRAVE_SEARCH_API_KEY", raising=False)
    # V2.6 : le fournisseur web de la concurrence est désactivé par défaut ; aucune variable du poste ne peut l'activer pendant la suite.
    monkeypatch.delenv("RADAR_CONCURRENCE_WEB", raising=False)
    monkeypatch.delenv("RADAR_RECHERCHE_WEB_MAX_MOIS", raising=False)
    # Sous-étape 4.0 : jamais d'identifiants Reddit réels dans la suite (le
    # poste de Mathéo peut les avoir exportés) ; client partagé remis à zéro.
    for variable in reddit_api.VARIABLES_ENV:
        monkeypatch.delenv(variable, raising=False)
    reddit_api.reinitialiser_pour_tests()
    cfg.get_settings.cache_clear()
    # Sous-étape 3.6, point 4 : l'espacement proactif par hôte
    # (app.adapters.http, `_dernier_appel_par_hote`) est un état du PROCESSUS,
    # pas du test — sans ce nettoyage, un test qui réutilise le même hôte
    # factice (ex. "https://exemple.invalid/...", omniprésent dans la suite)
    # qu'un test précédent hériterait de son horloge réelle et déclencherait
    # une VRAIE attente (jusqu'à DELAI_MIN_PAR_DEFAUT_SECONDES). Un test
    # dédié à ce mécanisme simule sa propre horloge (`time.monotonic`) et n'a
    # donc pas besoin de ce nettoyage pour fonctionner.
    http_module._dernier_appel_par_hote.clear()
    # V2.8b : le réessai lent de SIRENE est aussi un état du PROCESSUS ; aucun test n'hérite de l'attente d'un autre.
    from app import etablissements as _etab
    _etab.reinitialiser_reessai_lent()
    yield
    cfg.get_settings.cache_clear()
    http_module._dernier_appel_par_hote.clear()
    _etab.reinitialiser_reessai_lent()


@pytest.fixture(autouse=True)
def _aucun_import_reel_d_etablissements(monkeypatch, tmp_path):
    """V2.8b : le worker importe `data/etablissements_import.json` (fichier réel, plusieurs mégaoctets, commité). Aucun test ne doit
    le lire par accident (cycle complet avec les opérations par défaut) : le chemin par défaut pointe vers un fichier absent. Les tests
    de l'import passent leur propre fichier explicitement."""
    from app import import_etablissements as imp
    monkeypatch.setattr(imp, "chemin_fichier", lambda: tmp_path / "aucun_import.json")
    imp.reinitialiser_cache()
    yield
    imp.reinitialiser_cache()


@pytest.fixture(autouse=True)
def _pages_courtes_permises_par_defaut(monkeypatch):
    """Sous-étape 3.17 : depuis cette sous-étape, une page fetchée de moins de
    `enqueteur_page_longueur_min_caracteres` (300) caractères n'est jamais
    stockée. La plupart des tests de la suite fabriquent des pages de
    quelques mots (`<p>Contenu.</p>`) pour exercer autre chose que ce
    contrôle : le seuil est donc ramené à 1 par défaut pour toute la suite.
    Les tests de ce contrôle (`tests/test_enqueteur_qualite_page.py`) le
    remettent explicitement à sa vraie valeur."""
    reel = cfg.quotas()
    monkeypatch.setattr(cfg, "quotas", lambda: {**reel, "enqueteur_page_longueur_min_caracteres": 1})


@pytest.fixture
def engine_test(tmp_path):
    # Fichier sqlite dédié au test (pas ":memory:" : chaque connexion du pool
    # SQLAlchemy ouvrirait sinon sa propre base vide).
    chemin = tmp_path / "test.db"
    moteur = create_engine(f"sqlite:///{chemin}", future=True, connect_args={"check_same_thread": False})
    migrer(moteur)
    return moteur


# --------------------------------------------------------------- Reddit (4.0)
class FausseReponseReddit:
    def __init__(self, status_code=200, corps=None, en_tetes=None):
        self.status_code = status_code
        self._corps = corps if corps is not None else {}
        self.headers = en_tetes or {}

    def json(self):
        if isinstance(self._corps, Exception):
            raise self._corps
        return self._corps


class FauxServeurReddit:
    """Sert de `http` au `ClientRedditAPI` : `post` = jeton, `get` = données.
    Réponses de données consommées dans l'ordre de `reponses` (la dernière est
    répétée) ; tout est enregistré dans `appels_get` / `appels_post`."""

    def __init__(self):
        self.reponses = [FausseReponseReddit(200, {"data": {"children": []}})]
        self.appels_get: list[dict] = []
        self.appels_post: list[dict] = []
        self.reponse_jeton = lambda n: FausseReponseReddit(200, {"access_token": f"jeton{n}", "expires_in": 86400})

    def post(self, url, **kw):
        self.appels_post.append({"url": url, **kw})
        return self.reponse_jeton(len(self.appels_post))

    def get(self, url, **kw):
        self.appels_get.append({"url": url, **kw})
        index = min(len(self.appels_get) - 1, len(self.reponses) - 1)
        return self.reponses[index]


@pytest.fixture
def reddit_faux(monkeypatch):
    """Identifiants factices posés dans l'environnement + client partagé
    branché sur un faux serveur, horloge et attente simulées (aucun réseau,
    aucune vraie attente). Renvoie `(serveur, attentes)`."""
    monkeypatch.setenv("RADAR_REDDIT_CLIENT_ID", "id_factice")
    monkeypatch.setenv("RADAR_REDDIT_CLIENT_SECRET", "secret_factice")
    monkeypatch.setenv("RADAR_REDDIT_USER_AGENT", "radar-test/1.0 (by /u/factice)")
    serveur = FauxServeurReddit()
    attentes: list[float] = []
    temps = {"t": 1000.0}

    def _dormir(secondes):
        attentes.append(secondes)
        temps["t"] += secondes

    client = reddit_api.ClientRedditAPI(
        "id_factice", "secret_factice", "radar-test/1.0 (by /u/factice)",
        http=serveur, horloge=lambda: temps["t"], dormir=_dormir,
    )
    client.temps = temps  # les tests peuvent faire avancer l'horloge
    reddit_api._client = client
    reddit_api._cle_client = reddit_api._identifiants()
    return serveur, attentes


def listing_reddit(*posts):
    """JSON de liste Reddit à partir de dicts de champs de posts."""
    return {"kind": "Listing", "data": {"children": [{"kind": "t3", "data": p} for p in posts]}}


def post_reddit(identifiant="a1", sub="smallbusiness", titre="Titre", texte="Corps du post", cree=1790000000, **extra):
    return {
        "id": identifiant, "subreddit": sub, "title": titre, "selftext": texte,
        "permalink": f"/r/{sub}/comments/{identifiant}/x/", "created_utc": cree, **extra,
    }
