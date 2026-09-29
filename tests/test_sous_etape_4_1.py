"""Sous-étape 4.1 (AMELIORATIONS.md), sans réseau : mode économe, bloc de
faisabilité (jamais dans le score), recalcul des scores et archivage
`archive_faible`, reprise de la faisabilité."""
from __future__ import annotations

import time

import pytest
from sqlalchemy import select, text

from app import config as cfg
from app import recalcul
from app.adapters.model_client import estimer_cout_eur
from app.adapters.schema_strict import rendre_schema_strict
from app.faisabilite import MOTIF_NON_EVALUEE, evaluer_accessibilite
from app.models_schemas import (
    AnalystSortie, Competence, DelaiPremierRevenu, FaisabiliteSortie, InvestissementInitial, Marche,
    TailleProbleme,
)
from app.pipeline import orchestrator as orch
from app.pipeline.budget import BudgetDepasse, BudgetTracker
from app.roles import analyst as role_analyst
from app.roles import faisabilite as role_faisabilite
from app.scoring.engine import calculer_score
from app.storage import repo
from app.storage.db import migrer
from app.storage.schema import decisions, faisabilites, opportunities, scores


# ------------------------------------------------------------- fabriques --

def _bloc(invest="moins_de_5k", delai="moins_de_3_mois", marche="accessible_depuis_france",
          competences=("dev_ia", "vente"), taille="segment_pme") -> dict:
    j = "Une phrase de justification."
    return {
        "type": "hypothese",
        "investissement_initial": {"valeur": invest, "justification": j},
        "delai_premier_revenu": {"valeur": delai, "justification": j},
        "marche": {"valeur": marche, "justification": j},
        "competences": {"valeurs": list(competences), "justification": j},
        "taille_du_probleme": {"valeur": taille, "justification": j},
    }


def _analyse(*, forts: int = 0, faisabilite: dict | None = None) -> dict:
    """Payload d'Analyst : `forts` critères notés 'fort' (2 faits sur 2 sources distinctes)."""
    noms = list(cfg.poids_scoring()["criteres"])
    criteres = []
    for i, nom in enumerate(noms):
        affirmations = []
        if i < forts:
            affirmations = [
                {"texte": "f1", "type": "observe", "source_ids": ["s-origine"]},
                {"texte": "f2", "type": "observe", "source_ids": ["s-autre"]},
            ]
        criteres.append({"nom": nom, "affirmations": affirmations, "inconnues": []})
    payload = {
        "opportunity_id": "x", "criteres": criteres, "prix_observes": [], "marge_indicative": None,
        "contradictions": [], "prochain_test_moins_couteux": "t", "niveau_preuve_global": "moyen",
    }
    if faisabilite is not None:
        payload["faisabilite"] = faisabilite
    return payload


def _dossier(engine, *, forts=0, statut="incertain", ancien_score=90.0, avec_analyse=True, modele="claude-sonnet-5") -> str:
    opp = repo.creer_opportunite(
        engine, titre="t", acheteur="a", probleme="p", mecanisme_ia="m", secteur="intersectoriel",
        statut=statut, cluster_id=None,
    )
    o, _ = repo.upsert_source(
        engine, url_canonique=f"https://news.ycombinator.com/{opp}", domaine="news.ycombinator.com",
        date_publication=None, type_source="rss", extrait="signal", empreinte=f"o-{opp}", droits_collecte="t",
    )
    a, _ = repo.upsert_source(
        engine, url_canonique=f"https://www.exemple.test/{opp}", domaine="www.exemple.test",
        date_publication=None, type_source="page_web", extrait="page", empreinte=f"a-{opp}", droits_collecte="t",
    )
    repo.inserer_evidence(engine, opportunity_id=opp, source_id=o, claim="Scout: signal", type_="non_verifie", independant=True)
    repo.inserer_evidence(engine, opportunity_id=opp, source_id=a, claim="Enquête: page", type_="non_verifie", independant=True)
    if avec_analyse:
        payload = _analyse(forts=forts)
        for critere in payload["criteres"]:  # branche les vrais ids de source de ce dossier
            for aff in critere["affirmations"]:
                aff["source_ids"] = [o if aff["source_ids"] == ["s-origine"] else a]
        repo.inserer_assessment(engine, opportunity_id=opp, run_id="run-a", role="analyst", payload=payload,
                                modele=modele, version_prompt="analyst-v1", inconnues=[])
        repo.inserer_score(
            engine, opportunity_id=opp, run_id="run-a", version_poids="2026.09.1", valeurs={},
            score_brut=ancien_score, score_prudent=ancien_score, couverture_preuves=1.0, flags=[],
            decision_critic="a_verifier",
        )
    return opp


