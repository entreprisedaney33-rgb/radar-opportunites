"""Sous-étape 3.13 (AMELIORATIONS.md) : deux propriétés du pipeline face au
disjoncteur de l'appel au modèle (`app.pipeline.disjoncteur_api`) --

1. Quand il est ouvert, `app.pipeline.orchestrator` s'arrête pour le passage
   plutôt que de laisser un rôle retomber sur son repli heuristique (« aucun
   dossier n'est créé ni analysé par repli pendant cet état »).
2. `_phase_reprise` retraite en priorité les dossiers `a_reprendre` (posés
   par `app.reprise`) via un VRAI Scout, dès que le modèle répond.

Client Anthropic jamais atteint ici -- `app.pipeline.orchestrator.ModelClient`
est remplacé par un double, même pattern que
`tests/test_pipeline_integration.py`."""
from __future__ import annotations

import time

import pytest

from app import config as cfg
from app.adapters.model_client import AccesModeleIndisponible, DisjoncteurAPIOuvert
from app.models_schemas import ScoutSortie
from app.pipeline.orchestrator import OptionsRun, ResumeRun, _phase_reprise, executer_run
from app.storage import repo


class _FauxModelClientDisjoncteurSurScout:
    def __init__(self, *a, **kw):
        pass

    def appeler_structure(self, **kwargs):
        if kwargs.get("role") == "scout":
            raise DisjoncteurAPIOuvert("400 additionalProperties (simulé)")
        raise AccesModeleIndisponible("hors périmètre de ce test")


class _FauxModelClientDisjoncteurSurAnalyst:
    def __init__(self, *a, **kw):
        pass

    def appeler_structure(self, **kwargs):
        if kwargs.get("role") == "analyst":
            raise DisjoncteurAPIOuvert("400 additionalProperties (simulé)")
        raise AccesModeleIndisponible("hors périmètre de ce test")


def _avec_cle_api(monkeypatch, faux_model_client_cls):
    monkeypatch.setattr("app.pipeline.orchestrator.ModelClient", faux_model_client_cls)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "cle-de-test")
    cfg.get_settings.cache_clear()


def test_disjoncteur_ouvert_pendant_la_collecte_ne_cree_aucune_opportunite(engine_test, monkeypatch):
    _avec_cle_api(monkeypatch, _FauxModelClientDisjoncteurSurScout)
    try:
        run_id, resume = executer_run(
            engine_test, OptionsRun(mode="reel", forcer_demo=True, max_signaux=1, max_analyses=1),
        )
    finally:
        cfg.get_settings.cache_clear()

    assert resume.api_en_erreur is True
    # Jamais de repli heuristique déguisé en dossier réel pendant l'incident.
    assert repo.lister_opportunites_ouvertes(engine_test) == []
    run = repo.get_run(engine_test, run_id)
    assert run["statut"] == "interrompu"


def test_disjoncteur_ouvert_pendant_l_analyse_laisse_le_dossier_intact(engine_test, monkeypatch):
    opportunity_id = repo.creer_opportunite(
        engine_test, titre="t", acheteur="a", probleme="p", mecanisme_ia="m",
        secteur="intersectoriel", statut="enquete_terminee", cluster_id=None,
    )
    _avec_cle_api(monkeypatch, _FauxModelClientDisjoncteurSurAnalyst)
    try:
        executer_run(engine_test, OptionsRun(mode="reel", forcer_demo=True, max_signaux=0, max_analyses=1))
    finally:
        cfg.get_settings.cache_clear()

    dossier = repo.lister_opportunites_ouvertes(engine_test)[0]
    assert dossier["id"] == opportunity_id
    assert dossier["statut"] == "enquete_terminee"  # jamais passé par un repli Analyst/Critic


