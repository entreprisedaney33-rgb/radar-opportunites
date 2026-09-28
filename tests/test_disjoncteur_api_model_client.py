"""Sous-étape 3.13 (AMELIORATIONS.md) : branchement réel dans `ModelClient` --
correctif du schéma strict (cause vérifiée de la panne du 26/09/2026), coût
compté même en échec réseau/API, et disjoncteur après échecs consécutifs.
Client Anthropic simulé (aucun réseau), même harnais que
`tests/test_model_client_appeler_structure.py`."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select

from app.adapters.model_client import (
    ISSUE_PERDUE,
    DisjoncteurAPIOuvert,
    ModelClient,
)
from app.config import Settings
from app.models_schemas import CriticSortie
from app.pipeline import disjoncteur_api
from app.pipeline.budget import BudgetTracker
from app.storage import repo
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


class _ExceptionOuReponse:
    """Simule soit une exception réseau/API (comme la vraie panne du
    26/09/2026 : `client.messages.create()` lève avant toute réponse), soit
    une réponse normale."""

    def __init__(self, *, appels: list, sequence: list):
        self._appels = appels
        self._sequence = iter(sequence)

    def create(self, **kwargs):
        self._appels.append(kwargs)
        item = next(self._sequence)
        if isinstance(item, Exception):
            raise item
        return item


class _FauxClient:
    def __init__(self, sequence):
        self.appels: list = []
        self.messages = _ExceptionOuReponse(appels=self.appels, sequence=sequence)


def _client_pret(engine, sequence) -> tuple[ModelClient, list]:
    budget = BudgetTracker(engine, "run-test", plafond_eur=25.0, plafond_appels_approfondis=1000)
    client = ModelClient(_settings(), budget)
    faux = _FauxClient(sequence)
    client._client = faux
    return client, faux.appels


def _lignes_usage_events(engine) -> list[dict]:
    with engine.connect() as cx:
        rows = cx.execute(select(usage_events).order_by(usage_events.c.date_creation)).mappings().all()
    return [dict(r) for r in rows]


CRITIC_VALIDE = {
    "opportunity_id": "o1", "objections": [], "faits_contestes": [],
    "recherches_supplementaires": [], "decision": "a_verifier", "motif": "ok",
}


def test_input_schema_transmis_porte_additional_properties_false():
    """Cause vérifiée de la panne du 26/09/2026 (logs Render, voir
    rapports/POINT_ETAPE_2026-09-27.md) : l'API rejetait tout appel en 400
    ("additionalProperties must be explicitly set to false") -- corrigé par
    `app.adapters.schema_strict.rendre_schema_strict`."""
    engine = create_engine("sqlite:///:memory:", future=True)
    migrer(engine)
    client, appels = _client_pret(engine, [_FauxReponse(input_outil=CRITIC_VALIDE)])

    client.appeler_structure(
        modele="claude-sonnet-5", prompt_systeme="s", prompt_utilisateur="u",
        schema=CriticSortie, version_prompt="v1", role="critic", opportunity_id="o1",
    )

    schema_envoye = appels[0]["tools"][0]["input_schema"]
    assert schema_envoye["additionalProperties"] is False
    assert set(schema_envoye["required"]) == set(schema_envoye["properties"].keys())


def test_echec_reseau_compte_le_cout_estime_jamais_zero(engine_test):
    """Sous-étape 3.13, point 4 : le bug réel de la panne -- une exception
    à l'appel (aucune réponse reçue) journalisait 0 € dans `usage_events`,
    empêchant le plafond de 25 €/jour de jamais se remplir (voir Journal de
    cette sous-étape). Corrigé : le coût ESTIMÉ avant appel est compté."""
    client, appels = _client_pret(engine_test, [ConnectionError("panne réseau simulée")])

    resultat = client.appeler_structure(
        modele="claude-sonnet-5", prompt_systeme="s" * 40, prompt_utilisateur="u" * 40,
        schema=CriticSortie, version_prompt="v1", role="critic", opportunity_id="o1",
    )

    assert resultat is None
    ligne = _lignes_usage_events(engine_test)[0]
    assert ligne["issue"] == ISSUE_PERDUE
    assert ligne["cout_declare_ou_estime"] > 0.0
    assert ligne["tokens_in"] is None and ligne["tokens_out"] is None


def test_cinq_echecs_consecutifs_ouvrent_le_disjoncteur_et_bloquent_le_sixieme(engine_test):
    sequence = [ConnectionError("panne") for _ in range(disjoncteur_api.SEUIL_ECHECS_CONSECUTIFS)]
    client, appels = _client_pret(engine_test, sequence)

    for _ in range(disjoncteur_api.SEUIL_ECHECS_CONSECUTIFS):
        resultat = client.appeler_structure(
            modele="claude-sonnet-5", prompt_systeme="s", prompt_utilisateur="u",
            schema=CriticSortie, version_prompt="v1", role="critic", opportunity_id="o1",
        )
        assert resultat is None

    nb_appels_reels_avant = len(appels)

    with pytest.raises(DisjoncteurAPIOuvert):
        client.appeler_structure(
            modele="claude-sonnet-5", prompt_systeme="s", prompt_utilisateur="u",
            schema=CriticSortie, version_prompt="v1", role="critic", opportunity_id="o1",
        )

    # Le 6e appel n'a JAMAIS atteint le client Anthropic -- bloqué en amont.
    assert len(appels) == nb_appels_reels_avant

    etat = repo.lire_disjoncteur_api(engine_test)
    assert etat["en_erreur"] is True
    assert etat["depuis"] is not None


def test_un_succes_apres_des_echecs_referme_le_disjoncteur(engine_test):
    sequence = [ConnectionError("panne") for _ in range(disjoncteur_api.SEUIL_ECHECS_CONSECUTIFS - 1)]
    sequence.append(_FauxReponse(input_outil=CRITIC_VALIDE))
    client, appels = _client_pret(engine_test, sequence)

    for _ in range(disjoncteur_api.SEUIL_ECHECS_CONSECUTIFS - 1):
        client.appeler_structure(
            modele="claude-sonnet-5", prompt_systeme="s", prompt_utilisateur="u",
            schema=CriticSortie, version_prompt="v1", role="critic", opportunity_id="o1",
        )

    resultat = client.appeler_structure(
        modele="claude-sonnet-5", prompt_systeme="s", prompt_utilisateur="u",
        schema=CriticSortie, version_prompt="v1", role="critic", opportunity_id="o1",
    )
    assert resultat is not None

    etat = repo.lire_disjoncteur_api(engine_test)
    assert etat is None or etat["en_erreur"] is False

    # Le disjoncteur étant fermé, un nouvel appel n'est jamais bloqué.
    client2, appels2 = _client_pret(engine_test, [_FauxReponse(input_outil=CRITIC_VALIDE)])
    resultat2 = client2.appeler_structure(
        modele="claude-sonnet-5", prompt_systeme="s", prompt_utilisateur="u",
        schema=CriticSortie, version_prompt="v1", role="critic", opportunity_id="o1",
    )
    assert resultat2 is not None
    assert len(appels2) == 1


def test_disjoncteur_ouvert_persiste_entre_deux_model_client_distincts(engine_test):
    """L'état vit en base (`etats_disjoncteur_api`), pas en mémoire de
    l'instance -- un NOUVEAU `ModelClient` (comme à chaque passage du
    Background Worker, voir `app/pipeline/orchestrator.py`) voit le même
    disjoncteur ouvert."""
    sequence = [ConnectionError("panne") for _ in range(disjoncteur_api.SEUIL_ECHECS_CONSECUTIFS)]
    client1, _ = _client_pret(engine_test, sequence)
    for _ in range(disjoncteur_api.SEUIL_ECHECS_CONSECUTIFS):
        client1.appeler_structure(
            modele="claude-sonnet-5", prompt_systeme="s", prompt_utilisateur="u",
            schema=CriticSortie, version_prompt="v1", role="critic", opportunity_id="o1",
        )

    client2, appels2 = _client_pret(engine_test, [_FauxReponse(input_outil=CRITIC_VALIDE)])
    with pytest.raises(DisjoncteurAPIOuvert):
        client2.appeler_structure(
            modele="claude-sonnet-5", prompt_systeme="s", prompt_utilisateur="u",
            schema=CriticSortie, version_prompt="v1", role="critic", opportunity_id="o1",
        )
    assert len(appels2) == 0