def _statut(engine, opp) -> str:
    with engine.connect() as cx:
        return cx.execute(select(opportunities.c.statut).where(opportunities.c.id == opp)).scalar_one()


# ------------------------------------------------------------ 1. config ---

def test_budget_journalier_5_euros_et_appels_recales_en_proportion():
    q = cfg._load_yaml("quotas.yaml")
    assert q["budget_eur_par_jour"] == 5.0
    assert q["max_appels_approfondis_par_jour"] == round(1300 * 5.0 / 25.0) == 260


def test_le_tirage_de_controle_est_a_zero_meme_avec_des_rejetes_en_stock(engine_test):
    for _ in range(6):
        _dossier(engine_test, statut="rejete", avec_analyse=False)
    q = cfg._load_yaml("quotas.yaml")
    for _ in range(50):  # le tirage est aléatoire : 50 essais, aucun ne doit rien tirer
        selection = orch._selectionner_pour_analyse(
            engine_test, q["max_analyses_par_passage"], q["echantillon_rejetes_pour_controle"],
            max_tirages_par_jour=q["max_tirages_controle_par_jour"],
        )
        assert selection == []


# ------------------------------------------------ 2. faisabilité (code) ---

def test_config_faisabilite_ne_contient_que_des_valeurs_valides():
    c = cfg.faisabilite()
    assert set(c["investissements_acceptes"]) <= {e.value for e in InvestissementInitial}
    assert set(c["delais_acceptes"]) <= {e.value for e in DelaiPremierRevenu}
    assert set(c["marches_acceptes"]) <= {e.value for e in Marche}
    assert set(c["competences_refusees"]) <= {e.value for e in Competence}
    assert set(c["tailles_refusees"]) <= {e.value for e in TailleProbleme}


def test_les_seuils_de_mathéo_sont_ceux_de_la_decision_du_29_09():
    c = cfg.faisabilite()
    assert c["investissements_acceptes"] == ["moins_de_5k", "5k_a_20k"]
    assert c["delais_acceptes"] == ["moins_de_3_mois", "3_a_12_mois"]
    assert c["marches_acceptes"] == ["accessible_depuis_france", "europe"]
    assert sorted(c["competences_refusees"]) == ["materiel_industriel", "reglementaire_lourd"]
    assert c["tailles_refusees"] == ["systemique"]
    assert c["seuil_score_liste"] == 50


def test_dossier_accessible_sans_motif():
    r = evaluer_accessibilite(_bloc(invest="5k_a_20k", delai="3_a_12_mois", marche="europe",
                                    competences=("dev_ia", "reseau_specifique"), taille="marche_national_large"))
    assert r.accessible_solo is True and r.motif_exclusion is None


@pytest.mark.parametrize("champ, valeur, attendu", [
    ("invest", "20k_a_100k", "investissement trop élevé (20 à 100 k€)"),
    ("invest", "plus_de_100k", "investissement trop élevé (plus de 100 k€)"),
    ("delai", "plus_de_12_mois", "premier revenu trop lointain (plus de 12 mois)"),
    ("marche", "etats_unis_seulement", "marché non accessible (États-Unis seulement)"),
    ("marche", "autre", "marché non accessible (hors Europe)"),
    ("competences", ("dev_ia", "reglementaire_lourd"), "exige réglementaire lourd"),
    ("competences", ("materiel_industriel",), "exige matériel industriel"),
    ("taille", "systemique", "problème systémique"),
])
def test_chaque_critere_exclut_avec_un_motif_lisible(champ, valeur, attendu):
    r = evaluer_accessibilite(_bloc(**{champ: valeur}))
    assert r.accessible_solo is False
    assert r.motif_exclusion == attendu


