"""V2.8 (RADAR-V2.md) : cycle du worker (cartographie initiale, régime quotidien, priorités, enveloppe, plafond du jour, reprise).
Aucun réseau, aucun modèle réel, 0 € : les frontières (API France Travail, API Recherche d'entreprises, modèle) sont simulées ; les
fonctions de collecte, d'étiquetage, d'agrégation et de fiches sont les VRAIES."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app import config as cfg
from app import cycle_v2 as cyc
from app import etablissements as etab
from app import etiquetage as etiq
from app import fiches as fch
from app import referentiels
from app.adapters import france_travail as ft
from app.adapters import recherche_entreprises as api
from app.models_schemas import FaisabiliteSortie
from app.pipeline.budget import BudgetDepasse, BudgetTracker
from app.storage import repo
from app.storage.schema import collectes_offres, etablissements_secteur, fiches_secteur_tache, offres_emploi, offres_etiquetage, prospection, runs

MAINTENANT = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)
SECTEURS = referentiels.secteurs_tpe()
P1 = SECTEURS.codes_par_priorite(1)
P2 = SECTEURS.codes_par_priorite(2)
P3 = SECTEURS.codes_par_priorite(3)
CODE_P1 = "69.20Z"
DESCRIPTION = ("Vous assurez la relance des impayés auprès de nos clients, la saisie de factures fournisseurs et la préparation de la paie. "
               "Poste en CDI à Bordeaux, dans un cabinet de dix personnes.")


# ------------------------------------------------------------------ priorités des secteurs -----

def test_chaque_secteur_a_une_priorite_explicite_de_1_a_3():
    assert all(s.priorite in (1, 2, 3) for s in SECTEURS.secteurs)
    assert set(P1) | set(P2) | set(P3) == {s.code for s in SECTEURS.non_exclus()}
    assert not (set(P1) & set(P2)) and not (set(P2) & set(P3)) and not (set(P1) & set(P3))


def test_priorite_1_est_une_vingtaine_de_secteurs_de_l_univers_de_mathieo():
    assert 18 <= len(P1) <= 25
    familles = {SECTEURS.par_code()[c].famille for c in P1}
    assert familles <= {"batiment", "immobilier", "droit_chiffre_conseil", "restauration_hebergement", "tourisme_loisirs", "nautisme",
                        "agriculture_viticulture", "sante_liberale", "services_entreprises"}
    # chaque grande famille voulue est représentée (bâtiment, immobilier, comptabilité, restauration/hébergement, nautisme, vigne, santé, entreprises)
    assert {"batiment", "immobilier", "droit_chiffre_conseil", "restauration_hebergement", "nautisme", "agriculture_viticulture",
            "sante_liberale", "services_entreprises"} <= familles
    assert CODE_P1 in P1  # expertise comptable


def test_aucun_secteur_exclu_n_est_en_priorite_1():
    assert not [s.code for s in SECTEURS.secteurs if s.exclusion and s.code in P1]


def test_ordre_de_couverture_1_puis_2_puis_3():
    ordre = SECTEURS.codes_dans_l_ordre()
    assert ordre[:len(P1)] == P1 and ordre[len(P1):len(P1) + len(P2)] == P2 and ordre[-len(P3):] == P3


def test_priorite_absente_ou_hors_borne_est_refusee():
    from pydantic import ValidationError
    base = {"code": "69.20Z", "libelle": "Activités comptables", "famille": "droit_chiffre_conseil"}
    with pytest.raises(ValidationError):
        referentiels.SecteurTpe(**base)
    with pytest.raises(ValidationError):
        referentiels.SecteurTpe(**base, priorite=4)
    assert referentiels.SecteurTpe(**base, priorite=2).priorite == 2


# ------------------------------------------------------------------ doublures -----

class Journal:
    """Enregistre les appels du cycle, dans l'ordre."""

    def __init__(self):
        self.appels: list[tuple[str, dict]] = []

    def ajouter(self, nom: str, **kw):
        self.appels.append((nom, kw))

    def noms(self):
        return [n for n, _ in self.appels]

    def de(self, nom):
        return [kw for n, kw in self.appels if n == nom]


def operations_vides(journal: Journal, *, cout_fiches=0.0) -> cyc.Operations:
    """Opérations qui ne font rien mais comptent ce qu'on leur demande."""
    def faire(nom, **defauts):
        def op(engine, **kw):
            journal.ajouter(nom, **kw)
            return SimpleNamespace(**defauts)
        return op
    return cyc.Operations(
        collecter_offres=faire("offres", requetes=3, codes_en_echec=0, arret=None),
        rafraichir_etablissements=faire("etablissements", requetes=5, paires_en_echec=0, arret=None),
        etiqueter_offres=faire("etiquetage", offres_prevues=0, offres_traitees=0, arret=None),
        calculer_agregats=faire("agregation"),
        produire_fiches=faire("fiches", arret=None, fiches_ecrites=0),
    )


# ------------------------------------------------------------------ régime quotidien : priorités et plafond -----

