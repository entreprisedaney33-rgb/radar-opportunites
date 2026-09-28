"""Sous-étape 3.11 : `app.pipeline.orchestrator._phase_enquete` résout les
mots-clés transmis à l'Enquêteur -- la proposition déjà validée du Scout
(`opportunities.mots_cles_en`, persistée par `_phase_collecte_et_scout`) si
elle est présente, sinon un repli dérivé par du code de la douleur
(`app.pipeline.mots_cles.deriver_mots_cles_repli`) -- et compte une « requête
évitée » (`app.pipeline.budget.ROLE_ENQUETEUR_REQUETE_EVITEE`) quand aucun des
deux n'est utilisable, jamais une requête vide envoyée à un vrai fournisseur.

`enqueter_opportunite` est remplacée par un double qui capture juste
l'`HypotheseEnqueteur` reçue : ce module ne teste pas l'enquête elle-même
(déjà couverte par `tests/test_enqueteur_enqueteur.py`), seulement la
RÉSOLUTION des mots-clés en amont."""
from __future__ import annotations

import time
from datetime import datetime, timezone

from app.pipeline.budget import ROLE_ENQUETEUR_REQUETE_EVITEE, BudgetTracker
from app.pipeline.orchestrator import OptionsRun, ResumeRun, _phase_enquete
from app.storage import repo

QUOTAS = {
    "max_analyses_par_passage": 10,
    "enqueteur_resultats_par_requete": 5,
    "max_resultats_enquete_par_opportunite": 8,
    "enqueteur_fetch_delai_secondes": 0,
}


def _budget(engine, run_id):
    return BudgetTracker(engine, run_id, plafond_eur=25.0, plafond_appels_approfondis=1000)


def _creer_opportunite(engine, *, mots_cles_en=None, probleme):
    return repo.creer_opportunite(
        engine, titre="t", acheteur="a", probleme=probleme, mecanisme_ia="m",
        secteur="e_commerce", statut="nouveau", cluster_id=None, mots_cles_en=mots_cles_en,
    )


def _lancer_phase_enquete(engine, monkeypatch):
    """Remplace `enqueter_opportunite` par un double qui capture chaque
    `HypotheseEnqueteur` reçue, dans l'ordre des opportunités traitées."""
    hypotheses: list = []
    monkeypatch.setattr(
        "app.pipeline.orchestrator.enqueter_opportunite",
        lambda engine, opportunity_id, hypothese, **kw: (hypotheses.append(hypothese), [])[1],
    )
    run_id = repo.creer_run(engine, mode="reel", version_code="t", version_config="t", quotas={})
    budget = _budget(engine, run_id)
    resume = ResumeRun()
    _phase_enquete(
        engine, options=OptionsRun(mode="reel"), quotas=QUOTAS, budget=budget, resume=resume,
        debut=time.monotonic(), duree_max=999.0,
    )
    return hypotheses


def _nb_requetes_evitees(engine) -> int:
    jour = datetime.now(timezone.utc).date()
    return repo.nombre_evenements_role_jour_utc(engine, jour, role=ROLE_ENQUETEUR_REQUETE_EVITEE)


def test_utilise_les_mots_cles_valides_du_scout_quand_presents(engine_test, monkeypatch):
    _creer_opportunite(engine_test, mots_cles_en="invoice reconciliation manual", probleme="peu importe")

    hypotheses = _lancer_phase_enquete(engine_test, monkeypatch)

    assert hypotheses[0].mots_cles == "invoice reconciliation manual"
    assert _nb_requetes_evitees(engine_test) == 0


def test_derive_un_repli_quand_le_scout_n_a_pas_propose_de_mots_cles(engine_test, monkeypatch):
    """Opportunité créée avant la sous-étape 3.11 (ou proposition du Scout
    invalide) : `mots_cles_en` est `None` en base -- un repli est dérivé de
    la douleur, jamais une requête vide envoyée."""
    _creer_opportunite(
        engine_test, mots_cles_en=None,
        probleme="We manually reconcile the invoices every week and it is tedious",
    )

    hypotheses = _lancer_phase_enquete(engine_test, monkeypatch)

    assert hypotheses[0].mots_cles == "manually reconcile invoices every week"
    assert _nb_requetes_evitees(engine_test) == 0


def test_requete_evitee_quand_ni_scout_ni_repli_ne_sont_utilisables(engine_test, monkeypatch):
    """La douleur ne contient que des mots vides/trop courts : aucun mot-clé
    dérivable -- `hypothese.mots_cles` reste vide (jamais de requête
    `demande`/`concurrence` envoyée, voir `app.enqueteur.gabarits.generer_requetes`)
    et le phénomène est compté pour rester visible dans `app.metriques`."""
    _creer_opportunite(engine_test, mots_cles_en=None, probleme="the a of it is on at by")

    hypotheses = _lancer_phase_enquete(engine_test, monkeypatch)

    assert hypotheses[0].mots_cles == ""
    assert _nb_requetes_evitees(engine_test) == 1


def test_plusieurs_opportunites_ne_comptent_que_celles_sans_mots_cles_utilisables(engine_test, monkeypatch):
    _creer_opportunite(engine_test, mots_cles_en="invoice reconciliation manual", probleme="x")
    _creer_opportunite(engine_test, mots_cles_en=None, probleme="the a of it is on at by")
    _creer_opportunite(engine_test, mots_cles_en=None, probleme="manually reconcile invoices every week")

    hypotheses = _lancer_phase_enquete(engine_test, monkeypatch)

    assert len(hypotheses) == 3
    assert _nb_requetes_evitees(engine_test) == 1
