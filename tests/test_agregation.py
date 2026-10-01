"""Sous-étape V2.4 (RADAR-V2.md) : agrégation de la demande par secteur x tâche. Sans réseau, 0 € : offres et étiquettes
SYNTHÉTIQUES posées directement en base."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app import agregation as agg
from app.adapters import france_travail as ft
from app.storage import repo
from app.storage.schema import demande_secteur_tache

MAINTENANT = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
NAF = "69.20Z"


def _poser(engine, n, *, naf=NAF, jours=5, commune="33063", salaire="Annuel de 30000.0 Euros", taches=(), statut="ok",
           provenance="citation_verifiee"):
    """Pose une offre, son état d'étiquetage et ses tâches (liste de ids ; provenance(s) au choix)."""
    offre = ft.normaliser_offre({
        "id": f"AG{n:05d}", "intitule": "Poste", "description": "texte",
        "dateCreation": (MAINTENANT - timedelta(days=jours)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "lieuTravail": {"commune": commune}, "codeNAF": naf, "salaire": {"libelle": salaire} if salaire else {},
    })
    repo.enregistrer_offres(engine, [offre], naf_version="2", maintenant=MAINTENANT)
    provs = (provenance,) if isinstance(provenance, str) else provenance
    lignes = [{"tache_id": t, "provenance": p, "citation": "x"} for t in taches for p in provs]
    if statut is not None:
        repo.enregistrer_etiquetage(engine, id_offre=f"AG{n:05d}", statut=statut, version="v", modele="m", taches=lignes,
                                    nb_citations_proposees=len(taches), nb_citations_verifiees=len(taches), maintenant=MAINTENANT)


def _collecte(engine, code=NAF, *, il_y_a_jours=10):
    repo.enregistrer_collecte_offres(engine, code_naf=code, naf_version="2", debut=MAINTENANT - timedelta(days=90),
                                     fin=MAINTENANT - timedelta(days=il_y_a_jours), nb_offres=1, nb_nouvelles=1, requetes=1,
                                     fenetres_tronquees=0, horodatage=MAINTENANT - timedelta(days=il_y_a_jours))


def _lignes(engine):
    with engine.connect() as cx:
        return {(l["code_naf"], l["tache_id"]): l for l in cx.execute(select(demande_secteur_tache)).mappings()}


def test_comptes_part_et_salaire_median(engine_test):
    _collecte(engine_test)
    _poser(engine_test, 1, taches=["saisie_factures"], salaire="Annuel de 24000.0 Euros")
    _poser(engine_test, 2, taches=["saisie_factures", "reporting"], salaire="Annuel de 30000.0 Euros")
    _poser(engine_test, 3, taches=["saisie_factures"], salaire="Annuel de 36000.0 Euros")
    _poser(engine_test, 4, taches=[], salaire=None)
    r = agg.calculer_agregats(engine_test, maintenant=MAINTENANT)
    lignes = _lignes(engine_test)
    saisie = lignes[(NAF, "saisie_factures")]
    assert (saisie["nb_offres_tache"], saisie["nb_offres_secteur"], saisie["part_offres_tache"]) == (3, 4, 0.75)
    assert (saisie["salaire_median_annuel_eur"], saisie["nb_salaires"]) == (30000.0, 3)
    assert lignes[(NAF, "reporting")]["nb_offres_tache"] == 1
    assert (r.secteurs_avec_offres, r.couples_calcules, r.lignes_ajoutees) == (1, 2, 2)


def test_fourchette_de_salaire_utilise_le_milieu(engine_test):
    _collecte(engine_test)
    _poser(engine_test, 1, taches=["reporting"], salaire="Annuel de 30000.0 Euros à 40000.0 Euros")
    agg.calculer_agregats(engine_test, maintenant=MAINTENANT)
    assert _lignes(engine_test)[(NAF, "reporting")]["salaire_median_annuel_eur"] == 35000.0


def test_aucun_salaire_lisible_donne_null(engine_test):
    _collecte(engine_test)
    _poser(engine_test, 1, taches=["reporting"], salaire=None)
    agg.calculer_agregats(engine_test, maintenant=MAINTENANT)
    ligne = _lignes(engine_test)[(NAF, "reporting")]
    assert ligne["salaire_median_annuel_eur"] is None and ligne["nb_salaires"] == 0


def test_comptage_zone_et_france(engine_test):
    _collecte(engine_test)
    _poser(engine_test, 1, taches=["reporting"], commune="33063")   # Gironde : zone
    _poser(engine_test, 2, taches=["reporting"], commune="24037")   # Dordogne : zone (proximité)
    _poser(engine_test, 3, taches=["reporting"], commune="75056")   # Paris : hors zone
    agg.calculer_agregats(engine_test, maintenant=MAINTENANT)
    ligne = _lignes(engine_test)[(NAF, "reporting")]
    assert (ligne["nb_offres_tache"], ligne["nb_offres_tache_zone"]) == (3, 2)


def test_denominateur_ne_compte_que_les_offres_completement_etiquetees(engine_test):
    _collecte(engine_test)
    _poser(engine_test, 1, taches=["reporting"])
    _poser(engine_test, 2, taches=[])
    _poser(engine_test, 3, taches=["reporting"], statut="lexique_seul", provenance="lexique")   # modèle pas passé
    _poser(engine_test, 4, taches=[], statut=None)                                              # jamais étiquetée
    agg.calculer_agregats(engine_test, maintenant=MAINTENANT)
    ligne = _lignes(engine_test)[(NAF, "reporting")]
    assert (ligne["nb_offres_secteur"], ligne["nb_offres_secteur_total"], ligne["couverture_etiquetage"]) == (2, 4, 0.5)
    assert (ligne["nb_offres_tache"], ligne["part_offres_tache"]) == (1, 0.5)


def test_provenance_lexique_seul_contre_citation_verifiee(engine_test):
    _collecte(engine_test)
    _poser(engine_test, 1, taches=["reporting"], provenance="citation_verifiee")
    _poser(engine_test, 2, taches=["reporting"], provenance="lexique")
    _poser(engine_test, 3, taches=["reporting"], provenance=("lexique", "citation_verifiee"))   # les deux : compte une seule fois
    agg.calculer_agregats(engine_test, maintenant=MAINTENANT)
    ligne = _lignes(engine_test)[(NAF, "reporting")]
    assert (ligne["nb_offres_tache"], ligne["nb_citation_verifiee"], ligne["nb_lexique_seul"]) == (3, 2, 1)


def test_stock_limite_a_90_jours(engine_test):
    _collecte(engine_test)
    _poser(engine_test, 1, jours=10, taches=["reporting"])
    _poser(engine_test, 2, jours=89, taches=["reporting"])
    _poser(engine_test, 3, jours=95, taches=["reporting"])     # hors stock
    agg.calculer_agregats(engine_test, maintenant=MAINTENANT)
    ligne = _lignes(engine_test)[(NAF, "reporting")]
    assert (ligne["nb_offres_tache"], ligne["fenetre_jours"]) == (2, 90)


def test_secteur_exclu_ignore(engine_test):
    _poser(engine_test, 1, naf="47.73Z", taches=["reporting"])   # pharmacie : exclue
    r = agg.calculer_agregats(engine_test, maintenant=MAINTENANT)
    assert r.secteurs_exclus_ignores == 1 and r.couples_calcules == 0 and _lignes(engine_test) == {}


def test_regle_hds_ecrite_en_motif_d_exclusion_pour_la_sante(engine_test):
    _collecte(engine_test, "86.23Z")
    _poser(engine_test, 1, naf="86.23Z", taches=["prise_rendez_vous", "reporting"])
    agg.calculer_agregats(engine_test, maintenant=MAINTENANT)
    lignes = _lignes(engine_test)
    assert lignes[("86.23Z", "prise_rendez_vous")]["motif_exclusion"] == "HDS"
    assert lignes[("86.23Z", "reporting")]["motif_exclusion"] is None


def test_offre_sans_code_naf_ignoree(engine_test):
    _poser(engine_test, 1, naf=None, taches=["reporting"])
    assert agg.calculer_agregats(engine_test, maintenant=MAINTENANT).secteurs_avec_offres == 0


def test_base_vide(engine_test):
    r = agg.calculer_agregats(engine_test, maintenant=MAINTENANT)
    assert (r.couples_calcules, r.lignes_ajoutees) == (0, 0)


# ------------------------------------------------------------------ accumulation et tendance -----

def test_accumulation_courte_pas_de_tendance(engine_test):
    _collecte(engine_test, il_y_a_jours=30)
    _poser(engine_test, 1, jours=5, taches=["reporting"])
    agg.calculer_agregats(engine_test, maintenant=MAINTENANT)
    ligne = _lignes(engine_test)[(NAF, "reporting")]
    assert (ligne["accumulation_jours"], ligne["tendance_3_mois_pct"], ligne["tendance_statut"]) == (30, None, "accumulation_insuffisante")


def test_accumulation_suffisante_tendance_calculee(engine_test):
    _collecte(engine_test, il_y_a_jours=200)
    for i in range(1, 7):
        _poser(engine_test, i, jours=10 + i, taches=["reporting"])        # 6 offres sur les 90 derniers jours
    for i in range(7, 11):
        _poser(engine_test, i, jours=100 + i, taches=["reporting"])       # 4 offres sur les 90 jours d'avant
    agg.calculer_agregats(engine_test, maintenant=MAINTENANT)
    ligne = _lignes(engine_test)[(NAF, "reporting")]
    assert (ligne["accumulation_jours"], ligne["tendance_statut"], ligne["tendance_3_mois_pct"]) == (200, "calculee", 50.0)


def test_tendance_sans_reference_si_aucune_offre_avant(engine_test):
    _collecte(engine_test, il_y_a_jours=200)
    _poser(engine_test, 1, jours=10, taches=["reporting"])
    agg.calculer_agregats(engine_test, maintenant=MAINTENANT)
    ligne = _lignes(engine_test)[(NAF, "reporting")]
    assert (ligne["tendance_3_mois_pct"], ligne["tendance_statut"]) == (None, "sans_reference")


def test_sans_collecte_enregistree_accumulation_nulle(engine_test):
    _poser(engine_test, 1, taches=["reporting"])
    agg.calculer_agregats(engine_test, maintenant=MAINTENANT)
    assert _lignes(engine_test)[(NAF, "reporting")]["accumulation_jours"] == 0


# ------------------------------------------------------------------ instantanés append-only -----

def test_recalcul_sans_changement_n_ajoute_rien(engine_test):
    _collecte(engine_test)
    _poser(engine_test, 1, taches=["reporting"])
    agg.calculer_agregats(engine_test, maintenant=MAINTENANT)
    r = agg.calculer_agregats(engine_test, maintenant=MAINTENANT + timedelta(hours=3))
    assert (r.lignes_ajoutees, r.lignes_inchangees) == (0, 1)
    with engine_test.connect() as cx:
        assert cx.execute(select(func.count()).select_from(demande_secteur_tache)).scalar_one() == 1


def test_un_chiffre_qui_change_ajoute_un_instantane_sans_ecraser_le_precedent(engine_test):
    _collecte(engine_test)
    _poser(engine_test, 1, taches=["reporting"])
    agg.calculer_agregats(engine_test, maintenant=MAINTENANT)
    _poser(engine_test, 2, taches=["reporting"])
    r = agg.calculer_agregats(engine_test, maintenant=MAINTENANT + timedelta(days=1))
    assert r.lignes_ajoutees == 1
    with engine_test.connect() as cx:
        comptes = [l[0] for l in cx.execute(select(demande_secteur_tache.c.nb_offres_tache).order_by(demande_secteur_tache.c.calcule_le))]
    assert comptes == [1, 2]
    assert repo.dernier_agregat_par_couple(engine_test, "2")[(NAF, "reporting")]["nb_offres_tache"] == 2


def test_la_duree_d_accumulation_seule_ne_declenche_pas_un_nouvel_instantane(engine_test):
    _collecte(engine_test, il_y_a_jours=10)
    _poser(engine_test, 1, taches=["reporting"])
    agg.calculer_agregats(engine_test, maintenant=MAINTENANT)
    r = agg.calculer_agregats(engine_test, maintenant=MAINTENANT + timedelta(days=2))
    assert r.lignes_ajoutees == 0


# ------------------------------------------------------------------ extrapolation depuis un échantillon -----

def test_wilson_valeurs_connues():
    bas, haut = agg.intervalle_wilson(10, 100)
    assert (round(bas, 3), round(haut, 3)) == (0.055, 0.174)
    assert agg.intervalle_wilson(0, 50)[0] == 0.0 and agg.intervalle_wilson(50, 50)[1] == 1.0
    assert agg.intervalle_wilson(0, 0) == (0.0, 0.0)


def test_wilson_se_resserre_quand_l_echantillon_grossit():
    petit = agg.intervalle_wilson(6, 30)
    grand = agg.intervalle_wilson(60, 300)
    assert (grand[1] - grand[0]) < (petit[1] - petit[0])


def test_recensement_estime_egal_observe_et_intervalle_reduit_a_la_part(engine_test):
    _collecte(engine_test)
    for i in range(1, 5):
        _poser(engine_test, i, taches=["reporting"] if i <= 2 else [])
    agg.calculer_agregats(engine_test, maintenant=MAINTENANT)
    ligne = _lignes(engine_test)[(NAF, "reporting")]
    assert ligne["couverture_etiquetage"] == 1.0 and ligne["nb_offres_tache_estime"] == ligne["nb_offres_tache"] == 2
    assert ligne["part_ic95_bas"] == ligne["part_ic95_haut"] == ligne["part_offres_tache"] == 0.5


def test_echantillon_extrapole_la_part_a_toutes_les_offres_du_secteur(engine_test):
    _collecte(engine_test)
    # 20 offres collectées dans le secteur (dont 4 en Gironde), 10 étiquetées, 2 de celles-ci mentionnent « reporting »
    for i in range(1, 11):
        _poser(engine_test, i, commune="33063" if i <= 2 else "75056", taches=["reporting"] if i <= 2 else [])
    for i in range(11, 21):
        _poser(engine_test, i, commune="33063" if i <= 12 else "75056", taches=[], statut=None)
    agg.calculer_agregats(engine_test, maintenant=MAINTENANT)
    ligne = _lignes(engine_test)[(NAF, "reporting")]
    assert (ligne["nb_offres_secteur"], ligne["nb_offres_secteur_total"], ligne["couverture_etiquetage"]) == (10, 20, 0.5)
    assert (ligne["nb_offres_tache"], ligne["part_offres_tache"]) == (2, 0.2)
    assert ligne["nb_offres_tache_estime"] == 4                      # 0,2 x 20
    assert ligne["nb_offres_tache_zone_estime"] == 1                 # 0,2 x 4 offres en zone
    assert ligne["part_ic95_bas"] < 0.2 < ligne["part_ic95_haut"]


# ------------------------------------------------------------------ échantillon par code NAF (sélection) -----

def _offre_brute(n, naf, jours=5):
    return ft.normaliser_offre({
        "id": f"EC{n:05d}", "intitule": "Poste", "description": "texte", "codeNAF": naf, "lieuTravail": {"commune": "33063"},
        "dateCreation": (MAINTENANT - timedelta(days=jours)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })


def _choisies(engine, **kw):
    base = dict(version="v", limite=1000, departements_zone=("33",), avec_modele=True, depuis=MAINTENANT - timedelta(days=90))
    return [l["id_offre"] for l in repo.offres_a_etiqueter(engine, "2", **{**base, **kw})]


def test_plafond_par_code_limite_chaque_secteur(engine_test):
    repo.enregistrer_offres(engine_test, [_offre_brute(i, "69.20Z") for i in range(30)] + [_offre_brute(100 + i, "47.11B") for i in range(5)],
                            naf_version="2", maintenant=MAINTENANT)
    ids = _choisies(engine_test, max_par_code=10)
    assert len(ids) == 15                                   # 10 pour 69.20Z, les 5 de 47.11B
    assert sum(1 for i in ids if int(i[2:]) < 100) == 10 and sum(1 for i in ids if int(i[2:]) >= 100) == 5


def test_tirage_stable_d_un_appel_a_l_autre_et_independant_de_la_date(engine_test):
    repo.enregistrer_offres(engine_test, [_offre_brute(i, "69.20Z", jours=1 + i) for i in range(40)], naf_version="2", maintenant=MAINTENANT)
    premier = _choisies(engine_test, max_par_code=10)
    assert premier == _choisies(engine_test, max_par_code=10)
    recentes = {f"EC{i:05d}" for i in range(10)}              # les 10 plus récentes
    assert set(premier) != recentes                            # le tirage n'est pas « les plus récentes »


def test_deja_etiquetees_comptent_dans_le_plafond_du_code(engine_test):
    repo.enregistrer_offres(engine_test, [_offre_brute(i, "69.20Z") for i in range(20)], naf_version="2", maintenant=MAINTENANT)
    for i in _choisies(engine_test, max_par_code=8):
        repo.enregistrer_etiquetage(engine_test, id_offre=i, statut="ok", version="v", modele="m", taches=[], nb_citations_proposees=0,
                                    nb_citations_verifiees=0, maintenant=MAINTENANT)
    assert _choisies(engine_test, max_par_code=8) == []         # le code a déjà ses 8 offres
    assert len(_choisies(engine_test, max_par_code=12)) == 4    # relever le plafond ajoute la différence


def test_reprises_toujours_admises_meme_au_dela_du_plafond(engine_test):
    repo.enregistrer_offres(engine_test, [_offre_brute(i, "69.20Z") for i in range(5)], naf_version="2", maintenant=MAINTENANT)
    repo.enregistrer_etiquetage(engine_test, id_offre="EC00000", statut="echec_modele", version="v", modele=None, taches=[],
                                nb_citations_proposees=0, nb_citations_verifiees=0, maintenant=MAINTENANT)
    assert "EC00000" in _choisies(engine_test, max_par_code=1)


def test_passe_courte_se_repartit_entre_les_codes(engine_test):
    repo.enregistrer_offres(engine_test, [_offre_brute(i, "69.20Z") for i in range(50)] + [_offre_brute(100 + i, "47.11B") for i in range(50)],
                            naf_version="2", maintenant=MAINTENANT)
    ids = _choisies(engine_test, max_par_code=40, limite=6)
    codes = {"69.20Z" if int(i[2:]) < 100 else "47.11B" for i in ids}
    assert len(ids) == 6 and codes == {"69.20Z", "47.11B"}


def test_offres_hors_fenetre_du_stock_jamais_selectionnees(engine_test):
    repo.enregistrer_offres(engine_test, [_offre_brute(1, "69.20Z", jours=10), _offre_brute(2, "69.20Z", jours=120)], naf_version="2", maintenant=MAINTENANT)
    assert _choisies(engine_test, max_par_code=10) == ["EC00001"]


def test_sans_plafond_comportement_historique_zone_d_abord(engine_test):
    repo.enregistrer_offres(engine_test, [_offre_brute(i, "69.20Z") for i in range(5)], naf_version="2", maintenant=MAINTENANT)
    assert len(_choisies(engine_test, max_par_code=None)) == 5


def test_etiquetage_respecte_le_plafond_de_la_config(engine_test, monkeypatch):
    from app import config as cfg
    from app import etiquetage as etiq

    reel = cfg.etiquetage()
    monkeypatch.setattr(cfg, "etiquetage", lambda: {**reel, "echantillon_max_par_code": 3})
    repo.enregistrer_offres(engine_test, [_offre_brute(i, "69.20Z") for i in range(10)], naf_version="2", maintenant=MAINTENANT)
    r = etiq.etiqueter_offres(engine_test, avec_modele=False, maintenant=MAINTENANT)
    assert r.offres_traitees == 3
    assert etiq.etiqueter_offres(engine_test, avec_modele=False, maintenant=MAINTENANT).offres_prevues == 0


def test_offres_d_agence_d_interim_ignorees_par_l_agregation(engine_test):
    _poser(engine_test, 1, naf="78.20Z", taches=["reporting"])
    _poser(engine_test, 2, naf="78.10Z", taches=["reporting"])
    r = agg.calculer_agregats(engine_test, maintenant=MAINTENANT)
    assert r.secteurs_exclus_ignores == 2 and r.couples_calcules == 0 and _lignes(engine_test) == {}


def test_une_offre_d_agence_deja_en_base_n_est_jamais_etiquetee_ni_chiffree(engine_test):
    from app import etiquetage as etiq

    repo.enregistrer_offres(engine_test, [_offre_brute(1, "69.20Z"), _offre_brute(2, "78.20Z"), _offre_brute(3, "78.10Z")],
                            naf_version="2", maintenant=MAINTENANT)
    assert etiq.estimer_pour_base(engine_test).nb_offres == 1
    r = etiq.etiqueter_offres(engine_test, avec_modele=False, maintenant=MAINTENANT)
    assert r.offres_traitees == 1
    with engine_test.connect() as cx:
        from app.storage.schema import offres_etiquetage
        etiquetees = {l[0] for l in cx.execute(select(offres_etiquetage.c.id_offre))}
    assert etiquetees == {"EC00001"}  # les deux offres d'agence n'ont reçu aucune ligne d'étiquetage


def test_selection_sans_plafond_exclut_aussi_les_codes_demandes(engine_test):
    repo.enregistrer_offres(engine_test, [_offre_brute(1, "69.20Z"), _offre_brute(2, "78.20Z")], naf_version="2", maintenant=MAINTENANT)
    kw = dict(version="v", limite=10, departements_zone=("33",), avec_modele=True)
    assert [l["id_offre"] for l in repo.offres_a_etiqueter(engine_test, "2", exclure_codes=("78.20Z",), **kw)] == ["EC00001"]
    assert [l["id_offre"] for l in repo.offres_a_etiqueter(engine_test, "2", exclure_codes=("78.20Z",), max_par_code=5,
                                                          depuis=MAINTENANT - timedelta(days=90), **kw)] == ["EC00001"]
