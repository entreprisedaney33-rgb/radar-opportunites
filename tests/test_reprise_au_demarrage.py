"""Sous-étape 3.17, point 5 (AMELIORATIONS.md) : le marquage `a_reprendre` fait
au démarrage du worker quand `RADAR_REPRISE_DEPUIS` est définie -- sans réseau,
sans connexion en écriture depuis un poste."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest
from sqlalchemy import insert, select

from app.pipeline import orchestrator as orch
from app.reprise import VARIABLE_ENV, reprise_au_demarrage
from app.storage import repo
from app.storage.schema import opportunities, runs

DEPUIS = datetime(2026, 9, 26, 13, 41, tzinfo=timezone.utc)
AVANT = datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc)


def _dossier(engine, id_, *, date_creation, modeles_scout, statut="incertain"):
    with engine.begin() as cx:
        cx.execute(insert(opportunities).values(
            id=id_, titre=f"titre {id_}", acheteur="a", probleme="p", mecanisme_ia="m",
            secteur="intersectoriel", statut=statut, cluster_id=None,
            date_creation=date_creation, date_maj=date_creation,
        ))
    for modele in modeles_scout:
        repo.inserer_assessment(
            engine, opportunity_id=id_, run_id="run-test", role="scout",
            payload={}, modele=modele, version_prompt="scout-v1", inconnues=[],
        )


def _statuts(engine) -> dict[str, str]:
    with engine.connect() as cx:
        return {r[0]: r[1] for r in cx.execute(select(opportunities.c.id, opportunities.c.statut)).all()}


@pytest.fixture
def base_avec_repli(engine_test):
    _dossier(engine_test, "repli-apres-1", date_creation=DEPUIS, modeles_scout=["heuristique"])
    _dossier(engine_test, "repli-apres-2", date_creation=DEPUIS, modeles_scout=["heuristique"])
    _dossier(engine_test, "repli-avant", date_creation=AVANT, modeles_scout=["heuristique"])
    _dossier(engine_test, "reel-apres", date_creation=DEPUIS, modeles_scout=["claude-sonnet-5"])
    return engine_test


def test_variable_absente_ou_vide_ne_fait_rien(base_avec_repli, monkeypatch):
    monkeypatch.delenv(VARIABLE_ENV, raising=False)
    assert reprise_au_demarrage(base_avec_repli) is None
    monkeypatch.setenv(VARIABLE_ENV, "   ")
    assert reprise_au_demarrage(base_avec_repli) is None
    assert set(_statuts(base_avec_repli).values()) == {"incertain"}


def test_variable_definie_marque_les_dossiers_de_repli_et_resume(base_avec_repli, monkeypatch, caplog):
    monkeypatch.setenv(VARIABLE_ENV, "2026-09-26T13:41Z")

    with caplog.at_level(logging.INFO, logger="app.reprise"):
        message = reprise_au_demarrage(base_avec_repli)

    statuts = _statuts(base_avec_repli)
    assert statuts["repli-apres-1"] == statuts["repli-apres-2"] == "a_reprendre"
    assert statuts["repli-avant"] == "incertain"  # antérieur à la borne
    assert statuts["reel-apres"] == "incertain"  # déjà passé par un vrai Scout
    assert "2 dossier(s) nouvellement marqué(s)" in message
    assert "2 en attente" in message
    assert message in caplog.text  # résumé aussi dans les logs


def test_idempotent_un_redemarrage_ne_marque_rien_de_plus(base_avec_repli, monkeypatch):
    monkeypatch.setenv(VARIABLE_ENV, "2026-09-26T13:41Z")
    reprise_au_demarrage(base_avec_repli)

    message = reprise_au_demarrage(base_avec_repli)

    assert "0 dossier(s) nouvellement marqué(s)" in message
    assert "2 en attente" in message
    assert list(_statuts(base_avec_repli).values()).count("a_reprendre") == 2


def test_valeur_invalide_est_ignoree_sans_lever(base_avec_repli, monkeypatch):
    monkeypatch.setenv(VARIABLE_ENV, "hier soir")
    message = reprise_au_demarrage(base_avec_repli)
    assert "IGNORÉE" in message
    assert set(_statuts(base_avec_repli).values()) == {"incertain"}


def test_une_erreur_de_marquage_ne_remonte_jamais(base_avec_repli, monkeypatch):
    monkeypatch.setenv(VARIABLE_ENV, "2026-09-26T13:41Z")

    def _casse(*a, **kw):
        raise RuntimeError("base indisponible")

    monkeypatch.setattr(repo, "opportunites_creees_par_repli_scout", _casse)
    message = reprise_au_demarrage(base_avec_repli)
    assert "ÉCHOUÉE" in message and "base indisponible" in message


def test_le_worker_marque_au_demarrage_et_le_resume_survit_dans_runs_erreurs_json(base_avec_repli, monkeypatch):
    """`executer_continu` : marquage avant la boucle, résumé dans
    `runs.erreurs_json` (qui est réécrit en fin de passage par l'état du
    disjoncteur : le résumé doit y survivre)."""
    monkeypatch.setenv(VARIABLE_ENV, "2026-09-26T13:41Z")
    appels = {"n": 0}

    def fausse_pause(engine):
        appels["n"] += 1
        return appels["n"] > 1

    class ArretTest(Exception):
        pass

    def faux_sleep(_s):
        raise ArretTest()

    monkeypatch.setattr(orch, "_pause_demandee", fausse_pause)
    monkeypatch.setattr(orch.time, "sleep", faux_sleep)

    with pytest.raises(ArretTest):
        orch.executer_continu(base_avec_repli, forcer_demo=True)

    statuts = _statuts(base_avec_repli)
    # Sans accès modèle (pas de clé en test), `_phase_reprise` ne retraite rien :
    # les dossiers restent marqués, en attente du prochain passage avec l'API.
    assert statuts["repli-apres-1"] == statuts["repli-apres-2"] == "a_reprendre"
    with base_avec_repli.connect() as cx:
        erreurs = cx.execute(select(runs.c.erreurs_json)).scalars().all()
    assert any("Reprise au démarrage" in ligne for liste in erreurs for ligne in liste)


def test_le_message_de_reprise_ne_precede_jamais_le_message_d_incident(engine_test):
    """Jarvis relit le premier message d'`erreurs_json` pour l'état « API en
    erreur » : le résumé de reprise vient toujours après."""
    run_id = repo.creer_run(engine_test, mode="reel", version_code="t", version_config="t", quotas={})
    repo.marquer_disjoncteur_sur_run(
        engine_test, run_id, en_erreur=True, depuis=DEPUIS, message="boom", messages_permanents=["Reprise au démarrage : x"],
    )
    with engine_test.connect() as cx:
        erreurs = cx.execute(select(runs.c.erreurs_json).where(runs.c.id == run_id)).scalar_one()
    assert erreurs[0].startswith("Disjoncteur API en erreur")
    assert erreurs[1] == "Reprise au démarrage : x"
