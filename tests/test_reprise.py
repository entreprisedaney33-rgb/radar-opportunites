"""Sous-étape 3.13 (AMELIORATIONS.md) : `app.reprise` -- sélection des
dossiers créés uniquement par repli sans modèle (panne du 26/09/2026, voir
`rapports/POINT_ETAPE_2026-09-27.md`), marquage `a_reprendre`, idempotence,
mode simulation. Le retraitement réel (rappel du Scout) est testé à part
dans `tests/test_orchestrator_disjoncteur_et_reprise.py`."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine

from app import config as cfg
from app.reprise import _parser_depuis, main
from app.storage import repo
from app.storage.db import migrer


@pytest.fixture
def engine_test(tmp_path):
    chemin = tmp_path / "test.db"
    moteur = create_engine(f"sqlite:///{chemin}", future=True, connect_args={"check_same_thread": False})
    migrer(moteur)
    return moteur, chemin


DEPUIS = datetime(2026, 9, 26, 13, 41, tzinfo=timezone.utc)
AVANT = datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc)


def _creer_opportunite_avec_scout(engine, id_, *, date_creation, modeles_scout: list[str], statut="incertain"):
    """`modeles_scout` : une évaluation Scout par modèle listé, DANS L'ORDRE
    -- reproduit une reprise déjà réussie (une entrée "heuristique" suivie
    d'une entrée avec un vrai modèle)."""
    from sqlalchemy import insert

    from app.storage.schema import opportunities

    with engine.begin() as cx:
        cx.execute(insert(opportunities).values(
            id=id_, titre=f"titre {id_}", acheteur="a", probleme="p", mecanisme_ia="m",
            secteur="intersectoriel", statut=statut, cluster_id=None,
            date_creation=date_creation, date_maj=date_creation,
        ))
    for i, modele in enumerate(modeles_scout):
        repo.inserer_assessment(
            engine, opportunity_id=id_, run_id="run-test", role="scout",
            payload={}, modele=modele, version_prompt="scout-v1", inconnues=[],
        )


def test_parser_depuis_accepte_le_format_z():
    dt = _parser_depuis("2026-09-26T13:41Z")
    assert dt == DEPUIS


def test_selection_ne_retient_que_le_repli_apres_depuis(engine_test):
    engine, _ = engine_test
    _creer_opportunite_avec_scout(engine, "repli-apres", date_creation=DEPUIS, modeles_scout=["heuristique"])
    _creer_opportunite_avec_scout(engine, "reel-apres", date_creation=DEPUIS, modeles_scout=["claude-sonnet-5"])
    _creer_opportunite_avec_scout(engine, "repli-avant", date_creation=AVANT, modeles_scout=["heuristique"])
    _creer_opportunite_avec_scout(
        engine, "deja-reprise", date_creation=DEPUIS, modeles_scout=["heuristique", "claude-haiku-4-5-20251001"],
    )

    candidats = repo.opportunites_creees_par_repli_scout(engine, DEPUIS)

    assert [o["id"] for o in candidats] == ["repli-apres"]


def test_simulation_n_ecrit_rien(engine_test, capsys):
    engine, chemin = engine_test
    _creer_opportunite_avec_scout(engine, "repli-apres", date_creation=DEPUIS, modeles_scout=["heuristique"])

    code = _lancer(engine, chemin, ["--depuis", "2026-09-26T13:41Z", "--simulation"])

    assert code == 0
    dossier = repo.lister_opportunites_ouvertes(engine)[0]
    assert dossier["statut"] == "incertain"  # jamais touché
    assert "seraient marqués" in capsys.readouterr().out


def test_mode_reel_marque_a_reprendre_et_reste_idempotent(engine_test, capsys):
    engine, chemin = engine_test
    _creer_opportunite_avec_scout(engine, "repli-apres", date_creation=DEPUIS, modeles_scout=["heuristique"])

    code = _lancer(engine, chemin, ["--depuis", "2026-09-26T13:41Z"])
    assert code == 0
    assert repo.lister_opportunites_ouvertes(engine)[0]["statut"] == "a_reprendre"

    # Un dossier déjà `a_reprendre` sort de la sélection (rien à refaire) --
    # relancer la commande ne trouve donc plus rien, sans avoir eu besoin de
    # suivre "ce qui a déjà été marqué" ailleurs.
    capsys.readouterr()
    code2 = _lancer(engine, chemin, ["--depuis", "2026-09-26T13:41Z"])
    assert code2 == 0
    assert "Aucun dossier" in capsys.readouterr().out
    assert repo.lister_opportunites_ouvertes(engine)[0]["statut"] == "a_reprendre"


def test_reprise_reussie_disparait_de_la_selection(engine_test):
    """Une fois qu'un vrai Scout a tourné (nouvelle évaluation avec un vrai
    modèle, voir `app.pipeline.orchestrator._phase_reprise`), l'opportunité
    ne réapparaît plus jamais dans la sélection -- même sans changer son
    statut manuellement ici."""
    engine, _ = engine_test
    _creer_opportunite_avec_scout(
        engine, "repli-puis-reussi", date_creation=DEPUIS, modeles_scout=["heuristique"], statut="incertain",
    )
    assert len(repo.opportunites_creees_par_repli_scout(engine, DEPUIS)) == 1

    repo.inserer_assessment(
        engine, opportunity_id="repli-puis-reussi", run_id="run-test", role="scout",
        payload={}, modele="claude-sonnet-5", version_prompt="scout-v1", inconnues=[],
    )

    assert repo.opportunites_creees_par_repli_scout(engine, DEPUIS) == []


def test_signal_origine_scout_retrouve_le_texte_d_origine(engine_test):
    engine, _ = engine_test
    source_id, _ = repo.upsert_source(
        engine, url_canonique="https://exemple.invalid/1", domaine="exemple.invalid",
        date_publication=None, type_source="rss", extrait="texte original du signal",
        empreinte="e1", droits_collecte="public",
    )
    opportunity_id = repo.creer_opportunite(
        engine, titre="t", acheteur="a", probleme="p", mecanisme_ia="m",
        secteur="intersectoriel", statut="a_reprendre", cluster_id=None,
    )
    repo.inserer_evidence(
        engine, opportunity_id=opportunity_id, source_id=source_id,
        claim="Scout: texte original du signal", type_="hypothese", independant=True,
    )

    origine = repo.signal_origine_scout(engine, opportunity_id)

    assert origine == {"source_id": source_id, "extrait": "texte original du signal"}


def test_signal_origine_scout_none_si_aucune_preuve_scout(engine_test):
    engine, _ = engine_test
    opportunity_id = repo.creer_opportunite(
        engine, titre="t", acheteur="a", probleme="p", mecanisme_ia="m",
        secteur="intersectoriel", statut="a_reprendre", cluster_id=None,
    )
    assert repo.signal_origine_scout(engine, opportunity_id) is None


def test_maj_opportunite_depuis_reprise_ne_touche_que_les_champs_lites(engine_test):
    engine, _ = engine_test
    opportunity_id = repo.creer_opportunite(
        engine, titre="ancien", acheteur="ancien", probleme="ancien", mecanisme_ia="ancien",
        secteur="intersectoriel", statut="a_reprendre", cluster_id="cluster-1",
    )
    avant = repo.lister_opportunites_ouvertes(engine)[0]

    repo.maj_opportunite_depuis_reprise(
        engine, opportunity_id, titre="nouveau", acheteur="nouveau", probleme="nouveau",
        mecanisme_ia="nouveau", secteur="e_commerce", secteur_provenance="defaut", secteur_citation=None,
        mots_cles_en="new keywords", mots_cles_fr=None, statut="nouveau",
    )

    apres = repo.lister_opportunites_ouvertes(engine)[0]
    assert apres["titre"] == "nouveau"
    assert apres["statut"] == "nouveau"
    assert apres["secteur"] == "e_commerce"
    assert apres["mots_cles_en"] == "new keywords"
    assert apres["cluster_id"] == "cluster-1"  # inchangé
    assert apres["id"] == avant["id"]  # inchangé
    assert apres["date_creation"] == avant["date_creation"]  # inchangé


def _lancer(engine, chemin, argv):
    """Fait pointer `app.storage.db.get_engine()` (lu par `app.reprise.main`)
    vers le moteur du test, via `DATABASE_URL` -- même mécanisme que
    `tests/test_web.py`."""
    import app.storage.db as dbmod

    ancien = cfg.get_settings()
    try:
        import os

        os.environ["DATABASE_URL"] = f"sqlite:///{chemin}"
        cfg.get_settings.cache_clear()
        dbmod.get_engine.cache_clear()
        return main(argv)
    finally:
        os.environ.pop("DATABASE_URL", None)
        cfg.get_settings.cache_clear()
        dbmod.get_engine.cache_clear()
