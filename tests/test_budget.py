import pytest

from app.pipeline.budget import BudgetDepasse, BudgetTracker
from app.storage import repo


def test_engager_au_dela_du_plafond_leve_budget_depasse(engine_test):
    run_id = repo.creer_run(engine_test, mode="reel", version_code="test", version_config="test", quotas={})
    tracker = BudgetTracker(engine_test, run_id, plafond_eur=1.0)
    tracker.verifier_et_engager(0.6)
    with pytest.raises(BudgetDepasse):
        tracker.verifier_et_engager(0.6)


def test_enregistrer_reel_ajuste_engagement_et_persiste(engine_test):
    run_id = repo.creer_run(engine_test, mode="reel", version_code="test", version_config="test", quotas={})
    tracker = BudgetTracker(engine_test, run_id, plafond_eur=5.0)
    tracker.verifier_et_engager(1.0)
    tracker.enregistrer_reel(
        fournisseur="anthropic", modele_ou_actor="claude-haiku-4-5-20251001", appels=1,
        tokens_in=100, tokens_out=50, cout_reel=0.3, cout_estime_engage=1.0,
    )
    assert tracker.solde_restant() == pytest.approx(5.0 - 0.3)
    assert tracker.cout_total_reel() == pytest.approx(0.3)


def test_second_appel_refuse_si_solde_insuffisant_apres_ajustement(engine_test):
    run_id = repo.creer_run(engine_test, mode="reel", version_code="test", version_config="test", quotas={})
    tracker = BudgetTracker(engine_test, run_id, plafond_eur=1.0)
    tracker.verifier_et_engager(0.9)
    tracker.enregistrer_reel(
        fournisseur="anthropic", modele_ou_actor="m", appels=1, tokens_in=10, tokens_out=10,
        cout_reel=0.95, cout_estime_engage=0.9,
    )
    with pytest.raises(BudgetDepasse):
        tracker.verifier_et_engager(0.1)
