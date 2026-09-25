from datetime import date, datetime, timezone

from sqlalchemy import insert

from app.metriques import calculer_metriques, formater_comparaison, main
from app.storage.schema import opportunities, opportunity_evidence, scores, sources, usage_events

JOUR = date(2026, 1, 15)
JOUR_AUTRE = date(2026, 1, 16)


def _dt(jour: date, heure: int = 12) -> datetime:
    return datetime(jour.year, jour.month, jour.day, heure, tzinfo=timezone.utc)


def _creer_opportunite(engine, id_, *, jour, statut, secteur):
    with engine.begin() as cx:
        cx.execute(insert(opportunities).values(
            id=id_, titre=f"titre {id_}", acheteur="acheteur", probleme="probleme",
            mecanisme_ia="mecanisme", secteur=secteur, statut=statut, cluster_id=None,
            date_creation=_dt(jour), date_maj=_dt(jour),
        ))


def _creer_source(engine, id_):
    with engine.begin() as cx:
        cx.execute(insert(sources).values(
            id=id_, url_canonique=f"https://exemple.invalid/{id_}", domaine="exemple",
            date_publication=None, date_collecte=_dt(JOUR), type="rss",
            extrait="extrait", empreinte=id_, droits_collecte="test",
        ))


def _rattacher_preuve(engine, opportunity_id, source_id):
    with engine.begin() as cx:
        cx.execute(insert(opportunity_evidence).values(
            id=f"ev-{opportunity_id}-{source_id}", opportunity_id=opportunity_id, source_id=source_id,
            claim="preuve", type="observe", independant=True, date_creation=_dt(JOUR),
        ))


def _noter(engine, opportunity_id, *, score_prudent, decision_critic):
    with engine.begin() as cx:
        cx.execute(insert(scores).values(
            id=f"sc-{opportunity_id}", opportunity_id=opportunity_id, run_id="run-test",
            version_poids="v1", valeurs_json={}, score_brut=score_prudent, score_prudent=score_prudent,
            couverture_preuves=0.5, flags_json=[], decision_critic=decision_critic, date_creation=_dt(JOUR),
        ))


def _cout(engine, id_, montant, jour, *, role=None, opportunity_id=None):
    with engine.begin() as cx:
        cx.execute(insert(usage_events).values(
            id=id_, run_id="run-test", fournisseur="anthropic", modele_ou_actor="test",
            appels=1, tokens_in=100, tokens_out=50, cout_declare_ou_estime=montant, devise="EUR",
            date_creation=_dt(jour), role=role, opportunity_id=opportunity_id,
        ))


def _construire_jeu_de_test(engine):
    # 4 opportunités repérées le JOUR, 3 notées, 1 encore "nouveau".
    _creer_opportunite(engine, "opp1", jour=JOUR, statut="rejete", secteur="intersectoriel")
    _creer_opportunite(engine, "opp2", jour=JOUR, statut="incertain", secteur="services_professionnels")
    _creer_opportunite(engine, "opp3", jour=JOUR, statut="a_revoir", secteur="e_commerce")
    _creer_opportunite(engine, "opp4", jour=JOUR, statut="nouveau", secteur="intersectoriel")
    # Une 5e opportunité, un autre jour -- ne doit JAMAIS apparaître dans les métriques du JOUR.
    _creer_opportunite(engine, "opp5", jour=JOUR_AUTRE, statut="nouveau", secteur="intersectoriel")

    for i in range(1, 5):
        _creer_source(engine, f"src{i}a")
    _creer_source(engine, "src2b")
    _creer_source(engine, "src3b")
    _creer_source(engine, "src3c")

    _rattacher_preuve(engine, "opp1", "src1a")
    _rattacher_preuve(engine, "opp2", "src2a")
    _rattacher_preuve(engine, "opp2", "src2b")
    _rattacher_preuve(engine, "opp3", "src3a")
    _rattacher_preuve(engine, "opp3", "src3b")
    _rattacher_preuve(engine, "opp3", "src3c")
    _rattacher_preuve(engine, "opp4", "src4a")

    _noter(engine, "opp1", score_prudent=20.0, decision_critic="rejeter")
    _noter(engine, "opp2", score_prudent=50.0, decision_critic="a_verifier")
    _noter(engine, "opp3", score_prudent=90.0, decision_critic="eligible_revue_humaine")
    # opp4 : pas encore de score (toujours "nouveau").

    _cout(engine, "cout1", 0.05, JOUR)
    _cout(engine, "cout2", 0.10, JOUR)
    _cout(engine, "cout3", 99.0, JOUR_AUTRE)  # un autre jour -- ne doit pas compter