def test_le_motif_liste_tous_les_criteres_qui_echouent():
    r = evaluer_accessibilite(_bloc(invest="plus_de_100k", marche="autre", competences=("materiel_industriel",)))
    assert r.accessible_solo is False
    assert r.motif_exclusion.count(";") == 2
    assert "investissement" in r.motif_exclusion and "marché" in r.motif_exclusion and "matériel" in r.motif_exclusion


def test_sans_bloc_jamais_accessible_par_defaut():
    r = evaluer_accessibilite(None)
    assert r.accessible_solo is None and r.motif_exclusion == MOTIF_NON_EVALUEE


def test_le_bloc_est_toujours_type_hypothese_et_refuse_une_valeur_hors_liste():
    assert FaisabiliteSortie.model_validate(_bloc()).type == "hypothese"
    with pytest.raises(Exception):
        FaisabiliteSortie.model_validate({**_bloc(), "type": "observe"})
    with pytest.raises(Exception):
        FaisabiliteSortie.model_validate(_bloc(invest="beaucoup"))


def test_le_bloc_ne_touche_jamais_au_score_de_preuve():
    poids = cfg.poids_scoring()
    sans = AnalystSortie.model_validate(_analyse(forts=3))
    avec_ok = AnalystSortie.model_validate(_analyse(forts=3, faisabilite=_bloc()))
    avec_ko = AnalystSortie.model_validate(_analyse(
        forts=3, faisabilite=_bloc(invest="plus_de_100k", marche="autre", taille="systemique")))
    from app.scoring.engine import InfoSource
    infos = {"s-origine": InfoSource("news.ycombinator.com", True), "s-autre": InfoSource("exemple.test")}
    assert calculer_score(sans.criteres, poids, infos) == calculer_score(avec_ok.criteres, poids, infos) \
        == calculer_score(avec_ko.criteres, poids, infos)


def test_le_schema_de_l_analyst_demande_le_bloc_et_reste_compatible_mode_strict():
    schema = rendre_schema_strict(AnalystSortie.model_json_schema())
    assert "faisabilite" in schema["properties"]

    def objets(n):
        if isinstance(n, dict):
            if n.get("type") == "object" or "properties" in n:
                yield n
            for v in n.values():
                yield from objets(v)
        elif isinstance(n, list):
            for v in n:
                yield from objets(v)
    assert all(o.get("additionalProperties") is False for o in objets(schema))


def test_le_prompt_de_l_analyst_demande_les_cinq_champs():
    for champ in ("investissement_initial", "delai_premier_revenu", "marche", "competences", "taille_du_probleme"):
        assert champ in role_analyst.PROMPT_SYSTEME
    assert role_analyst.VERSION_PROMPT == "analyst-v2"


# ------------------------------------------- 3. recalcul + archivage ---

def test_le_recalcul_ajoute_une_ligne_et_garde_l_ancienne(engine_test):
    opp = _dossier(engine_test, forts=7, ancien_score=95.0)
    plan = recalcul.planifier_recalcul(engine_test)
    bilan = recalcul.appliquer_recalcul(engine_test, plan)
    lignes = repo.historique_scores(engine_test, opp)
    assert bilan == {"recalcules": 1, "archives": 0}
    assert len(lignes) == 2
    assert lignes[0]["score_prudent"] == 95.0 and lignes[0]["origine"] is None  # l'ancienne, intacte
    assert lignes[1]["origine"] == "recalcul_4_1" and lignes[1]["version_poids"] == cfg.poids_scoring()["version"]
    assert lignes[1]["score_prudent"] == 100.0  # 7 critères forts sur 2 sources distinctes
    assert lignes[1]["decision_critic"] == "a_verifier"  # décision du Critic reportée telle quelle
    assert _statut(engine_test, opp) == "incertain"


