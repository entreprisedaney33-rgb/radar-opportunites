"""Sous-étape 3.5 (AMELIORATIONS.md) : fournisseur "moteur web" payant
(Brave Search), derrière un drapeau, désactivé par défaut. Sans réseau
(client HTTP simulé) -- jamais un vrai appel à Brave Search, jamais une
vraie clé dans ce fichier."""
from __future__ import annotations

import pytest

from app.adapters.http import ErreurCollecte
from app.enqueteur import fournisseur_payant as module
from app.enqueteur.fournisseur_payant import (
    VARIABLE_CLE,
    FournisseurBraveSearch,
    FournisseurWebPayantNonConfigure,
    definition_fournisseur_brave_search,
)
from app.enqueteur.fournisseurs import RegistreFournisseurs
from app.enqueteur.fournisseurs_gratuits import construire_registre_fournisseurs_gratuits

# `engine_test` : fixture globale (tests/conftest.py). `RADAR_BRAVE_SEARCH_API_KEY`
# est purgée et `RADAR_ENQUETEUR_ACTIF_BRAVE_SEARCH` forcée à "0" pour toute
# la suite par défaut par ce même conftest (garde-fou §0.2.5, même esprit que
# pour Algolia HN/Reddit depuis la sous-étape 3.4) -- chaque test ci-dessous
# qui a besoin d'une clé ou d'une activation la pose lui-même.


class _FauxReponseJSON:
    def __init__(self, donnees, status_code=200):
        self._donnees = donnees
        self.status_code = status_code

    def json(self):
        return self._donnees


class _FauxReponseNonJSON:
    status_code = 200

    def json(self):
        raise ValueError("pas du JSON")


# ------------------------------------------------------- sans configuration

def test_rechercher_sans_cle_leve_sans_appeler_le_reseau(monkeypatch):
    appele = {"n": 0}
    monkeypatch.setattr(module, "get_with_retry", lambda *a, **kw: appele.__setitem__("n", appele["n"] + 1))

    with pytest.raises(FournisseurWebPayantNonConfigure):
        FournisseurBraveSearch().rechercher("rapprochement bancaire manuel", 5)

    assert appele["n"] == 0  # jamais d'appel réseau sans la clé


# ------------------------------------------------------------ avec une clé

def test_rechercher_envoie_la_cle_en_en_tete_et_parse_les_resultats(monkeypatch):
    appels = []

    def _faux_get_with_retry(url, **kw):
        appels.append((url, kw))
        return _FauxReponseJSON({
            "web": {"results": [
                {"url": "https://concurrentx.example/", "title": "ConcurrentX", "description": "Un concurrent."},
                {"url": "https://concurrenty.example/", "title": "ConcurrentY", "description": "Un autre."},
            ]}
        })

    monkeypatch.setenv(VARIABLE_CLE, "vraie-cle-de-test-jamais-reelle")
    monkeypatch.setattr(module, "get_with_retry", _faux_get_with_retry)

    resultats = FournisseurBraveSearch().rechercher("rapprochement bancaire manuel", 5)

    assert len(resultats) == 2
    assert resultats[0].url == "https://concurrentx.example/"
    assert resultats[0].titre == "ConcurrentX"
    assert resultats[0].extrait == "Un concurrent."
    assert resultats[0].fournisseur == "brave_search"
    assert resultats[0].horodatage_source is None

    url_appelee, kwargs = appels[0]
    assert url_appelee.startswith(module.URL_RECHERCHE + "?")
    assert "q=rapprochement+bancaire+manuel" in url_appelee
    assert "count=5" in url_appelee
    assert kwargs["headers"] == {"X-Subscription-Token": "vraie-cle-de-test-jamais-reelle"}


def test_rechercher_plafonne_count_au_maximum_documente_de_l_api(monkeypatch):
    appels = []

    def _faux_get_with_retry(url, **kw):
        appels.append(url)
        return _FauxReponseJSON({"web": {"results": []}})

    monkeypatch.setenv(VARIABLE_CLE, "cle-de-test")
    monkeypatch.setattr(module, "get_with_retry", _faux_get_with_retry)

    FournisseurBraveSearch().rechercher("une requête", 50)  # au-delà du plafond documenté (20)

    assert f"count={module.MAX_RESULTATS_PAR_APPEL}" in appels[0]