def test_metriques_sur_jeu_de_test_complet(engine_test):
    _construire_jeu_de_test(engine_test)

    m = calculer_metriques(engine_test, JOUR)

    assert m["jour"] == "2026-01-15"
    assert m["opportunites_reperees"] == 4
    assert m["opportunites_analysees"] == 3
    assert m["par_statut"] == {"rejete": 1, "incertain": 1, "a_revoir": 1, "nouveau": 1}
    assert m["par_decision_critic"] == {"rejeter": 1, "a_verifier": 1, "eligible_revue_humaine": 1}

    assert m["score_prudent"]["min"] == 20.0
    assert m["score_prudent"]["max"] == 90.0
    assert m["score_prudent"]["mediane"] == 50.0
    assert m["score_prudent"]["p90"] == 90.0
    assert m["score_prudent"]["nb_superieur_60"] == 1
    assert m["score_prudent"]["nb_superieur_80"] == 1

    # opp2 (services_professionnels) et opp3 (e_commerce) sont hors intersectoriel -> 2/4.
    assert m["part_hors_intersectoriel"] == 0.5
    assert m["par_secteur"] == {"intersectoriel": 2, "services_professionnels": 1, "e_commerce": 1}

    assert m["sources_par_dossier"]["min"] == 1
    assert m["sources_par_dossier"]["max"] == 3
    assert m["sources_par_dossier"]["mediane"] == 2.0
    # opp1 et opp4 n'ont qu'une seule source -> 2/4.
    assert m["sources_par_dossier"]["part_une_seule_source"] == 0.5

    # Le type d'objection n'existe pas encore dans le modèle -- jamais inventé.
    assert m["objections_critic_par_type"] is None

    assert m["cout_jour_eur"] == 0.15
    assert m["cout_moyen_par_dossier_analyse_eur"] == round(0.15 / 3, 4)


def test_metriques_cout_par_role_et_par_opportunite(engine_test):
    """Sous-étape 0.7 : les colonnes role/opportunity_id d'usage_events sont
    ventilées par app.metriques — l'historique (role=None) est regroupé sous
    'sans_role', jamais ignoré ni jamais crashé."""
    _construire_jeu_de_test(engine_test)
    # opp1 : deux appels tracés (scout puis analyst) -> 0,05 + 0,03 = 0,08.
    _cout(engine_test, "cout-scout-opp1", 0.02, JOUR, role="scout", opportunity_id="opp1")
    _cout(engine_test, "cout-analyst-opp1", 0.06, JOUR, role="analyst", opportunity_id="opp1")
    # opp2 : un seul appel tracé -> 0,04.
    _cout(engine_test, "cout-critic-opp2", 0.04, JOUR, role="critic", opportunity_id="opp2")

    m = calculer_metriques(engine_test, JOUR)

    # cout1 (0.05) et cout2 (0.10) de _construire_jeu_de_test n'ont pas de
    # role -> regroupés sous "sans_role".
    assert m["cout_par_role_eur"]["sans_role"] == round(0.05 + 0.10, 4)
    assert m["cout_par_role_eur"]["scout"] == 0.02
    assert m["cout_par_role_eur"]["analyst"] == 0.06
    assert m["cout_par_role_eur"]["critic"] == 0.04

    # Moyenne UNIQUEMENT sur les lignes avec opportunity_id (opp1: 0.08, opp2: 0.04).
    assert m["cout_moyen_par_opportunite_eur"] == round((0.08 + 0.04) / 2, 4)