def test_sous_50_le_dossier_passe_archive_faible_avec_trace_et_sans_suppression(engine_test):
    faible = _dossier(engine_test, forts=1, statut="rejete", ancien_score=88.0)  # 1 critère fort = 20 pts max
    plan = recalcul.planifier_recalcul(engine_test)
    assert plan.lignes[0].nouveau.score_prudent < 50
    recalcul.appliquer_recalcul(engine_test, plan)
    assert _statut(engine_test, faible) == "archive_faible"
    with engine_test.connect() as cx:
        traces = cx.execute(select(decisions.c.action, decisions.c.justification)
                            .where(decisions.c.opportunity_id == faible)).all()
        assert cx.execute(select(opportunities.c.id).where(opportunities.c.id == faible)).first() is not None
    assert traces[0][0] == "archive_faible" and "statut avant : rejete" in traces[0][1]
    assert len(repo.historique_scores(engine_test, faible)) == 2  # ancien score conservé


def test_le_seuil_est_inclusif_50_reste_liste(engine_test):
    opp = _dossier(engine_test, forts=0, ancien_score=10.0)
    plan = recalcul.planifier_recalcul(engine_test)
    plan.lignes[0].nouveau.score_prudent = 50.0
    recalcul.appliquer_recalcul(engine_test, plan)
    assert _statut(engine_test, opp) == "incertain"


def test_le_recalcul_est_idempotent(engine_test):
    _dossier(engine_test, forts=1, ancien_score=80.0)
    recalcul.appliquer_recalcul(engine_test, recalcul.planifier_recalcul(engine_test))
    plan2 = recalcul.planifier_recalcul(engine_test)
    assert plan2.lignes == [] and plan2.deja_recalcules == 1


def test_a_reprendre_et_dossiers_sans_analyse_ne_sont_pas_touches(engine_test):
    a_reprendre = _dossier(engine_test, statut="a_reprendre", forts=0)
    sans = _dossier(engine_test, avec_analyse=False, statut="nouveau")
    plan = recalcul.planifier_recalcul(engine_test)
    recalcul.appliquer_recalcul(engine_test, plan)
    assert plan.sans_analyse == 1 and plan.lignes == []
    assert _statut(engine_test, a_reprendre) == "a_reprendre" and _statut(engine_test, sans) == "nouveau"


def test_le_recalcul_prend_la_derniere_analyse(engine_test):
    opp = _dossier(engine_test, forts=0, ancien_score=10.0)
    time.sleep(0.01)
    repo.inserer_assessment(engine_test, opportunity_id=opp, run_id="run-b", role="analyst", payload=_analyse(forts=0),
                            modele="claude-sonnet-5", version_prompt="analyst-v2", inconnues=[])
    assert recalcul.planifier_recalcul(engine_test).lignes[0].run_id == "run-b"


def test_analyse_illisible_ignoree_sans_planter(engine_test):
    opp = _dossier(engine_test, avec_analyse=False)
    repo.inserer_assessment(engine_test, opportunity_id=opp, run_id="r", role="analyst", payload={"vieux": "format"},
                            modele="m", version_prompt="analyst-v0", inconnues=[])
    plan = recalcul.planifier_recalcul(engine_test)
    assert plan.analyses_illisibles == [opp] and plan.lignes == []


def test_recalcul_au_demarrage_seulement_si_la_variable_vaut_1(engine_test, monkeypatch):
    opp = _dossier(engine_test, forts=0, ancien_score=90.0)
    monkeypatch.delenv(recalcul.VARIABLE_RECALCUL, raising=False)
    assert recalcul.recalcul_au_demarrage(engine_test) is None
    assert len(repo.historique_scores(engine_test, opp)) == 1
    monkeypatch.setenv(recalcul.VARIABLE_RECALCUL, "1")
    message = recalcul.recalcul_au_demarrage(engine_test)
    assert "1 dossier(s) recalculé(s)" in message and "1 passé(s) `archive_faible`" in message
    assert _statut(engine_test, opp) == "archive_faible"


