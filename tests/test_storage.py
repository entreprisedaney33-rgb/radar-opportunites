from datetime import date, datetime, timezone

from app.storage import repo


def test_upsert_source_idempotent_meme_url_meme_empreinte(engine_test):
    id1, cree1 = repo.upsert_source(
        engine_test, url_canonique="https://exemple.invalid/a", domaine="exemple",
        date_publication=None, type_source="rss", extrait="texte", empreinte="abc",
        droits_collecte="test",
    )
    id2, cree2 = repo.upsert_source(
        engine_test, url_canonique="https://exemple.invalid/a", domaine="exemple",
        date_publication=None, type_source="rss", extrait="texte", empreinte="abc",
        droits_collecte="test",
    )
    assert id1 == id2
    assert cree1 is True
    assert cree2 is False


def test_scores_est_append_only_jamais_ecrase(engine_test):
    run_id = repo.creer_run(engine_test, mode="reel", version_code="t", version_config="t", quotas={})
    opp_id = repo.creer_opportunite(
        engine_test, titre="t", acheteur="a", probleme="p", mecanisme_ia="m",
        secteur="e_commerce", statut="nouveau", cluster_id=None,
    )
    repo.inserer_score(
        engine_test, opportunity_id=opp_id, run_id=run_id, version_poids="v1", valeurs={},
        score_brut=40.0, score_prudent=30.0, couverture_preuves=0.5, flags=[], decision_critic=None,
    )
    repo.inserer_score(
        engine_test, opportunity_id=opp_id, run_id=run_id, version_poids="v2", valeurs={},
        score_brut=70.0, score_prudent=60.0, couverture_preuves=0.8, flags=[], decision_critic="a_verifier",
    )
    historique = repo.historique_scores(engine_test, opp_id)
    assert len(historique) == 2
    assert [h["score_prudent"] for h in historique] == [30.0, 60.0]


def test_signal_deja_traite_detecte_une_source_deja_lue(engine_test):
    source_id, _ = repo.upsert_source(
        engine_test, url_canonique="https://exemple.invalid/b", domaine="exemple",
        date_publication=None, type_source="rss", extrait="texte", empreinte="xyz",
        droits_collecte="test",
    )
    assert repo.signal_deja_traite(engine_test, source_id) is False
    run_id = repo.creer_run(engine_test, mode="reel", version_code="t", version_config="t", quotas={})
    repo.inserer_signal(
        engine_test, source_id=source_id, run_id=run_id, texte_court="x", categorie="e_commerce",
        date_signal=datetime.now(timezone.utc), normalisation={},
    )
    assert repo.signal_deja_traite(engine_test, source_id) is True


def test_cout_total_jour_utc_additionne_tous_les_runs(engine_test):
    """Sous-étape 0.7 : la clé du plafond dur est le jour calendaire UTC,
    pas le run — deux runs différents le même jour comptent ensemble."""
    jour = datetime.now(timezone.utc).date()
    run1 = repo.creer_run(engine_test, mode="reel", version_code="t", version_config="t", quotas={})
    run2 = repo.creer_run(engine_test, mode="reel", version_code="t", version_config="t", quotas={})
    repo.inserer_usage_event(
        engine_test, run_id=run1, fournisseur="anthropic", modele_ou_actor="m", appels=1,
        tokens_in=10, tokens_out=10, cout=0.30, role="analyst",
    )
    repo.inserer_usage_event(
        engine_test, run_id=run2, fournisseur="anthropic", modele_ou_actor="m", appels=1,
        tokens_in=10, tokens_out=10, cout=0.20, role="critic",
    )
    assert repo.cout_total_jour_utc(engine_test, jour) == 0.50
    assert repo.cout_total_jour_utc(engine_test, date(1999, 1, 1)) == 0.0


def test_nombre_appels_approfondis_jour_utc_compte_analyst_et_critic_seulement(engine_test):
    jour = datetime.now(timezone.utc).date()
    run_id = repo.creer_run(engine_test, mode="reel", version_code="t", version_config="t", quotas={})
    for role in ("scout", "analyst", "critic", "analyst"):
        repo.inserer_usage_event(
            engine_test, run_id=run_id, fournisseur="anthropic", modele_ou_actor="m", appels=1,
            tokens_in=1, tokens_out=1, cout=0.001, role=role,
        )
    # Une ligne d'historique sans role (avant la migration) ne doit jamais compter.
    repo.inserer_usage_event(
        engine_test, run_id=run_id, fournisseur="anthropic", modele_ou_actor="m", appels=1,
        tokens_in=1, tokens_out=1, cout=0.001, role=None,
    )
    assert repo.nombre_appels_approfondis_jour_utc(engine_test, jour) == 3


def test_tirage_controle_une_seule_fois_par_opportunite(engine_test):
    opp_id = repo.creer_opportunite(
        engine_test, titre="t", acheteur="a", probleme="p", mecanisme_ia="m",
        secteur="e_commerce", statut="rejete", cluster_id=None,
    )
    run_id = repo.creer_run(engine_test, mode="reel", version_code="t", version_config="t", quotas={})

    assert repo.opportunites_deja_tirees_controle(engine_test) == set()
    repo.inserer_tirage_controle_rejete(
        engine_test, opportunity_id=opp_id, run_id=run_id,
        decision_avant="rejete", decision_apres="incertain",
    )
    assert repo.opportunites_deja_tirees_controle(engine_test) == {opp_id}

    # Un deuxième tirage sur la MÊME opportunité viole la contrainte d'unicité.
    import pytest
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        repo.inserer_tirage_controle_rejete(
            engine_test, opportunity_id=opp_id, run_id=run_id,
            decision_avant="rejete", decision_apres="rejete",
        )