def test_metriques_jour_vide_ne_plante_pas(engine_test):
    _construire_jeu_de_test(engine_test)

    m = calculer_metriques(engine_test, date(2026, 1, 1))  # aucune donnée ce jour-là

    assert m["opportunites_reperees"] == 0
    assert m["opportunites_analysees"] == 0
    assert m["par_statut"] == {}
    assert m["par_decision_critic"] == {}
    assert m["score_prudent"] == {
        "min": None, "mediane": None, "p90": None, "max": None,
        "nb_superieur_60": 0, "nb_superieur_80": 0,
    }
    assert m["par_secteur"] == {}
    assert m["part_hors_intersectoriel"] is None
    assert m["sources_par_dossier"]["part_une_seule_source"] is None
    assert m["cout_jour_eur"] == 0.0
    assert m["cout_moyen_par_dossier_analyse_eur"] is None
    assert m["cout_par_role_eur"] == {}
    assert m["cout_moyen_par_opportunite_eur"] is None


def test_metriques_ignore_les_autres_jours(engine_test):
    """opp5 (JOUR_AUTRE) et cout3 (JOUR_AUTRE) ne doivent jamais fuiter dans
    les métriques du JOUR, ni inversement."""
    _construire_jeu_de_test(engine_test)

    m_autre = calculer_metriques(engine_test, JOUR_AUTRE)
    assert m_autre["opportunites_reperees"] == 1
    assert m_autre["cout_jour_eur"] == 99.0


def test_formater_comparaison_affiche_les_deux_jours_cote_a_cote(engine_test):
    _construire_jeu_de_test(engine_test)

    m_jour = calculer_metriques(engine_test, JOUR)
    m_autre = calculer_metriques(engine_test, JOUR_AUTRE)

    texte = formater_comparaison(JOUR, m_jour, JOUR_AUTRE, m_autre)

    assert JOUR.isoformat() in texte
    assert JOUR_AUTRE.isoformat() in texte
    assert "Opportunités repérées" in texte
    # 4 opportunités le JOUR, 1 le JOUR_AUTRE -- les deux valeurs apparaissent
    # bien sur la même ligne, chacune dans sa colonne.
    ligne_reperees = next(l for l in texte.splitlines() if l.startswith("Opportunités repérées"))
    assert "4" in ligne_reperees
    assert "1" in ligne_reperees


def test_main_avec_comparer_ecrit_les_deux_jours_et_affiche_la_comparaison(tmp_path, monkeypatch, capsys):
    from sqlalchemy import create_engine

    from app.storage.db import migrer

    chemin_db = tmp_path / "metriques_cli_test.db"
    moteur = create_engine(f"sqlite:///{chemin_db}", future=True, connect_args={"check_same_thread": False})
    migrer(moteur)
    _construire_jeu_de_test(moteur)

    monkeypatch.setenv("RADAR_DATABASE_URL", f"sqlite:///{chemin_db}")

    import app.metriques as metriques_mod
    monkeypatch.setattr(metriques_mod, "DOSSIER_RAPPORTS", tmp_path / "rapports_test")

    code = main(["--jour", JOUR.isoformat(), "--comparer", JOUR_AUTRE.isoformat()])

    assert code == 0
    assert (tmp_path / "rapports_test" / f"{JOUR.isoformat()}.json").exists()
    assert (tmp_path / "rapports_test" / f"{JOUR_AUTRE.isoformat()}.json").exists()

    sortie = capsys.readouterr().out
    assert "Opportunités repérées" in sortie
    assert JOUR.isoformat() in sortie
    assert JOUR_AUTRE.isoformat() in sortie