def test_un_recalcul_rate_ne_leve_jamais(engine_test, monkeypatch):
    monkeypatch.setenv(recalcul.VARIABLE_RECALCUL, "1")
    monkeypatch.setattr(recalcul, "planifier_recalcul", lambda e: 1 / 0)
    assert "ÉCHOUÉ" in recalcul.recalcul_au_demarrage(engine_test)


def test_migration_additive_sur_une_base_d_avant_4_1(tmp_path):
    from sqlalchemy import create_engine
    moteur = create_engine(f"sqlite:///{tmp_path / 'vieille.db'}", future=True)
    with moteur.begin() as cx:
        cx.execute(text("CREATE TABLE scores (id VARCHAR PRIMARY KEY, opportunity_id VARCHAR NOT NULL, run_id VARCHAR NOT NULL, "
                        "version_poids VARCHAR NOT NULL, valeurs_json JSON NOT NULL, score_brut FLOAT NOT NULL, "
                        "score_prudent FLOAT NOT NULL, couverture_preuves FLOAT NOT NULL, flags_json JSON NOT NULL, "
                        "decision_critic VARCHAR, date_creation DATETIME NOT NULL)"))
        cx.execute(text("INSERT INTO scores VALUES ('s1','o1','r','v',  '{}', 1, 1, 1, '[]', NULL, '2026-09-01 00:00:00')"))
    migrer(moteur)
    migrer(moteur)  # idempotent
    with moteur.connect() as cx:
        assert cx.execute(select(scores.c.id, scores.c.origine)).all() == [("s1", None)]
        assert cx.execute(select(faisabilites)).all() == []


def test_lecture_seule_avant_migration_ne_plante_pas(tmp_path):
    """Simulation sur la base de production AVANT le déploiement : ni la
    colonne `scores.origine` ni la table `faisabilites` n'existent."""
    from sqlalchemy import create_engine
    moteur = create_engine(f"sqlite:///{tmp_path / 'vieille.db'}", future=True)
    migrer(moteur)
    with moteur.begin() as cx:
        cx.execute(text("DROP TABLE faisabilites"))
        cx.execute(text("ALTER TABLE scores DROP COLUMN origine"))
    assert repo.dernier_score_par_dossier(moteur) == {}
    assert repo.dossiers_avec_faisabilite(moteur) == set()


# ------------------------------------------------ 4. faisabilité reprise ---

class _ClientFaux:
    """Répond à `appeler_structure` comme le vrai client : une instance du schéma demandé."""

    def __init__(self, bloc: dict | None = _bloc(), erreur: Exception | None = None):
        self.bloc, self.erreur, self.appels = bloc, erreur, []

    def appeler_structure(self, *, schema, role, opportunity_id, **kw):
        self.appels.append((role, opportunity_id, kw["modele"]))
        if self.erreur:
            raise self.erreur
        return None if self.bloc is None else schema.model_validate({"faisabilite": self.bloc})


def test_la_reprise_ne_cible_que_les_dossiers_50_plus_non_archives_sans_bloc(engine_test):
    ok = _dossier(engine_test, forts=7)
    _dossier(engine_test, forts=1)  # < 50
    archive = _dossier(engine_test, forts=7, statut="archive_faible")
    a_reprendre = _dossier(engine_test, forts=7, statut="a_reprendre")
    deja = _dossier(engine_test, forts=7)
    repo.inserer_faisabilite(engine_test, opportunity_id=deja, run_id=None, origine="analyse", payload=_bloc(),
                             accessible_solo=True, motif_exclusion=None, modele="m")
    recalcul.appliquer_recalcul(engine_test, recalcul.planifier_recalcul(engine_test))
    # après recalcul : les deux dossiers à 7 critères forts ont gardé 100, celui à 1 critère est archivé
    ids = [o["id"] for o in recalcul.dossiers_pour_faisabilite(engine_test)]
    assert ids == [ok]
    assert archive not in ids and a_reprendre not in ids and deja not in ids


