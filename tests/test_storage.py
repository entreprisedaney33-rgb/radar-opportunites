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


def test_upsert_source_trace_les_requetes_multiples_sans_dupliquer_la_source(engine_test):
    """Sous-étape 1.4, point 1 : un même post (même URL canonique, même
    empreinte de contenu) remonté par 3 requêtes de recherche différentes
    (Reddit et/ou HN) ne crée toujours qu'UNE SEULE source — donc jamais plus
    d'une opportunité, la logique de dédoublonnage existante étant
    inchangée — mais les 3 requêtes qui l'ont retrouvé restent toutes
    tracées."""
    id1, cree1 = repo.upsert_source(
        engine_test, url_canonique="https://exemple.invalid/post", domaine="exemple",
        date_publication=None, type_source="rss", extrait="texte", empreinte="abc",
        droits_collecte="test", flux_origine="Reddit r/smallbusiness — recherche « manually »",
        requete_origine="manually",
    )
    id2, cree2 = repo.upsert_source(
        engine_test, url_canonique="https://exemple.invalid/post", domaine="exemple",
        date_publication=None, type_source="rss", extrait="texte", empreinte="abc",
        droits_collecte="test", flux_origine="Reddit r/smallbusiness — recherche « spreadsheet »",
        requete_origine="spreadsheet",
    )
    id3, cree3 = repo.upsert_source(
        engine_test, url_canonique="https://exemple.invalid/post", domaine="exemple",
        date_publication=None, type_source="rss", extrait="texte", empreinte="abc",
        droits_collecte="test", flux_origine="Hacker News — recherche « manually » (Ask HN)",
        requete_origine="manually",
    )
    # Rejouer EXACTEMENT la même requête (même flux, même texte) ne doit pas
    # dupliquer la trace non plus (idempotence, ex. reprise après panne).
    id4, cree4 = repo.upsert_source(
        engine_test, url_canonique="https://exemple.invalid/post", domaine="exemple",
        date_publication=None, type_source="rss", extrait="texte", empreinte="abc",
        droits_collecte="test", flux_origine="Reddit r/smallbusiness — recherche « manually »",
        requete_origine="manually",
    )

    assert id1 == id2 == id3 == id4
    assert (cree1, cree2, cree3, cree4) == (True, False, False, False)

    requetes = repo.requetes_pour_source(engine_test, id1)
    assert len(requetes) == 3  # jamais 4 : la requête rejouée ne duplique rien
    assert {r["requete_origine"] for r in requetes} == {"manually", "spreadsheet"}
    assert sum(1 for r in requetes if r["requete_origine"] == "manually") == 2  # une fois par flux distinct


def test_upsert_source_sans_requete_origine_ne_cree_aucune_trace(engine_test):
    """Les flux qui ne sont pas des recherches (RSS frontpage, démo...) n'ont
    pas de `requete_origine` : aucune ligne `source_requetes` ne doit être
    créée pour eux (rien à tracer, et ça éviterait une croissance illimitée
    de la table pour un flux revisité à chaque passage)."""
    source_id, _ = repo.upsert_source(
        engine_test, url_canonique="https://exemple.invalid/rss", domaine="exemple",
        date_publication=None, type_source="rss", extrait="texte", empreinte="def",
        droits_collecte="test", flux_origine="TechCrunch",
    )

    assert repo.requetes_pour_source(engine_test, source_id) == []


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


def test_creer_opportunite_sans_provenance_laisse_les_champs_2_1_a_null(engine_test):
    """Compatibilité : les deux nouveaux paramètres de la sous-étape 2.1
    sont optionnels — un appelant qui ne les passe pas (comme avant cette
    sous-étape) ne doit jamais planter, et obtient NULL, pas une valeur
    inventée."""
    opp_id = repo.creer_opportunite(
        engine_test, titre="t", acheteur="a", probleme="p", mecanisme_ia="m",
        secteur="e_commerce", statut="nouveau", cluster_id=None,
    )
    ouvertes = {o["id"]: o for o in repo.lister_opportunites_ouvertes(engine_test)}
    assert ouvertes[opp_id]["secteur_provenance"] is None
    assert ouvertes[opp_id]["secteur_citation"] is None


def test_creer_opportunite_persiste_la_provenance_et_la_citation_du_secteur(engine_test):
    opp_id = repo.creer_opportunite(
        engine_test, titre="t", acheteur="a", probleme="p", mecanisme_ia="m",
        secteur="flux_documentaires", statut="nouveau", cluster_id=None,
        secteur_provenance="citation_verifiee", secteur_citation="rapprochement bancaire à la main",
    )
    ouvertes = {o["id"]: o for o in repo.lister_opportunites_ouvertes(engine_test)}
    assert ouvertes[opp_id]["secteur_provenance"] == "citation_verifiee"
    assert ouvertes[opp_id]["secteur_citation"] == "rapprochement bancaire à la main"


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
