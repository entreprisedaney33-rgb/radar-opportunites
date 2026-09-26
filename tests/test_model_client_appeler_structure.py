"""Sous-étape 3.10 (AMELIORATIONS.md) : `ModelClient.appeler_structure` de
bout en bout, avec un client Anthropic simulé (aucun réseau). Reproduit les
déformations observées en usage réel sur Render (voir Journal de la
sous-étape) : objet unique au lieu d'une liste, chaîne JSON, sortie `{}`
vide, sortie tronquée (`stop_reason == "max_tokens"`), et le cas normal
(sortie valide du premier coup)."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select

from app.adapters.model_client import (
    ISSUE_NORMALISEE,
    ISSUE_PERDUE,
    ISSUE_RELANCEE,
    ISSUE_VALIDE,
    MAX_TENTATIVES,
    ModelClient,
)
from app.config import Settings
from app.models_schemas import CriticSortie
from app.pipeline.budget import BudgetTracker
from app.storage.db import migrer
from app.storage.schema import usage_events


@pytest.fixture
def engine_test(tmp_path):
    chemin = tmp_path / "test.db"
    moteur = create_engine(f"sqlite:///{chemin}", future=True, connect_args={"check_same_thread": False})
    migrer(moteur)
    return moteur


def _settings():
    return Settings(
        database_url="sqlite:///./ignore.db", anthropic_api_key="fake-key", model_tri="m-tri",
        model_approfondi="m-approfondi", apify_token=None, ui_password=None, pause_all=False,
    )


class _FauxUsage:
    def __init__(self, input_tokens=100, output_tokens=50):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FauxBlocOutil:
    def __init__(self, input_):
        self.type = "tool_use"
        self.input = input_


class _FauxReponse:
    def __init__(self, *, input_outil=None, stop_reason="tool_use"):
        self.usage = _FauxUsage()
        self.stop_reason = stop_reason
        self.content = [] if input_outil is None else [_FauxBlocOutil(input_outil)]


class _FauxMessages:
    def __init__(self, appels: list, reponses):
        self._appels = appels
        self._reponses = iter(reponses)

    def create(self, **kwargs):
        self._appels.append(kwargs)
        return next(self._reponses)


class _FauxClient:
    def __init__(self, reponses):
        self.appels: list = []
        self.messages = _FauxMessages(self.appels, reponses)


def _client_pret(engine, reponses) -> tuple[ModelClient, list]:
    budget = BudgetTracker(engine, "run-test", plafond_eur=25.0, plafond_appels_approfondis=1000)
    client = ModelClient(_settings(), budget)
    faux = _FauxClient(reponses)
    client._client = faux  # évite l'import réel du SDK / une vraie clé
    return client, faux.appels


def _lignes_usage_events(engine) -> list[dict]:
    with engine.connect() as cx:
        rows = cx.execute(select(usage_events).order_by(usage_events.c.date_creation)).mappings().all()
    return [dict(r) for r in rows]


CRITIC_VALIDE = {
    "opportunity_id": "o1", "objections": [{"texte": "Faille X.", "source_ids": []}],
    "faits_contestes": [], "recherches_supplementaires": [], "decision": "a_verifier", "motif": "à vérifier",
}


def test_sortie_objet_unique_au_lieu_d_une_liste_est_normalisee(engine_test):
    """Déformation observée : `objections` renvoyé comme un OBJET unique
    (pas une liste, pas une chaîne) -- le filet de `_normaliser_sortie_outil`
    (sous-étape 3.10, point 3 de sa docstring) le remet dans une liste."""
    payload = dict(CRITIC_VALIDE, objections={"texte": "Faille X.", "source_ids": []})
    client, appels = _client_pret(engine_test, [_FauxReponse(input_outil=payload)])

    resultat = client.appeler_structure(
        modele="claude-sonnet-5", prompt_systeme="s", prompt_utilisateur="u",
        schema=CriticSortie, version_prompt="v1", role="critic", opportunity_id="o1",
    )

    assert resultat is not None
    assert resultat.objections[0].texte == "Faille X."
    assert len(appels) == 1  # jamais de relance : la normalisation a suffi
    lignes = _lignes_usage_events(engine_test)
    assert len(lignes) == 1
    assert lignes[0]["issue"] == ISSUE_NORMALISEE


def test_sortie_liste_en_chaine_json_est_normalisee(engine_test):
    """Déformation déjà connue (sous-étape initiale) : `objections` renvoyé
    comme une chaîne JSON -- vérifiée ici de bout en bout (pas seulement sur
    la fonction pure, voir tests/test_model_client.py)."""
    payload = dict(CRITIC_VALIDE, objections='[{"texte": "Faille X.", "source_ids": []}]')
    client, appels = _client_pret(engine_test, [_FauxReponse(input_outil=payload)])

    resultat = client.appeler_structure(
        modele="claude-sonnet-5", prompt_systeme="s", prompt_utilisateur="u",
        schema=CriticSortie, version_prompt="v1", role="critic", opportunity_id="o1",
    )

    assert resultat is not None
    assert resultat.objections[0].texte == "Faille X."
    assert len(appels) == 1
    assert _lignes_usage_events(engine_test)[0]["issue"] == ISSUE_NORMALISEE


def test_sortie_vide_declenche_une_seule_relance_avec_l_erreur_jointe(engine_test):
    """Sortie `{}` vide (déformation observée côté Analyst, reproduite ici
    sur Critic) : la validation échoue, UNE SEULE relance est tentée, avec
    le message d'erreur joint au prompt -- si la relance réussit, son
    résultat est renvoyé."""
    client, appels = _client_pret(engine_test, [
        _FauxReponse(input_outil={}),
        _FauxReponse(input_outil=CRITIC_VALIDE),
    ])

    resultat = client.appeler_structure(
        modele="claude-sonnet-5", prompt_systeme="s", prompt_utilisateur="u",
        schema=CriticSortie, version_prompt="v1", role="critic", opportunity_id="o1",
    )

    assert resultat is not None
    assert resultat.opportunity_id == "o1"
    assert len(appels) == 2
    # Le 2e appel joint bien l'erreur de validation au prompt d'origine.
    assert "u" in appels[1]["messages"][0]["content"]
    assert "erreur de validation" in appels[1]["messages"][0]["content"]

    lignes = _lignes_usage_events(engine_test)
    assert len(lignes) == 2
    assert lignes[0]["issue"] == ISSUE_RELANCEE  # 1ère tentative invalide, mais relancée
    assert lignes[1]["issue"] == ISSUE_VALIDE  # 2e tentative (la relance), valide


def test_sortie_invalide_deux_fois_de_suite_renvoie_none_et_journalise_perdue(engine_test):
    """Si la relance échoue ELLE AUSSI : jamais de repli silencieux au-delà
    -- `appeler_structure` renvoie `None`, et la DERNIÈRE tentative est
    journalisée `perdue` (la première reste `relancee`)."""
    client, appels = _client_pret(engine_test, [
        _FauxReponse(input_outil={}),
        _FauxReponse(input_outil={}),
    ])

    resultat = client.appeler_structure(
        modele="claude-sonnet-5", prompt_systeme="s", prompt_utilisateur="u",
        schema=CriticSortie, version_prompt="v1", role="critic", opportunity_id="o1",
    )

    assert resultat is None
    assert len(appels) == MAX_TENTATIVES == 2
    lignes = _lignes_usage_events(engine_test)
    assert [l["issue"] for l in lignes] == [ISSUE_RELANCEE, ISSUE_PERDUE]


def test_sortie_tronquee_est_journalisee_independamment_de_la_validation(engine_test):
    """`stop_reason == "max_tokens"` (sous-étape 3.10, point 3) est
    journalisé dans `sortie_tronquee`, MÊME quand la sortie valide quand même
    (cas limite : la coupure tombe juste après la fin du JSON utile) --
    n'empêche jamais de renvoyer le résultat à l'appelant."""
    client, appels = _client_pret(engine_test, [_FauxReponse(input_outil=CRITIC_VALIDE, stop_reason="max_tokens")])

    resultat = client.appeler_structure(
        modele="claude-sonnet-5", prompt_systeme="s", prompt_utilisateur="u",
        schema=CriticSortie, version_prompt="v1", role="critic", opportunity_id="o1", max_tokens=200,
    )

    assert resultat is not None
    assert len(appels) == 1
    ligne = _lignes_usage_events(engine_test)[0]
    assert ligne["sortie_tronquee"] is True
    assert ligne["issue"] == ISSUE_VALIDE


def test_sortie_valide_du_premier_coup_ne_relance_jamais(engine_test):
    client, appels = _client_pret(engine_test, [_FauxReponse(input_outil=CRITIC_VALIDE)])

    resultat = client.appeler_structure(
        modele="claude-sonnet-5", prompt_systeme="s", prompt_utilisateur="u",
        schema=CriticSortie, version_prompt="v1", role="critic", opportunity_id="o1",
    )

    assert resultat is not None
    assert len(appels) == 1
    ligne = _lignes_usage_events(engine_test)[0]
    assert ligne["issue"] == ISSUE_VALIDE
    assert ligne["sortie_tronquee"] is False


def test_outil_transmis_a_l_api_porte_strict_true(engine_test):
    """Sous-étape 3.10, point 2 : le dictionnaire `outil` passé à l'API porte
    `"strict": True` (SDK Anthropic >= 1.8, garantit la forme de la sortie
    au niveau de l'API elle-même)."""
    client, appels = _client_pret(engine_test, [_FauxReponse(input_outil=CRITIC_VALIDE)])

    client.appeler_structure(
        modele="claude-sonnet-5", prompt_systeme="s", prompt_utilisateur="u",
        schema=CriticSortie, version_prompt="v1", role="critic", opportunity_id="o1",
    )

    assert appels[0]["tools"][0]["strict"] is True


def test_aucun_tool_use_dans_la_reponse_relance_puis_perd(engine_test):
    """Absence totale de bloc `tool_use` (jamais observé en pratique avec
    `tool_choice` forcé + `strict`, mais possible en théorie) : traité comme
    une sortie invalide -- une relance, puis perdu si ça persiste."""
    client, appels = _client_pret(engine_test, [_FauxReponse(input_outil=None), _FauxReponse(input_outil=None)])

    resultat = client.appeler_structure(
        modele="claude-sonnet-5", prompt_systeme="s", prompt_utilisateur="u",
        schema=CriticSortie, version_prompt="v1", role="critic", opportunity_id="o1",
    )

    assert resultat is None
    assert len(appels) == 2
    lignes = _lignes_usage_events(engine_test)
    assert [l["issue"] for l in lignes] == [ISSUE_RELANCEE, ISSUE_PERDUE]