def test_la_phase_ecrit_le_bloc_et_le_drapeau_deduit_par_le_code(engine_test, monkeypatch):
    opp = _dossier(engine_test, forts=7)
    recalcul.appliquer_recalcul(engine_test, recalcul.planifier_recalcul(engine_test))
    client = _ClientFaux(_bloc(invest="plus_de_100k"))
    monkeypatch.setenv(recalcul.VARIABLE_FAISABILITE, "1")
    resume = orch.ResumeRun()
    orch._phase_faisabilite(engine_test, "run-f", quotas=cfg.quotas(), settings=cfg.get_settings(),
                            model_client=client, resume=resume, debut=time.monotonic(), duree_max=3600)
    with engine_test.connect() as cx:
        ligne = cx.execute(select(faisabilites)).mappings().one()
    assert client.appels == [("faisabilite", opp, "claude-sonnet-5")]
    assert ligne["opportunity_id"] == opp and ligne["origine"] == "reprise_4_1"
    assert ligne["accessible_solo"] is False and ligne["motif_exclusion"].startswith("investissement trop élevé")
    assert ligne["payload_json"]["type"] == "hypothese"
    # rejouée : plus rien à faire
    orch._phase_faisabilite(engine_test, "run-f", quotas=cfg.quotas(), settings=cfg.get_settings(),
                            model_client=client, resume=resume, debut=time.monotonic(), duree_max=3600)
    assert len(client.appels) == 1


def test_la_phase_est_eteinte_sans_la_variable(engine_test, monkeypatch):
    _dossier(engine_test, forts=7)
    recalcul.appliquer_recalcul(engine_test, recalcul.planifier_recalcul(engine_test))
    monkeypatch.delenv(recalcul.VARIABLE_FAISABILITE, raising=False)
    client = _ClientFaux()
    orch._phase_faisabilite(engine_test, "r", quotas=cfg.quotas(), settings=cfg.get_settings(), model_client=client,
                            resume=orch.ResumeRun(), debut=time.monotonic(), duree_max=3600)
    assert client.appels == []


def test_budget_depasse_arrete_la_reprise_sans_rien_inventer(engine_test, monkeypatch):
    _dossier(engine_test, forts=7)
    recalcul.appliquer_recalcul(engine_test, recalcul.planifier_recalcul(engine_test))
    monkeypatch.setenv(recalcul.VARIABLE_FAISABILITE, "1")
    resume = orch.ResumeRun()
    orch._phase_faisabilite(engine_test, "r", quotas=cfg.quotas(), settings=cfg.get_settings(),
                            model_client=_ClientFaux(erreur=BudgetDepasse("plafond")), resume=resume,
                            debut=time.monotonic(), duree_max=3600)
    assert resume.budget_atteint is True
    with engine_test.connect() as cx:
        assert cx.execute(select(faisabilites)).all() == []


def test_sortie_invalide_jamais_de_valeur_inventee(engine_test, monkeypatch):
    _dossier(engine_test, forts=7)
    recalcul.appliquer_recalcul(engine_test, recalcul.planifier_recalcul(engine_test))
    monkeypatch.setenv(recalcul.VARIABLE_FAISABILITE, "1")
    orch._phase_faisabilite(engine_test, "r", quotas=cfg.quotas(), settings=cfg.get_settings(),
                            model_client=_ClientFaux(bloc=None), resume=orch.ResumeRun(),
                            debut=time.monotonic(), duree_max=3600)
    with engine_test.connect() as cx:
        assert cx.execute(select(faisabilites)).all() == []


def test_la_faisabilite_compte_dans_le_plafond_d_appels_approfondis(engine_test):
    run = repo.creer_run(engine_test, mode="reel", version_code="t", version_config="t", quotas={})
    budget = BudgetTracker(engine_test, run, plafond_eur=5.0, plafond_appels_approfondis=1)
    budget.verifier_et_engager(0.01, role="faisabilite")
    with pytest.raises(BudgetDepasse):
        budget.verifier_et_engager(0.01, role="analyst")


