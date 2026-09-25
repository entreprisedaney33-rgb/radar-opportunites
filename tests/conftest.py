from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from app import config as cfg
from app.adapters import http as http_module
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
    yield
    cfg.get_settings.cache_clear()
    http_module._dernier_appel_par_hote.clear()


@pytest.fixture
def engine_test(tmp_path):
    # Fichier sqlite dédié au test (pas ":memory:" : chaque connexion du pool
    # SQLAlchemy ouvrirait sinon sa propre base vide).
    chemin = tmp_path / "test.db"
    moteur = create_engine(f"sqlite:///{chemin}", future=True, connect_args={"check_same_thread": False})
    migrer(moteur)
    return moteur
