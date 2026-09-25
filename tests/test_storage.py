from datetime import datetime, timezone

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