def test_main_sans_radar_database_url_message_clair_sans_repli(tmp_path, monkeypatch, capsys):
    import app.metriques as metriques_mod

    monkeypatch.delenv("RADAR_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    # Chemin isolé : ce test ne doit jamais dépendre du vrai ~/.config de la
    # machine qui l'exécute (qui peut très bien contenir ce fichier une fois
    # la sous-étape 0.5 utilisée pour de vrai).
    monkeypatch.setattr(metriques_mod, "FICHIER_ENV_LECTURE_SEULE", tmp_path / "n-existe-pas" / "env")

    code = main(["--jour", JOUR.isoformat()])

    assert code == 1
    erreur = capsys.readouterr().err
    assert "RADAR_DATABASE_URL" in erreur


def test_main_ignore_database_url_meme_si_definie(tmp_path, monkeypatch, capsys):
    """DATABASE_URL (celle du reste de l'app, avec écriture) ne doit jamais
    servir de repli : sans RADAR_DATABASE_URL, la commande s'arrête même si
    DATABASE_URL, elle, est définie."""
    import app.metriques as metriques_mod

    monkeypatch.setenv("DATABASE_URL", "sqlite:///./ne-devrait-jamais-etre-ouvert.db")
    monkeypatch.delenv("RADAR_DATABASE_URL", raising=False)
    monkeypatch.setattr(metriques_mod, "FICHIER_ENV_LECTURE_SEULE", tmp_path / "n-existe-pas" / "env")

    code = main(["--jour", JOUR.isoformat()])

    assert code == 1
    assert "RADAR_DATABASE_URL" in capsys.readouterr().err


def test_main_utilise_le_fichier_env_si_variable_absente(tmp_path, monkeypatch, capsys):
    """Repli sur le fichier écrit par scripts/creer_acces_lecture.py (0.5)
    quand RADAR_DATABASE_URL n'est pas dans l'environnement."""
    from sqlalchemy import create_engine

    from app.storage.db import migrer

    chemin_db = tmp_path / "metriques_fichier_env.db"
    moteur = create_engine(f"sqlite:///{chemin_db}", future=True, connect_args={"check_same_thread": False})
    migrer(moteur)
    _construire_jeu_de_test(moteur)

    fichier_env = tmp_path / "config" / "radar-opportunites" / "env"
    fichier_env.parent.mkdir(parents=True)
    fichier_env.write_text(f"RADAR_DATABASE_URL=sqlite:///{chemin_db}\n", encoding="utf-8")

    import app.metriques as metriques_mod

    monkeypatch.delenv("RADAR_DATABASE_URL", raising=False)
    monkeypatch.setattr(metriques_mod, "FICHIER_ENV_LECTURE_SEULE", fichier_env)
    monkeypatch.setattr(metriques_mod, "DOSSIER_RAPPORTS", tmp_path / "rapports_test")

    code = main(["--jour", JOUR.isoformat()])

    assert code == 0
    assert '"opportunites_reperees": 4' in capsys.readouterr().out


def test_main_variable_environnement_prioritaire_sur_le_fichier(tmp_path, monkeypatch):
    """Si RADAR_DATABASE_URL est définie, le fichier n'est même pas lu."""
    import app.metriques as metriques_mod

    fichier_env = tmp_path / "env"
    fichier_env.write_text("RADAR_DATABASE_URL=sqlite:///ne-devrait-jamais-etre-ouvert.db\n", encoding="utf-8")

    monkeypatch.setenv("RADAR_DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setattr(metriques_mod, "FICHIER_ENV_LECTURE_SEULE", fichier_env)

    moteur = metriques_mod._engine_lecture_seule()
    assert "ne-devrait-jamais-etre-ouvert" not in str(moteur.url)


def test_main_comparer_date_invalide(capsys):
    code = main(["--jour", "2026-01-15", "--comparer", "pas-une-date"])
    assert code == 1
    assert "Format de date invalide" in capsys.readouterr().err
