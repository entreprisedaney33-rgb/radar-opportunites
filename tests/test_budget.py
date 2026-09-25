import pytest

from app.pipeline.budget import BudgetDepasse, BudgetTracker
from app.storage import repo


def _tracker(engine, run_id, *, plafond_eur=1.0, plafond_appels=1000,
             plafond_requetes_recherche=600, plafond_fetchs_pages=400):
    return BudgetTracker(
        engine, run_id, plafond_eur=plafond_eur, plafond_appels_approfondis=plafond_appels,
        plafond_requetes_recherche_par_jour=plafond_requetes_recherche,
        plafond_fetchs_pages_par_jour=plafond_fetchs_pages,
    )


def test_engager_au_dela_du_plafond_leve_budget_depasse(engine_test):
    run_id = repo.creer_run(engine_test, mode="reel", version_code="test", version_config="test", quotas={})
    tracker = _tracker(engine_test, run_id, plafond_eur=1.0)
    tracker.verifier_et_engager(0.6, role="analyst")
    with pytest.raises(BudgetDepasse):
        tracker.verifier_et_engager(0.6, role="analyst")


def test_enregistrer_reel_persiste_et_solde_restant_reflete_la_base(engine_test):
    run_id = repo.creer_run(engine_test, mode="reel", version_code="test", version_config="test", quotas={})
    tracker = _tracker(engine_test, run_id, plafond_eur=5.0)
    tracker.verifier_et_engager(1.0, role="analyst")
    tracker.enregistrer_reel(
        fournisseur="anthropic", modele_ou_actor="claude-haiku-4-5-20251001", appels=1,
        tokens_in=100, tokens_out=50, cout_reel=0.3, cout_estime_engage=1.0,
        role="analyst", opportunity_id="opp1",
    )
    assert tracker.solde_restant() == pytest.approx(5.0 - 0.3)
    assert tracker.cout_total_reel() == pytest.approx(0.3)


def test_second_appel_refuse_si_solde_insuffisant_apres_ajustement(engine_test):
    run_id = repo.creer_run(engine_test, mode="reel", version_code="test", version_config="test", quotas={})
    tracker = _tracker(engine_test, run_id, plafond_eur=1.0)
    tracker.verifier_et_engager(0.9, role="analyst")
    tracker.enregistrer_reel(
        fournisseur="anthropic", modele_ou_actor="m", appels=1, tokens_in=10, tokens_out=10,
        cout_reel=0.95, cout_estime_engage=0.9, role="analyst",
    )
    with pytest.raises(BudgetDepasse):
        tracker.verifier_et_engager(0.1, role="analyst")


def test_plafond_journalier_porte_sur_tous_les_runs_du_jour(engine_test):
    """Sous-étape 0.7 — cœur de la correction : deux runs DIFFÉRENTS le même
    jour UTC doivent partager le même plafond, pas repartir de zéro chacun
    (voir rapports/DIAGNOSTIC_BUDGET_2026-09-25.md)."""
    run1 = repo.creer_run(engine_test, mode="reel", version_code="test", version_config="test", quotas={})
    tracker1 = _tracker(engine_test, run1, plafond_eur=1.0)
    tracker1.verifier_et_engager(0.7, role="analyst")
    tracker1.enregistrer_reel(
        fournisseur="anthropic", modele_ou_actor="m", appels=1, tokens_in=10, tokens_out=10,
        cout_reel=0.7, cout_estime_engage=0.7, role="analyst",
    )

    # Un DEUXIÈME run (ex. redémarrage du worker) le même jour : le plafond
    # doit tenir compte de ce que le premier run a déjà dépensé.
    run2 = repo.creer_run(engine_test, mode="reel", version_code="test", version_config="test", quotas={})
    tracker2 = _tracker(engine_test, run2, plafond_eur=1.0)
    with pytest.raises(BudgetDepasse):
        tracker2.verifier_et_engager(0.4, role="analyst")  # 0.7 + 0.4 > 1.0


def test_verifier_et_engager_relit_la_base_a_chaque_appel(engine_test):
    """Pas de compteur local figé à l'initialisation : un coût inséré par un
    AUTRE tracker (même run ou non) doit être vu immédiatement."""
    run_id = repo.creer_run(engine_test, mode="reel", version_code="test", version_config="test", quotas={})
    tracker = _tracker(engine_test, run_id, plafond_eur=1.0)
    tracker.verifier_et_engager(0.1, role="analyst")  # ne lève rien, solde encore large

    # Une dépense apparaît en base "par ailleurs" (autre process, autre run).
    repo.inserer_usage_event(
        engine_test, run_id=run_id, fournisseur="anthropic", modele_ou_actor="m", appels=1,
        tokens_in=10, tokens_out=10, cout=0.95, role="analyst",
    )

    with pytest.raises(BudgetDepasse):
        tracker.verifier_et_engager(0.1, role="analyst")