def test_regime_quotidien_collecte_mesure_et_etiquette_dans_l_ordre_des_priorites(engine_test):
    j = Journal()
    cyc.cycle_quotidien(engine_test, operations_vides(j), horloge=lambda: MAINTENANT)
    for nom in ("offres", "etablissements"):
        assert [tuple(kw["codes"]) for kw in j.de(nom)] == [P1, P2, P3], nom
    etiq_codes = [tuple(kw["codes"]) for kw in j.de("etiquetage")]
    assert etiq_codes == [P1, P2, P3]
    # fiches : avant l'étiquetage du jour, dans l'ordre des priorités, jamais d'enveloppe en régime de croisière
    assert j.noms().index("fiches") < j.noms().index("etiquetage")
    fiches = j.de("fiches")[0]
    assert fiches["ordre_codes"] == SECTEURS.codes_dans_l_ordre() and fiches["enveloppe"] is None
    assert all(kw["enveloppe"] is None and kw["plafond_jour_eur"] == 2.0 for kw in j.de("etiquetage"))


def test_fiches_ne_prennent_qu_une_part_du_plafond_du_jour(engine_test):
    j = Journal()
    cyc.cycle_quotidien(engine_test, operations_vides(j), horloge=lambda: MAINTENANT)
    assert j.de("fiches")[0]["plafond_jour_eur"] == pytest.approx(0.8)  # 0,4 x 2 € (rien dépensé avant)


def test_plafond_du_jour_atteint_arrete_les_tranches_suivantes(engine_test):
    j = Journal()
    ops = operations_vides(j)
    ops.etiqueter_offres = lambda engine, **kw: (j.ajouter("etiquetage", **kw), SimpleNamespace(offres_prevues=40, offres_traitees=12, arret="budget : Plafond de 2.00 €/jour dépassé"))[1]
    bilan = cyc.cycle_quotidien(engine_test, ops, horloge=lambda: MAINTENANT)
    assert len(j.de("etiquetage")) == 1  # P1 a épuisé le budget : P2 et P3 attendent demain
    assert bilan.plafond_atteint and bilan.travail_restant and not bilan.erreur_systemique


def test_les_etapes_du_jour_terminees_ne_sont_pas_refaites_a_la_passe_suivante(engine_test):
    j = Journal()
    ops = operations_vides(j)
    cyc.cycle_quotidien(engine_test, ops, horloge=lambda: MAINTENANT)
    n_offres = len(j.de("offres"))
    cyc.cycle_quotidien(engine_test, ops, horloge=lambda: MAINTENANT + timedelta(hours=2))
    assert len(j.de("offres")) == n_offres, "la collecte du jour est faite : elle n'est pas relancée"
    jour = MAINTENANT.date()
    runs_du_jour = [r for r in _runs(engine_test) if r["mode"] == cyc.MODE_QUOTIDIEN]
    assert len(runs_du_jour) == 1 and runs_du_jour[0]["resume_json"]["passes"] == 2 and jour.isoformat() == runs_du_jour[0]["resume_json"]["jour"]


def test_collecte_incomplete_est_reessayee_a_la_passe_suivante(engine_test):
    j = Journal()
    ops = operations_vides(j)
    ops.collecter_offres = lambda engine, **kw: (j.ajouter("offres", **kw), SimpleNamespace(requetes=3, codes_en_echec=0, arret="plafond de 3000 requêtes atteint"))[1]
    b = cyc.cycle_quotidien(engine_test, ops, horloge=lambda: MAINTENANT)
    assert b.travail_restant
    n = len(j.de("offres"))
    cyc.cycle_quotidien(engine_test, ops, horloge=lambda: MAINTENANT + timedelta(hours=1))
    assert len(j.de("offres")) > n


def test_un_nouveau_jour_cloture_le_run_de_la_veille_et_en_ouvre_un_autre(engine_test):
    j = Journal()
    cyc.cycle_quotidien(engine_test, operations_vides(j), horloge=lambda: MAINTENANT)
    cyc.cycle_quotidien(engine_test, operations_vides(j), horloge=lambda: MAINTENANT + timedelta(days=1))
    mes = sorted((r for r in _runs(engine_test) if r["mode"] == cyc.MODE_QUOTIDIEN), key=lambda r: r["debut"])
    assert [r["statut"] for r in mes] == ["termine", "en_cours"]
    assert len(j.de("offres")) == 6  # 3 tranches x 2 jours : la collecte quotidienne est refaite chaque jour


def test_une_etape_en_erreur_n_arrete_ni_les_suivantes_ni_le_cycle(engine_test):
    j = Journal()
    ops = operations_vides(j)

    def casse(engine, **kw):
        raise RuntimeError("API en panne")
    ops.rafraichir_etablissements = casse
    b = cyc.cycle_quotidien(engine_test, ops, horloge=lambda: MAINTENANT)
    assert "etiquetage" in j.noms() and b.erreur_systemique


