"""Sous-étape 3.13 (AMELIORATIONS.md) : trois ajouts à `app.metriques` --
un dossier `a_reprendre` (créé uniquement par repli sans modèle pendant une
panne, voir `app.reprise`) exclu des métriques tant qu'il n'est pas repassé
par un vrai Scout ; le nombre d'appels en erreur PAR HEURE (repérer un
incident avant qu'il ne dure 25 h, voir
`rapports/POINT_ETAPE_2026-09-27.md`) ; l'état courant du disjoncteur de
l'appel au modèle."""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import create_engine, insert

from app.adapters.model_client import ISSUE_PERDUE, ISSUE_VALIDE
from app.metriques import calculer_metriques
from app.storage import repo
from app.storage.db import migrer
from app.storage.schema import opportunities, usage_events

JOUR = date(2026, 9, 27)


def engine_test():
    moteur = create_engine("sqlite:///:memory:", future=True)
    migrer(moteur)
    return moteur


def _dt(heure: int, minute: int = 0) -> datetime:
    return datetime(JOUR.year, JOUR.month, JOUR.day, heure, minute, tzinfo=timezone.utc)


def _creer_opportunite(engine, id_, *, statut, heure=12):
    with engine.begin() as cx:
        cx.execute(insert(opportunities).values(
            id=id_, titre=f"titre {id_}", acheteur="a", probleme="p", mecanisme_ia="m",
            secteur="intersectoriel", statut=statut, cluster_id=None,
            date_creation=_dt(heure), date_maj=_dt(heure),
        ))


def _cout(engine, id_, *, role, issue, heure):
    with engine.begin() as cx:
        cx.execute(insert(usage_events).values(
            id=id_, run_id="run-test", fournisseur="anthropic", modele_ou_actor="test",
            appels=1, tokens_in=None, tokens_out=None, cout_declare_ou_estime=0.01, devise="EUR",
            date_creation=_dt(heure), role=role, opportunity_id=None, issue=issue, sortie_tronquee=None,
        ))


def test_dossier_a_reprendre_exclu_des_metriques_du_jour():
    engine = engine_test()
    _creer_opportunite(engine, "opp-normal", statut="incertain")
    _creer_opportunite(engine, "opp-repli", statut="a_reprendre")

    resultat = calculer_metriques(engine, JOUR)

    assert resultat["opportunites_reperees"] == 1
    assert resultat["par_statut"] == {"incertain": 1}


def test_appels_en_erreur_par_heure_compte_uniquement_les_perdues():
    engine = engine_test()
    _cout(engine, "u1", role="critic", issue=ISSUE_PERDUE, heure=13)
    _cout(engine, "u2", role="analyst", issue=ISSUE_PERDUE, heure=13)
    _cout(engine, "u3", role="critic", issue=ISSUE_PERDUE, heure=14)
    _cout(engine, "u4", role="critic", issue=ISSUE_VALIDE, heure=13)  # jamais compté

    resultat = calculer_metriques(engine, JOUR)

    assert resultat["appels_en_erreur_par_heure"] == {
        _dt(13).isoformat(): 2,
        _dt(14).isoformat(): 1,
    }


def test_disjoncteur_api_modele_absent_par_defaut():
    engine = engine_test()
    resultat = calculer_metriques(engine, JOUR)
    assert resultat["disjoncteur_api_modele"] == {
        "echecs_consecutifs": 0, "en_erreur": False, "depuis": None,
        "pause_jusqu_a": None, "dernier_message": None,
    }


def test_disjoncteur_api_modele_reflete_l_etat_persiste():
    engine = engine_test()
    depuis = _dt(13, 41)
    pause = _dt(13, 56)
    repo.ecrire_disjoncteur_api(
        engine, echecs_consecutifs=0, en_erreur=True, depuis=depuis, pause_jusqu_a=pause,
        dernier_message="400 additionalProperties",
    )

    resultat = calculer_metriques(engine, JOUR)

    assert resultat["disjoncteur_api_modele"] == {
        "echecs_consecutifs": 0, "en_erreur": True, "depuis": depuis.isoformat(),
        "pause_jusqu_a": pause.isoformat(), "dernier_message": "400 additionalProperties",
    }