def test_plafond_appels_approfondis_independant_du_prix(engine_test):
    """Second garde-fou : même avec un solde € très large, le nombre
    d'appels Analyst/Critic du jour ne dépasse jamais le plafond configuré."""
    run_id = repo.creer_run(engine_test, mode="reel", version_code="test", version_config="test", quotas={})
    tracker = _tracker(engine_test, run_id, plafond_eur=1000.0, plafond_appels=2)

    tracker.verifier_et_engager(0.001, role="analyst")
    tracker.enregistrer_reel(
        fournisseur="anthropic", modele_ou_actor="m", appels=1, tokens_in=1, tokens_out=1,
        cout_reel=0.001, cout_estime_engage=0.001, role="analyst",
    )
    tracker.verifier_et_engager(0.001, role="critic")
    tracker.enregistrer_reel(
        fournisseur="anthropic", modele_ou_actor="m", appels=1, tokens_in=1, tokens_out=1,
        cout_reel=0.001, cout_estime_engage=0.001, role="critic",
    )

    with pytest.raises(BudgetDepasse):
        tracker.verifier_et_engager(0.001, role="analyst")


def test_plafond_appels_approfondis_ne_compte_pas_le_scout(engine_test):
    """Le Scout utilise le modèle de tri, bien moins cher — il ne doit
    jamais consommer le plafond d'appels approfondis."""
    run_id = repo.creer_run(engine_test, mode="reel", version_code="test", version_config="test", quotas={})
    tracker = _tracker(engine_test, run_id, plafond_eur=1000.0, plafond_appels=1)

    for _ in range(5):
        tracker.verifier_et_engager(0.001, role="scout")
        tracker.enregistrer_reel(
            fournisseur="anthropic", modele_ou_actor="m", appels=1, tokens_in=1, tokens_out=1,
            cout_reel=0.001, cout_estime_engage=0.001, role="scout",
        )
    # Toujours aucun appel "approfondi" engagé -> le premier passe encore.
    tracker.verifier_et_engager(0.001, role="analyst")


def test_plafond_requetes_recherche_par_jour_independant_du_budget_eur(engine_test):
    """Sous-étape 3.1 : les compteurs de l'Enquêteur sont indépendants du
    budget en euros — gratuits en V1 (sous-étape 3.2), un solde € très large
    ne doit jamais les contourner."""
    run_id = repo.creer_run(engine_test, mode="reel", version_code="test", version_config="test", quotas={})
    tracker = _tracker(engine_test, run_id, plafond_eur=1000.0, plafond_requetes_recherche=2)

    tracker.verifier_et_engager_requete_recherche()
    tracker.enregistrer_requete_recherche(fournisseur="hn_algolia")
    tracker.verifier_et_engager_requete_recherche()
    tracker.enregistrer_requete_recherche(fournisseur="hn_algolia")

    with pytest.raises(BudgetDepasse):
        tracker.verifier_et_engager_requete_recherche()


def test_plafond_fetchs_pages_par_jour_independant_du_plafond_requetes(engine_test):
    run_id = repo.creer_run(engine_test, mode="reel", version_code="test", version_config="test", quotas={})
    tracker = _tracker(engine_test, run_id, plafond_requetes_recherche=1, plafond_fetchs_pages=1)

    tracker.verifier_et_engager_requete_recherche()
    tracker.enregistrer_requete_recherche(fournisseur="reddit")
    # Le plafond des requêtes est atteint, mais celui des fetchs est distinct.
    tracker.verifier_et_engager_fetch_page()
    tracker.enregistrer_fetch_page(fournisseur="reddit")

    with pytest.raises(BudgetDepasse):
        tracker.verifier_et_engager_fetch_page()


def test_requetes_recherche_partagent_le_plafond_journalier_entre_deux_runs(engine_test):
    """Même logique que le budget en euros (0.7) : le plafond porte sur la
    journée UTC entière, tous runs confondus, pas sur un seul run."""
    run1 = repo.creer_run(engine_test, mode="reel", version_code="test", version_config="test", quotas={})
    tracker1 = _tracker(engine_test, run1, plafond_requetes_recherche=1)
    tracker1.verifier_et_engager_requete_recherche()
    tracker1.enregistrer_requete_recherche(fournisseur="hn_algolia")

    run2 = repo.creer_run(engine_test, mode="reel", version_code="test", version_config="test", quotas={})
    tracker2 = _tracker(engine_test, run2, plafond_requetes_recherche=1)
    with pytest.raises(BudgetDepasse):
        tracker2.verifier_et_engager_requete_recherche()


def test_enregistrer_requete_recherche_et_fetch_page_persistent_avec_cout_zero(engine_test):
    run_id = repo.creer_run(engine_test, mode="reel", version_code="test", version_config="test", quotas={})
    tracker = _tracker(engine_test, run_id)

    tracker.verifier_et_engager_requete_recherche()
    tracker.enregistrer_requete_recherche(fournisseur="hn_algolia")
    tracker.verifier_et_engager_fetch_page()
    tracker.enregistrer_fetch_page(fournisseur="magasin_interne")

    assert tracker.requetes_recherche_jour_engagees() == 1
    assert tracker.fetchs_pages_jour_engages() == 1
    # Gratuit en V1 : ne consomme jamais le budget en euros.
    assert tracker.depense_jour_engagee() == pytest.approx(0.0)