def test_le_resume_d_avancement_est_ecrit_dans_runs_et_dans_les_logs(engine_test, caplog):
    caplog.set_level("INFO")
    cyc.cycle_quotidien(engine_test, operations_vides(Journal()), horloge=lambda: MAINTENANT)
    run = next(r for r in _runs(engine_test) if r["mode"] == cyc.MODE_QUOTIDIEN)
    resume = run["resume_json"]
    assert resume["phase"] == "quotidien" and resume["journal"] and "avancement" in resume
    assert any("[offres]" in ligne for ligne in resume["journal"]) and any("[bilan]" in ligne for ligne in resume["journal"])
    assert "depense_modele_du_jour_eur" in resume["avancement"]
    assert any("[cycle v2]" in r.message and "[etiquetage_p1]" in r.message for r in caplog.records)


def _runs(engine):
    with engine.connect() as cx:
        return [dict(r) for r in cx.execute(select(runs)).mappings()]


# ------------------------------------------------------------------ boucle du worker -----

def test_worker_pause_all_ne_fait_rien(engine_test, monkeypatch):
    monkeypatch.setenv("RADAR_PAUSE_ALL", "1")
    cfg.get_settings.cache_clear()
    j = Journal()
    assert cyc.executer_cycle_v2(engine_test, ops=operations_vides(j), une_iteration=True, horloge=lambda: MAINTENANT) == {"pause": True}
    assert j.appels == []


def test_worker_sans_cartographie_initiale_demandee_va_droit_au_regime_quotidien(engine_test):
    j = Journal()
    r = cyc.executer_cycle_v2(engine_test, ops=operations_vides(j), une_iteration=True, horloge=lambda: MAINTENANT, environ={})
    assert r["initiale_faite"] is None and "etiquetage" in j.noms()
    assert not [x for x in _runs(engine_test) if x["mode"] == cyc.MODE_INITIALE]


def test_attente_jusqu_a_minuit_utc():
    assert cyc.secondes_jusqu_a_minuit_utc(datetime(2026, 10, 2, 23, 0, tzinfo=timezone.utc)) == pytest.approx(3660)


def test_la_boucle_dort_jusqu_a_minuit_quand_le_plafond_est_atteint(engine_test):
    class Horloge:
        def __init__(self):
            self.t = MAINTENANT
            self.sommeils: list[float] = []

        def __call__(self):
            return self.t

        def dormir(self, s):
            self.sommeils.append(s)
            self.t += timedelta(seconds=s)
            if len(self.sommeils) > 400:
                raise KeyboardInterrupt  # garde-fou de test : jamais de boucle infinie
    h = Horloge()
    j = Journal()
    ops = operations_vides(j)
    ops.etiqueter_offres = lambda engine, **kw: SimpleNamespace(offres_prevues=5, offres_traitees=1, arret="budget : plafond")
    with pytest.raises(KeyboardInterrupt):
        cyc.executer_cycle_v2(engine_test, ops=ops, dormir=h.dormir, horloge=h, environ={})
    # première attente découpée en tranches d'une minute ; le cycle suivant n'a lieu qu'au lendemain (pas toutes les 30 minutes)
    jours = {kw_run["resume_json"]["jour"] for kw_run in _runs(engine_test) if kw_run["mode"] == cyc.MODE_QUOTIDIEN}
    assert jours == {"2026-10-02", "2026-10-03"} or jours == {"2026-10-02"}
    assert len(j.de("offres")) in (3, 6)


# ------------------------------------------------------------------ interrupteur du pipeline v1 -----

def test_run_forever_lance_le_cycle_v2_et_jamais_le_pipeline_v1(monkeypatch):
    from app import cli
    appels = []
    monkeypatch.setattr(cyc, "executer_cycle_v2", lambda engine, **kw: appels.append("v2"))
    import app.pipeline.orchestrator as orch
    monkeypatch.setattr(orch, "executer_continu", lambda engine, **kw: appels.append("v1"))
    assert cfg.cycle_v2()["pipeline_v1_actif"] is False
    assert cli.main(["run-forever"]) == 0
    assert appels == ["v2"]


def test_pipeline_v1_reste_rallumable_par_config(monkeypatch):
    from app import cli
    appels = []
    monkeypatch.setattr(cyc, "executer_cycle_v2", lambda engine, **kw: appels.append("v2"))
    import app.pipeline.orchestrator as orch
    monkeypatch.setattr(orch, "executer_continu", lambda engine, **kw: appels.append("v1"))
    reel = cfg.cycle_v2()
    monkeypatch.setattr(cfg, "cycle_v2", lambda: {**reel, "pipeline_v1_actif": True})
    assert cli.main(["run-forever"]) == 0
    assert appels == ["v1"]


def test_les_migrations_du_demarrage_sont_additives_et_ne_touchent_a_aucune_donnee(engine_test):
    from app.storage.db import migrer
    run_id = repo.creer_run(engine_test, mode="reel", version_code="v1", version_config="x", quotas={})
    migrer(engine_test)
    migrer(engine_test)
    assert repo.get_run(engine_test, run_id)["mode"] == "reel"


# ------------------------------------------------------------------ cartographie initiale : doublures de bout en bout -----

class Reseau:
    """API France Travail, API Recherche d'entreprises : simulées. Compte les appels par code NAF."""

    def __init__(self):
        self.codes_offres: list[str] = []
        self.paires: list[tuple[str, str]] = []
        self.pannes_offres = 0