def test_rechercher_respecte_la_limite(monkeypatch):
    monkeypatch.setenv(VARIABLE_CLE, "cle-de-test")
    monkeypatch.setattr(
        module, "get_with_retry",
        lambda url, **kw: _FauxReponseJSON({
            "web": {"results": [
                {"url": f"https://exemple{i}.invalid/", "title": f"Résultat {i}", "description": "d"}
                for i in range(10)
            ]}
        }),
    )

    resultats = FournisseurBraveSearch().rechercher("une requête", 3)

    assert len(resultats) == 3


def test_rechercher_ignore_un_resultat_sans_url_ou_sans_titre(monkeypatch):
    monkeypatch.setenv(VARIABLE_CLE, "cle-de-test")
    monkeypatch.setattr(
        module, "get_with_retry",
        lambda url, **kw: _FauxReponseJSON({
            "web": {"results": [
                {"url": "", "title": "Sans URL", "description": "d"},
                {"url": "https://ok.invalid/", "title": "", "description": "d"},
                {"url": "https://ok.invalid/vrai", "title": "Vrai résultat", "description": "d"},
            ]}
        }),
    )

    resultats = FournisseurBraveSearch().rechercher("une requête", 5)

    assert len(resultats) == 1
    assert resultats[0].url == "https://ok.invalid/vrai"


def test_rechercher_reponse_non_json_renvoie_liste_vide(monkeypatch):
    monkeypatch.setenv(VARIABLE_CLE, "cle-de-test")
    monkeypatch.setattr(module, "get_with_retry", lambda url, **kw: _FauxReponseNonJSON())

    assert FournisseurBraveSearch().rechercher("une requête", 5) == []


def test_rechercher_erreur_collecte_absorbee_sans_planter(monkeypatch):
    monkeypatch.setenv(VARIABLE_CLE, "cle-de-test")

    def _leve(url, **kw):
        raise ErreurCollecte("panne simulée")

    monkeypatch.setattr(module, "get_with_retry", _leve)

    assert FournisseurBraveSearch().rechercher("une requête", 5) == []


# ----------------------------------------------------- désactivé par défaut

def test_definition_est_desactivee_par_defaut():
    assert definition_fournisseur_brave_search().actif_par_defaut is False


def test_registre_avec_la_definition_reste_inactif_sans_variable_d_environnement(monkeypatch):
    monkeypatch.delenv("RADAR_ENQUETEUR_ACTIF_BRAVE_SEARCH", raising=False)
    registre = RegistreFournisseurs()
    registre.enregistrer(definition_fournisseur_brave_search())

    assert registre.est_actif("brave_search") is False
    assert registre.fournisseurs_actifs() == []


def test_registre_activable_explicitement_par_la_meme_variable_que_les_gratuits(monkeypatch):
    """Aucun mécanisme nouveau : réutilise `RegistreFournisseurs.est_actif`
    (sous-étape 3.1), comme pour Algolia HN/Reddit."""
    monkeypatch.setenv("RADAR_ENQUETEUR_ACTIF_BRAVE_SEARCH", "1")
    registre = RegistreFournisseurs()
    registre.enregistrer(definition_fournisseur_brave_search())

    assert registre.est_actif("brave_search") is True


def test_pas_enregistre_dans_le_registre_des_fournisseurs_gratuits(engine_test):
    """Point 3 du texte de 3.5 (« Ne pas activer ») : ce fournisseur n'est
    câblé dans AUCUN registre utilisé par le pipeline réel
    (`app.pipeline.orchestrator._phase_enquete` appelle uniquement
    `construire_registre_fournisseurs_gratuits`)."""
    registre = construire_registre_fournisseurs_gratuits(engine_test)

    with pytest.raises(KeyError):
        registre.est_actif("brave_search")