def _preparer_dossier_a_reprendre(engine):
    source_id, _ = repo.upsert_source(
        engine, url_canonique="https://exemple.invalid/1", domaine="exemple.invalid",
        date_publication=None, type_source="rss", extrait="Je passe 6h/semaine sur ce rapprochement à la main.",
        empreinte="empreinte-1", droits_collecte="public",
    )
    opportunity_id = repo.creer_opportunite(
        engine, titre="titre heuristique", acheteur="à confirmer", probleme="p", mecanisme_ia="m",
        secteur="intersectoriel", statut="a_reprendre", cluster_id=None,
    )
    repo.inserer_assessment(
        engine, opportunity_id=opportunity_id, run_id="run-origine", role="scout",
        payload={}, modele="heuristique", version_prompt="scout-v1", inconnues=[],
    )
    repo.inserer_evidence(
        engine, opportunity_id=opportunity_id, source_id=source_id,
        claim="Scout: Je passe 6h/semaine sur ce rapprochement à la main.", type_="hypothese", independant=True,
    )
    return opportunity_id, source_id


def test_phase_reprise_avec_modele_disponible_repasse_le_dossier_a_nouveau(engine_test):
    opportunity_id, source_id = _preparer_dossier_a_reprendre(engine_test)

    class _FauxModelClientSucces:
        def appeler_structure(self, **kwargs):
            return ScoutSortie(
                opportunity_candidate="Rapprochement bancaire automatisé",
                buyer="PME avec compta interne", pain="6h/semaine de rapprochement manuel",
                ai_mechanism="agent de rapprochement automatique", why_now="coût du temps humain",
                signal_ids=[source_id],
            )

    run_id = repo.creer_run(engine_test, mode="reel", version_code="t", version_config="t", quotas={})
    resume = ResumeRun()
    _phase_reprise(
        engine_test, run_id, quotas=cfg.quotas(), settings=cfg.get_settings(),
        model_client=_FauxModelClientSucces(), resume=resume, debut=time.monotonic(), duree_max=999.0,
    )

    assert resume.reprises_terminees == 1
    dossier = repo.lister_opportunites_ouvertes(engine_test)[0]
    assert dossier["id"] == opportunity_id
    assert dossier["statut"] == "nouveau"
    assert dossier["titre"] == "Rapprochement bancaire automatisé"


def test_phase_reprise_avec_disjoncteur_ouvert_laisse_le_dossier_a_reprendre(engine_test):
    opportunity_id, _ = _preparer_dossier_a_reprendre(engine_test)

    class _FauxModelClientDisjoncteur:
        def appeler_structure(self, **kwargs):
            raise DisjoncteurAPIOuvert("400 additionalProperties (simulé)")

    run_id = repo.creer_run(engine_test, mode="reel", version_code="t", version_config="t", quotas={})
    resume = ResumeRun()
    _phase_reprise(
        engine_test, run_id, quotas=cfg.quotas(), settings=cfg.get_settings(),
        model_client=_FauxModelClientDisjoncteur(), resume=resume, debut=time.monotonic(), duree_max=999.0,
    )

    assert resume.api_en_erreur is True
    assert resume.reprises_terminees == 0
    dossier = repo.lister_opportunites_ouvertes(engine_test)[0]
    assert dossier["id"] == opportunity_id
    assert dossier["statut"] == "a_reprendre"  # jamais un second repli


def test_phase_reprise_sans_acces_modele_ne_touche_a_rien(engine_test):
    opportunity_id, _ = _preparer_dossier_a_reprendre(engine_test)
    run_id = repo.creer_run(engine_test, mode="reel", version_code="t", version_config="t", quotas={})
    resume = ResumeRun()
    _phase_reprise(
        engine_test, run_id, quotas=cfg.quotas(), settings=cfg.get_settings(),
        model_client=None, resume=resume, debut=time.monotonic(), duree_max=999.0,
    )
    assert resume.reprises_terminees == 0
    assert repo.lister_opportunites_ouvertes(engine_test)[0]["statut"] == "a_reprendre"