def test_estimation_du_cout_de_la_reprise_sans_appel_modele(engine_test):
    _dossier(engine_test, forts=7)
    _dossier(engine_test, forts=7)
    recalcul.appliquer_recalcul(engine_test, recalcul.planifier_recalcul(engine_test))
    candidats = recalcul.dossiers_pour_faisabilite(engine_test)
    est = recalcul.estimer_cout_reprise_faisabilite(engine_test, candidats, "claude-sonnet-5")
    assert est["dossiers"] == 2 and est["tokens_entree"] > 0 and est["tokens_sortie"] == 1400
    assert est["cout_estime_eur"] == round(estimer_cout_eur("claude-sonnet-5", est["tokens_entree"], 1400), 4)
    assert 0 < est["cout_estime_eur"] < 0.05


# ------------------------------- pipeline : nouvelle analyse (bout en bout) ---

class _ClientPipeline:
    """Analyst : `forts` critères forts + un bloc de faisabilité ; Critic : a_verifier."""

    def __init__(self, forts: int, bloc: dict):
        self.forts, self.bloc = forts, bloc

    def appeler_structure(self, *, schema, role, opportunity_id, **kw):
        if role == "analyst":
            payload = _analyse(forts=self.forts, faisabilite=self.bloc)
            payload["opportunity_id"] = opportunity_id
            return schema.model_validate(payload)
        return schema.model_validate({
            "opportunity_id": opportunity_id, "objections": [], "faits_contestes": [],
            "recherches_supplementaires": [], "decision": "a_verifier", "motif": "m"})


def _pipeline(engine, opp, client):
    # `_analyse` cite "s-origine"/"s-autre" : on les enregistre comme sources du dossier.
    for sid, domaine, claim in (("s-origine", "news.ycombinator.com", "Scout: s"), ("s-autre", "www.exemple.test", "Enquête: p")):
        from app.storage.schema import sources
        with engine.begin() as cx:
            from sqlalchemy import insert
            from datetime import datetime, timezone
            cx.execute(insert(sources).values(
                id=sid, url_canonique=f"https://{domaine}/{sid}", domaine=domaine, date_collecte=datetime.now(timezone.utc),
                type="rss", extrait="e", empreinte=f"emp-{sid}", droits_collecte="t"))
        repo.inserer_evidence(engine, opportunity_id=opp, source_id=sid, claim=claim, type_="non_verifie", independant=True)
    orch._phase_analyse_et_critique(
        engine, "run-p", options=orch.OptionsRun(mode="reel"), quotas=cfg.quotas(), poids_config=cfg.poids_scoring(),
        settings=cfg.get_settings(), model_client=client, resume=orch.ResumeRun(), debut=time.monotonic(), duree_max=3600)


def test_nouvelle_analyse_range_la_faisabilite_et_garde_un_dossier_solide(engine_test):
    opp = repo.creer_opportunite(engine_test, titre="t", acheteur="a", probleme="p", mecanisme_ia="m",
                                 secteur="intersectoriel", statut="enquete_terminee", cluster_id=None)
    _pipeline(engine_test, opp, _ClientPipeline(forts=7, bloc=_bloc()))
    with engine_test.connect() as cx:
        f = cx.execute(select(faisabilites)).mappings().one()
    assert f["origine"] == "analyse" and f["accessible_solo"] is True and f["motif_exclusion"] is None
    assert _statut(engine_test, opp) == "incertain"
    assert repo.historique_scores(engine_test, opp)[-1]["score_prudent"] == 100.0


def test_nouvelle_analyse_sous_50_est_archivee_d_office(engine_test):
    opp = repo.creer_opportunite(engine_test, titre="t", acheteur="a", probleme="p", mecanisme_ia="m",
                                 secteur="intersectoriel", statut="enquete_terminee", cluster_id=None)
    _pipeline(engine_test, opp, _ClientPipeline(forts=1, bloc=_bloc()))
    assert _statut(engine_test, opp) == "archive_faible"
    assert len(repo.historique_scores(engine_test, opp)) == 1  # score conservé