@pytest.fixture
def reseau(monkeypatch):
    r = Reseau()
    monkeypatch.setattr(ft, "obtenir_jeton", lambda **kw: SimpleNamespace(valeur="x", valide=lambda: True))

    def collecter_code(jeton, code_naf, debut, fin, *, engine=None, **kw):
        r.codes_offres.append(code_naf)
        if r.pannes_offres > 0:
            r.pannes_offres -= 1
            from app.adapters.http import ErreurCollecte
            raise ErreurCollecte("panne simulée")
        offres = []
        if code_naf == CODE_P1:
            offres = [{
                "id": f"OFF{n:05d}", "intitule": "Assistant comptable (H/F)", "description": DESCRIPTION,
                "dateCreation": (MAINTENANT - timedelta(days=n % 30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "lieuTravail": {"commune": "33063", "codePostal": "33000"}, "codeNAF": code_naf,
                "salaire": {"libelle": "Annuel de 30000.0 Euros"}} for n in range(70)]
        return ft.Collecte(code_naf=code_naf, offres=offres, requetes=1)
    monkeypatch.setattr(ft, "collecter_code", collecter_code)

    def collecter_echantillon(code_naf, departement, *, plafond=500, engine=None, **kw):
        r.paires.append((code_naf, departement))
        prospects = []
        if code_naf == CODE_P1 and departement == "33":
            prospects = [api.Prospect(siret=f"6920133{i:07d}", siren=None, raison_sociale=f"Cabinet {i}", code_naf=code_naf, adresse="1 rue", code_postal="33000",
                                       code_commune="33063", commune="Bordeaux", latitude=44.84, longitude=-0.58, tranche_effectif_salarie="02",
                                       categorie_entreprise="PME", est_siege=True) for i in range(40)]
        return api.Echantillon(code_naf=code_naf, departement=departement, total_entreprises=len(prospects), prospects=prospects, pages_lues=1,
                               complet=True, url_premiere_page="https://exemple.invalid")
    monkeypatch.setattr(api, "collecter_echantillon", collecter_echantillon)
    monkeypatch.setattr(api, "compter_entreprises_actives", lambda code_naf, dep=None, *, engine=None: SimpleNamespace(total_resultats=40, plafonne=False, url="https://exemple.invalid"))
    return r


COUT_ETIQUETAGE, COUT_ANALYSTE, COUT_CRITIC = 0.0033, 0.035, 0.029


class ClientQuiCoute:
    """Remplace ModelClient : réponses valides, MAIS chaque appel est journalisé avec un coût (donc le plafond du jour et l'enveloppe
    s'appliquent pour de vrai, comme avec le client réel)."""

    def __init__(self, engine, plafond):
        run = repo.creer_run(engine, mode="test", version_code="x", version_config="x", quotas={})
        self.budget = BudgetTracker(engine, run, plafond, plafond_appels_approfondis=0)
        self.engine = engine
        self.appels: list[str] = []

    def appeler_structure(self, *, role, schema, **kw):
        cout = {etiq.ROLE_ETIQUETEUR: COUT_ETIQUETAGE, fch.ROLE_ANALYSTE: COUT_ANALYSTE, fch.ROLE_CRITIC: COUT_CRITIC}[role]
        self.budget.verifier_et_engager(cout, role=role)  # lève BudgetDepasse comme le vrai client
        self.budget.enregistrer_reel(fournisseur="anthropic", modele_ou_actor="faux", appels=1, tokens_in=1, tokens_out=1, cout_reel=cout,
                                     cout_estime_engage=cout, role=role)
        self.appels.append(role)
        if role == etiq.ROLE_ETIQUETEUR:
            return etiq.EtiquetageOffre(taches=[])
        if role == fch.ROLE_ANALYSTE:
            return fch.FicheAnalyste.model_validate({
                "service_ia_propose": "Relances automatiques des clients en retard de paiement, par courriel.",
                "ce_quil_remplace": "Les relances manuelles faites par le secrétariat du cabinet.",
                "affirmations": [{"texte": "La tâche est mentionnée par 100 % des offres du secteur.", "source_id": "agregat"}],
                "hypothese_prix": {"montant_min_eur": 150, "montant_max_eur": 300, "periodicite": "par_mois", "justification": "Abonnement mensuel (hypothèse)."},
                "faisabilite": FaisabiliteSortie.model_validate({
                    "type": "hypothese", "investissement_initial": {"valeur": "moins_de_5k", "justification": "Outils en ligne."},
                    "delai_premier_revenu": {"valeur": "moins_de_3_mois", "justification": "Pilote rapide."},
                    "marche": {"valeur": "accessible_depuis_france", "justification": "Cibles françaises."},
                    "competences": {"valeurs": ["dev_ia", "vente"], "justification": "Intégration et vente."},
                    "taille_du_probleme": {"valeur": "segment_pme", "justification": "Des centaines d'établissements."}}).model_dump(mode="json"),
                "personnes_necessaires": "deux", "prochain_test": "Appeler dix cabinets de la liste et leur poser deux questions simples sur la relance des clients.", "preuves_manquantes": ["concurrence non évaluée"]})
        return fch.CritiqueFiche.model_validate({"objections": [], "decision": "poursuivre", "motif": "rien de bloquant"})


def operations_reelles(engine, journal: Journal, *, plafond_client=50.0) -> cyc.Operations:
    """Les VRAIES fonctions du cycle ; seul le client modèle est remplacé (par un client qui coûte)."""
    from app.agregation import calculer_agregats
    from app.etablissements import rafraichir_etablissements
    from app.offres import collecter_offres

    def espion(nom, fonction, avec_client=False):
        def op(eng, **kw):
            journal.ajouter(nom, **kw)
            if avec_client:
                kw["client"] = ClientQuiCoute(eng, plafond_client)
            return fonction(eng, **kw)
        return op
    return cyc.Operations(
        collecter_offres=espion("offres", collecter_offres), rafraichir_etablissements=espion("etablissements", rafraichir_etablissements),
        etiqueter_offres=espion("etiquetage", etiq.etiqueter_offres, True), calculer_agregats=espion("agregation", calculer_agregats),
        produire_fiches=espion("fiches", fch.produire_fiches, True),
    )


@pytest.fixture(autouse=True)
def sans_precriblage(monkeypatch):
    """Avec 70 offres synthétiques, le score maximal atteignable reste sous le seuil et le pré-criblage (gratuit, voir config/fiches.yaml)
    écarterait tous les couples : on l'éteint ICI pour exercer la chaîne complète jusqu'à la fiche. Le pré-criblage a ses propres tests."""
    reel = cfg.fiches()
    monkeypatch.setattr(cfg, "fiches", lambda: {**reel, "selection": {**reel["selection"], "precriblage_score_maximal": False}})


@pytest.fixture
def initiale(monkeypatch):
    monkeypatch.setenv("RADAR_CARTOGRAPHIE_INITIALE", "1")
    monkeypatch.setenv("RADAR_ENVELOPPE_INITIALE_EUR", "10")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "cle-de-test")
    cfg.get_settings.cache_clear()
    yield
    cfg.get_settings.cache_clear()


def _compte(engine, table, *cond):
    with engine.connect() as cx:
        return cx.execute(select(func.count()).select_from(table).where(*cond)).scalar_one()


def test_cartographie_initiale_ne_traite_que_la_priorite_1_dans_l_enveloppe(engine_test, reseau, initiale):
    j = Journal()
    assert cyc.cartographie_initiale(engine_test, operations_reelles(engine_test, j, plafond_client=10.0), dormir=lambda s: None) is True
    # Priorité 1 seulement : aucune offre, aucune mesure, aucune collecte pour les autres secteurs.
    assert set(reseau.codes_offres) == set(P1) and not set(reseau.codes_offres) & set(P2 + P3)
    assert {c for c, _d in reseau.paires} == set(P1)
    with engine_test.connect() as cx:
        assert {c for (c,) in cx.execute(select(collectes_offres.c.code_naf).distinct())} == set(P1)
        assert {c for (c,) in cx.execute(select(etablissements_secteur.c.code_naf).distinct())} == set(P1)
    # Les cinq étapes ont produit leur résultat réel.
    assert _compte(engine_test, offres_emploi) == 70
    assert _compte(engine_test, prospection) == 40
    assert _compte(engine_test, offres_etiquetage, offres_etiquetage.c.statut == "ok") == 60  # échantillon de 60 offres par secteur
    assert _compte(engine_test, fiches_secteur_tache) >= 1 and _compte(engine_test, fiches_secteur_tache) <= 2  # au plus 2 fiches par secteur
    # Enveloppe : jamais dépassée, étiquetage sous sa part (75 %), et le budget des fiches est resté disponible.
    depense = repo.cout_total_par_roles(engine_test, etiq.ROLES_ENVELOPPE)
    assert 0 < depense <= 10
    assert repo.cout_total_par_role(engine_test, etiq.ROLE_ETIQUETEUR) == pytest.approx(60 * COUT_ETIQUETAGE)
    # Résumé d'avancement en base
    run = next(r for r in _runs(engine_test) if r["mode"] == cyc.MODE_INITIALE)
    assert run["statut"] == "termine" and run["resume_json"]["terminee"] is True
    assert run["resume_json"]["avancement"]["depense_modele_cumulee_eur"] == pytest.approx(depense, abs=1e-3)
    assert {"etablissements", "offres", "etiquetage", "agregation", "fiches"} <= set(run["resume_json"]["etapes"])
    assert run["resume_json"]["reste_final"] == {"paires_etablissements": 0, "codes_sans_collecte": 0, "offres_a_etiqueter": 0, "fiches_a_produire": 0}


def test_cartographie_initiale_est_idempotente_et_ne_repaye_rien(engine_test, reseau, initiale):
    j = Journal()
    ops = operations_reelles(engine_test, j, plafond_client=10.0)
    cyc.cartographie_initiale(engine_test, ops, dormir=lambda s: None)
    depense, offres, fiches = repo.cout_total_par_roles(engine_test, etiq.ROLES_ENVELOPPE), _compte(engine_test, offres_emploi), _compte(engine_test, fiches_secteur_tache)
    n_appels = len(j.appels)
    # Redémarrage du worker avec la variable encore posée : rien n'est refait.
    assert cyc.cartographie_initiale(engine_test, ops, dormir=lambda s: None) is True
    assert len(j.appels) == n_appels, "aucune opération relancée : la cartographie initiale est terminée"
    assert [r["mode"] for r in _runs(engine_test)].count(cyc.MODE_INITIALE) == 1
    assert (repo.cout_total_par_roles(engine_test, etiq.ROLES_ENVELOPPE), _compte(engine_test, offres_emploi), _compte(engine_test, fiches_secteur_tache)) == (depense, offres, fiches)


def test_cartographie_initiale_reprend_apres_une_panne_sans_rien_perdre(engine_test, reseau, initiale):
    reseau.pannes_offres = 3  # les 3 premières collectes d'offres échouent (API France Travail indisponible)
    j = Journal()
    sommeils: list[float] = []
    assert cyc.cartographie_initiale(engine_test, operations_reelles(engine_test, j, plafond_client=10.0), dormir=sommeils.append) is True
    assert sommeils, "une passe incomplète attend avant de reprendre"
    assert _compte(engine_test, offres_emploi) == 70 and _compte(engine_test, offres_etiquetage, offres_etiquetage.c.statut == "ok") == 60
    # Chaque offre n'a été étiquetée (et payée) qu'une fois, malgré la reprise.
    assert repo.cout_total_par_role(engine_test, etiq.ROLE_ETIQUETEUR) == pytest.approx(60 * COUT_ETIQUETAGE)
    run = next(r for r in _runs(engine_test) if r["mode"] == cyc.MODE_INITIALE)
    assert run["resume_json"]["passes"] >= 2


def test_cartographie_initiale_reprend_un_run_interrompu(engine_test, reseau, initiale):
    """Le worker a été arrêté en pleine cartographie : le run « en cours » est repris (même identifiant), pas dupliqué."""
    run_id = repo.creer_run(engine_test, mode=cyc.MODE_INITIALE, version_code="x", version_config="initiale", quotas={})
    repo.mettre_a_jour_progression(engine_test, run_id, couts={}, resume={"phase": "initiale", "passes": 1, "journal": ["déjà fait"], "etapes": {}})
    assert cyc.cartographie_initiale(engine_test, operations_reelles(engine_test, Journal(), plafond_client=10.0), dormir=lambda s: None) is True
    lignes = [r for r in _runs(engine_test) if r["mode"] == cyc.MODE_INITIALE]
    assert len(lignes) == 1 and lignes[0]["id"] == run_id and lignes[0]["statut"] == "termine"
    assert "déjà fait" in lignes[0]["resume_json"]["journal"] and lignes[0]["resume_json"]["passes"] >= 2


def test_enveloppe_epuisee_termine_la_cartographie_initiale_sans_depasser(engine_test, reseau, initiale, monkeypatch):
    monkeypatch.setenv("RADAR_ENVELOPPE_INITIALE_EUR", "0.5")  # 0,375 € pour l'étiquetage (60 offres = 0,198 €), puis les fiches dans le reste
    j = Journal()
    # Le client réel est construit avec l'enveloppe comme plafond (BudgetTracker) : la doublure fait de même.
    assert cyc.cartographie_initiale(engine_test, operations_reelles(engine_test, j, plafond_client=0.5), dormir=lambda s: None) is True
    assert repo.cout_total_par_roles(engine_test, etiq.ROLES_ENVELOPPE) <= 0.5 + 1e-9


def test_enveloppe_trop_petite_arrete_net_et_le_regime_quotidien_prend_le_relais(engine_test, reseau, initiale, monkeypatch):
    monkeypatch.setenv("RADAR_ENVELOPPE_INITIALE_EUR", "0.1")
    j = Journal()
    assert cyc.cartographie_initiale(engine_test, operations_reelles(engine_test, j, plafond_client=0.1), dormir=lambda s: None) is True
    assert repo.cout_total_par_roles(engine_test, etiq.ROLES_ENVELOPPE) <= 0.1 + 1e-9
    run = next(r for r in _runs(engine_test) if r["mode"] == cyc.MODE_INITIALE)
    assert run["statut"] == "termine"  # terminée : l'enveloppe est épuisée, le reste sera couvert par le régime quotidien


def test_sans_enveloppe_la_cartographie_initiale_est_refusee_et_ne_depense_rien(engine_test, reseau, initiale, monkeypatch):
    monkeypatch.delenv("RADAR_ENVELOPPE_INITIALE_EUR")
    j = Journal()
    assert cyc.cartographie_initiale(engine_test, operations_reelles(engine_test, j), dormir=lambda s: None) is False
    assert j.appels == [] and reseau.codes_offres == []
    run = next(r for r in _runs(engine_test) if r["mode"] == cyc.MODE_INITIALE)
    assert run["statut"] == "echoue" and "RADAR_ENVELOPPE_INITIALE_EUR" in run["erreurs_json"][0]


def test_enveloppe_au_dela_du_maximum_est_refusee(engine_test, reseau, initiale, monkeypatch):
    monkeypatch.setenv("RADAR_ENVELOPPE_INITIALE_EUR", "500")
    assert cyc.cartographie_initiale(engine_test, operations_reelles(engine_test, Journal()), dormir=lambda s: None) is False
    assert reseau.codes_offres == []


# ------------------------------------------------------------------ régime quotidien après la cartographie initiale -----

def test_regime_quotidien_apres_l_initiale_couvre_les_autres_secteurs_a_2_euros_par_jour(engine_test, reseau, initiale, monkeypatch):
    j = Journal()
    ops = operations_reelles(engine_test, j, plafond_client=10.0)
    cyc.cartographie_initiale(engine_test, ops, dormir=lambda s: None)
    avant = repo.cout_total_par_roles(engine_test, etiq.ROLES_ENVELOPPE)
    j.appels.clear()
    reseau.codes_offres.clear()
    # Le même jour : les offres de la priorité 1 sont déjà étiquetées ; la collecte du jour couvre les priorités 1, 2 puis 3 dans l'ordre.
    monkeypatch.setenv("RADAR_ENVELOPPE_INITIALE_EUR", "10")  # encore posée : ne sert QUE les fiches de la priorité 1 (V2.8b)
    b = cyc.cycle_quotidien(engine_test, ops, horloge=lambda: MAINTENANT)
    assert reseau.codes_offres[:len(P1)] == [c for c in reseau.codes_offres[:len(P1)] if c in P1], "priorité 1 d'abord"
    assert set(reseau.codes_offres) == set(P1 + P2 + P3)
    assert all(kw["enveloppe"] is None for kw in j.de("etiquetage"))
    # V2.8b (décision du 2026-10-02) : l'étiquetage et les fiches de toutes priorités restent sous le plafond du jour ; seules les fiches de
    # la priorité 1 peuvent en plus puiser dans le reste de l'enveloppe initiale.
    sur_enveloppe = [kw for kw in j.de("fiches") if kw["enveloppe"] is not None]
    assert all(kw["enveloppe"] == 10.0 and tuple(kw["seulement_codes"]) == P1 for kw in sur_enveloppe)
    assert repo.cout_total_par_roles(engine_test, etiq.ROLES_ENVELOPPE) <= 10.0 + COUT_ANALYSTE + COUT_CRITIC
    assert b.erreur_systemique is False
    assert avant <= 10.0


def test_le_plafond_de_2_euros_par_jour_est_applique_au_cout_reel_en_regime_quotidien(engine_test, reseau, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "cle-de-test")
    monkeypatch.delenv("RADAR_ENVELOPPE_INITIALE_EUR", raising=False)
    cfg.get_settings.cache_clear()
    j = Journal()
    ops = operations_reelles(engine_test, j, plafond_client=2.0)
    # 5 jours de suite : chaque jour dépense au plus 2 €, jamais davantage (même si le travail à faire est bien supérieur).
    for jour in range(5):
        horloge = MAINTENANT + timedelta(days=jour)
        cyc.cycle_quotidien(engine_test, ops, horloge=lambda h=horloge: h)
        assert repo.cout_total_jour_utc(engine_test, horloge.date()) <= 2.0 + 1e-9
    cfg.get_settings.cache_clear()


def test_aucune_cle_modele_pas_de_fiche_par_repli_et_le_cycle_continue(engine_test, reseau, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg.get_settings.cache_clear()
    from app.agregation import calculer_agregats
    from app.etablissements import rafraichir_etablissements
    from app.offres import collecter_offres
    ops = cyc.Operations(collecter_offres=lambda e, **kw: collecter_offres(e, **kw), rafraichir_etablissements=lambda e, **kw: rafraichir_etablissements(e, **kw),
                         etiqueter_offres=lambda e, **kw: etiq.etiqueter_offres(e, **kw), calculer_agregats=lambda e, **kw: calculer_agregats(e, **kw),
                         produire_fiches=lambda e, **kw: fch.produire_fiches(e, **kw))
    b = cyc.cycle_quotidien(engine_test, ops, horloge=lambda: MAINTENANT)
    assert _compte(engine_test, fiches_secteur_tache) == 0
    assert b.erreur_systemique  # « ANTHROPIC_API_KEY absente » est un arrêt systémique : attente d'une heure, pas toutes les 30 minutes


# ------------------------------------------------------------------ V2.8b : fiches de la priorité 1 sur le reste de l'enveloppe initiale -----

def _depenser(engine, montant, role=etiq.ROLE_ETIQUETEUR):
    run = repo.creer_run(engine, mode="test", version_code="x", version_config="x", quotas={})
    budget = BudgetTracker(engine, run, 1000.0, plafond_appels_approfondis=0)
    budget.verifier_et_engager(montant, role=role)
    budget.enregistrer_reel(fournisseur="anthropic", modele_ou_actor="faux", appels=1, tokens_in=1, tokens_out=1, cout_reel=montant,
                            cout_estime_engage=montant, role=role)


def test_fiches_p1_puisent_dans_l_enveloppe_en_plus_du_plafond_du_jour(engine_test, monkeypatch):
    monkeypatch.setenv("RADAR_ENVELOPPE_INITIALE_EUR", "10")
    j = Journal()
    cyc.cycle_quotidien(engine_test, operations_vides(j), horloge=lambda: MAINTENANT)
    fiches = j.de("fiches")
    assert len(fiches) == 2
    assert fiches[0]["enveloppe"] is None  # d'abord le plafond du jour, toutes priorités
    assert fiches[1]["enveloppe"] == 10.0 and tuple(fiches[1]["seulement_codes"]) == P1 and tuple(fiches[1]["ordre_codes"]) == P1


def test_sans_enveloppe_posee_pas_de_fiches_sur_enveloppe(engine_test, monkeypatch):
    monkeypatch.delenv("RADAR_ENVELOPPE_INITIALE_EUR", raising=False)
    j = Journal()
    cyc.cycle_quotidien(engine_test, operations_vides(j), horloge=lambda: MAINTENANT)
    assert [kw["enveloppe"] for kw in j.de("fiches")] == [None]


def test_enveloppe_epuisee_pas_de_fiches_sur_enveloppe(engine_test, monkeypatch):
    monkeypatch.setenv("RADAR_ENVELOPPE_INITIALE_EUR", "10")
    _depenser(engine_test, 9.99)
    j = Journal()
    cyc.cycle_quotidien(engine_test, operations_vides(j), horloge=lambda: MAINTENANT)
    assert [kw["enveloppe"] for kw in j.de("fiches")] == [None]


def test_enveloppe_illisible_ignoree_sans_arreter_le_cycle(engine_test, monkeypatch):
    monkeypatch.setenv("RADAR_ENVELOPPE_INITIALE_EUR", "beaucoup")
    j = Journal()
    b = cyc.cycle_quotidien(engine_test, operations_vides(j), horloge=lambda: MAINTENANT)
    assert [kw["enveloppe"] for kw in j.de("fiches")] == [None] and b.erreur_systemique is False


def test_interrupteur_de_config_eteint_revient_au_comportement_v2_8(engine_test, monkeypatch):
    monkeypatch.setenv("RADAR_ENVELOPPE_INITIALE_EUR", "10")
    reel = cfg.cycle_v2()
    monkeypatch.setattr(cfg, "cycle_v2", lambda: {**reel, "fiches_priorite_initiale_sur_enveloppe": False})
    j = Journal()
    cyc.cycle_quotidien(engine_test, operations_vides(j), horloge=lambda: MAINTENANT)
    assert [kw["enveloppe"] for kw in j.de("fiches")] == [None]


def test_plafond_du_jour_epuise_les_fiches_p1_sortent_sur_l_enveloppe_sans_la_depasser(engine_test, reseau, monkeypatch):
    """Le cas du 2026-10-02 : plafond du jour déjà dépensé en étiquetage ; les fiches de la priorité 1 sortent quand même, sur l'enveloppe."""
    maintenant = datetime.now(timezone.utc)  # les dépenses sont horodatées à l'heure réelle : le cycle doit regarder le même jour
    monkeypatch.setenv("ANTHROPIC_API_KEY", "cle-de-test")
    monkeypatch.setenv("RADAR_ENVELOPPE_INITIALE_EUR", "10")
    cfg.get_settings.cache_clear()
    j = Journal()
    ops = operations_reelles(engine_test, j, plafond_client=2.0)
    # Préparation sans modèle payant : collecte, établissements, étiquetage (coût simulé), agrégation.
    from app.agregation import calculer_agregats
    from app.etablissements import rafraichir_etablissements
    from app.offres import collecter_offres
    collecter_offres(engine_test, codes=(CODE_P1,))
    rafraichir_etablissements(engine_test, codes=(CODE_P1,))
    etiq.etiqueter_offres(engine_test, codes=(CODE_P1,), enveloppe=None, plafond_jour_eur=2.0, client=ClientQuiCoute(engine_test, 2.0))
    calculer_agregats(engine_test)
    _depenser(engine_test, 2.0 - repo.cout_total_jour_utc(engine_test, maintenant.date()))  # plafond du jour atteint
    avant = repo.cout_total_jour_utc(engine_test, maintenant.date())
    assert avant >= 2.0 - 1e-9 and _compte(engine_test, fiches_secteur_tache) == 0

    # Le client « qui coûte » applique SON plafond : on lui donne celui que produire_fiches lui passerait (l'enveloppe).
    ops.produire_fiches = (lambda eng, **kw: (j.ajouter("fiches", **kw),
                                              fch.produire_fiches(eng, client=ClientQuiCoute(eng, kw["enveloppe"] or 2.0), **kw))[1])
    cyc.cycle_quotidien(engine_test, ops, horloge=lambda: maintenant)
    assert 1 <= _compte(engine_test, fiches_secteur_tache) <= 2
    with engine_test.connect() as cx:
        assert {c for (c,) in cx.execute(select(fiches_secteur_tache.c.code_naf))} <= set(P1)
    assert repo.cout_total_jour_utc(engine_test, maintenant.date()) > avant  # dépense au-delà du plafond du jour...
    assert repo.cout_total_par_roles(engine_test, etiq.ROLES_ENVELOPPE) <= 10.0  # ...jamais au-delà de l'enveloppe cumulée
    cfg.get_settings.cache_clear()
