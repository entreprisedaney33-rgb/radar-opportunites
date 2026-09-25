"""Tests de bout en bout du pipeline, en mode démo (aucun réseau, aucun
appel modèle), qui prouvent les propriétés exigées par le §7 (Phase 2) :
pas de doublon sur reprise, PAUSE_ALL arrête tout immédiatement."""
import pytest

from app.pipeline.orchestrator import ArretPause, OptionsRun, executer_run
from app.storage import repo


def test_run_demo_traite_tous_les_signaux_sans_appel_modele(engine_test):
    run_id, resume = executer_run(engine_test, OptionsRun(mode="dry-run", forcer_demo=True, max_signaux=6, max_analyses=2))

    run = repo.get_run(engine_test, run_id)
    assert run["statut"] == "termine"
    assert resume.signaux_lus == 6
    assert resume.opportunites_nouvelles + resume.opportunites_fusionnees == 6
    assert resume.analyses_terminees <= 2
    # Mode démo, sans clé modèle : aucun coût ne peut avoir été engagé.
    assert run["couts_json"]["total_eur_estime"] == 0.0


def test_reprise_ne_double_pas_les_dossiers(engine_test):
    """Relancer le pipeline sur la même base ne doit RIEN retraiter : les
    mêmes signaux de démo ont la même URL et la même empreinte de contenu à
    chaque appel, donc `signal_deja_traite` doit tous les faire ignorer."""
    executer_run(engine_test, OptionsRun(mode="dry-run", forcer_demo=True, max_signaux=6, max_analyses=2))
    _, resume2 = executer_run(engine_test, OptionsRun(mode="dry-run", forcer_demo=True, max_signaux=6, max_analyses=2))

    assert resume2.signaux_lus == 0
    assert resume2.signaux_deja_vus_ignores == 6
    assert resume2.opportunites_nouvelles == 0
    assert resume2.opportunites_fusionnees == 0


def test_budget_depasse_interrompt_le_run_proprement(engine_test, monkeypatch):
    """Un budget épuisé doit arrêter la collecte EN COURS, pas planter le
    run ni continuer à dépenser."""
    from app.pipeline.budget import BudgetDepasse

    class FauxModelClient:
        def __init__(self, *a, **kw):
            pass

        def appeler_structure(self, **kwargs):
            raise BudgetDepasse("plafond de test dépassé")

    monkeypatch.setattr("app.pipeline.orchestrator.ModelClient", FauxModelClient)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "cle-de-test")
    from app import config as cfg

    cfg.get_settings.cache_clear()
    try:
        run_id, resume = executer_run(
            engine_test, OptionsRun(mode="reel", forcer_demo=True, max_signaux=6, max_analyses=2),
        )
    finally:
        cfg.get_settings.cache_clear()

    assert resume.budget_atteint is True
    run = repo.get_run(engine_test, run_id)
    assert run["statut"] == "interrompu"
    # Le run s'est arrêté au tout premier signal : aucune opportunité créée.
    assert resume.opportunites_nouvelles == 0


def test_pause_all_bloque_immediatement(engine_test, monkeypatch):
    from app import config as cfg

    monkeypatch.setenv("RADAR_PAUSE_ALL", "1")
    cfg.get_settings.cache_clear()
    try:
        with pytest.raises(ArretPause):
            executer_run(engine_test, OptionsRun(mode="dry-run", forcer_demo=True))
    finally:
        cfg.get_settings.cache_clear()
