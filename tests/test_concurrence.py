"""V2.6 : concurrence (recherche web bornée, désactivée ; liste et import pour la procédure V2.6b). Aucun réseau, aucun modèle, 0 €."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

import test_fiches as tf
from app import cli
from app import concurrence as conc
from app import config as cfg
from app import fiches as fch
from app import scoring_v2 as sc
from app import selection_couples as sel
from app.enqueteur import fournisseur_payant as fp
from app.enqueteur.fetch import PageCollectee
from app.enqueteur.fournisseurs import ResultatRecherche
from app.faisabilite import ResultatAccessibilite
from app.metriques import calculer_metriques
from app.models_schemas import DelaiPremierRevenu
from app.storage import repo
from app.storage.schema import concurrence_secteur_tache, recherches_web

MAINT = tf.MAINTENANT
NAF, TACHE = tf.NAF, tf.TACHE


# ------------------------------------------------------------------ outils de test -----

def _r(url, titre, extrait="", fournisseur="faux"):
    return ResultatRecherche(url=url, titre=titre, extrait=extrait or titre, horodatage_source=None, fournisseur=fournisseur)


class FournisseurFaux:
    nom = "faux"

    def __init__(self, outils=None, prestataires=None, erreur=None):
        self.outils, self.prestataires, self.erreur, self.requetes = outils, prestataires, erreur, []

    def rechercher(self, requete, limite):
        self.requetes.append((requete, limite))
        if self.erreur:
            raise self.erreur
        return list(self.outils if "logiciel" in requete else self.prestataires)


OUTILS = [_r("https://relancepro.example/accueil", "RelancePro : logiciel de relance des clients"),
          _r("https://impayes.example/", "Impayés Facile - SaaS de recouvrement"),
          _r("https://fr.wikipedia.org/wiki/Recouvrement", "Recouvrement : logiciel et méthodes")]  # domaine exclu (plateforme générique)
PRESTATAIRES_SANS_LOCAL = [_r("https://cabinet-lyon.example/", "Cabinet de recouvrement à Lyon", "Prestataire lyonnais"),
                           _r("http://compta-bordeaux.example/", "Compta Bordeaux", "Prestataire local")]  # http : jamais retenu comme preuve
PRESTATAIRES_AVEC_LOCAL = PRESTATAIRES_SANS_LOCAL + [_r("https://relance-gironde.example/", "Relance Gironde - recouvrement pour TPE", "Basés à Mérignac")]


def _page(url, texte):
    return PageCollectee(url=url, titre="t", texte=texte, date_collecte=MAINT, horodatage_source=None, fournisseur="faux", requete_origine=None)


def _actif(monkeypatch, plafond=None):
    monkeypatch.setenv("RADAR_CONCURRENCE_WEB", "1")
    monkeypatch.setenv(fp.VARIABLE_CLE, "cle-factice-de-test")
    if plafond is not None:
        monkeypatch.setenv("RADAR_RECHERCHE_WEB_MAX_MOIS", str(plafond))


def _nb(engine, table):
    with engine.connect() as cx:
        return cx.execute(select(func.count()).select_from(table)).scalar_one()


def _poser_fiche(engine, *, code=NAF, tache=TACHE, score_prudent=62.0, score_brut=63.0, decision="a_verifier", calcule_le=MAINT):
    repo.enregistrer_fiche(engine, dict(
        code_naf=code, tache_id=tache, naf_version="2", version_score=str(cfg.fiches()["version"]), version_prompt=fch.VERSION_PROMPT, decision=decision,
        motifs_json=[], score_brut=score_brut, score_prudent=score_prudent, score_json={}, agregats_json={}, fiche_json=None, critique_json=None,
        modele_analyste="m", modele_critic=None, calcule_le=calcule_le))


# ------------------------------------------------------------------ drapeau, plafond, requêtes -----

def test_desactive_par_defaut_et_la_cle_seule_ne_suffit_jamais(monkeypatch):
    assert conc.etat_fournisseur().actif is False
    monkeypatch.setenv(fp.VARIABLE_CLE, "x")
    assert conc.etat_fournisseur().actif is False and "n'est pas à 1" in conc.etat_fournisseur().raison
    monkeypatch.delenv(fp.VARIABLE_CLE)
    monkeypatch.setenv("RADAR_CONCURRENCE_WEB", "1")
    assert conc.etat_fournisseur().actif is False and "aucune clé" in conc.etat_fournisseur().raison
    monkeypatch.setenv(fp.VARIABLE_CLE, "x")
    assert conc.etat_fournisseur().actif is True


@pytest.mark.parametrize("valeur", ["0", "non", "", "2"])
def test_drapeau_autre_que_un_true_yes_reste_desactive(monkeypatch, valeur):
    monkeypatch.setenv("RADAR_CONCURRENCE_WEB", valeur)
    monkeypatch.setenv(fp.VARIABLE_CLE, "x")
    assert conc.etat_fournisseur().actif is False


def test_la_raison_n_expose_jamais_la_cle(monkeypatch):
    monkeypatch.setenv("RADAR_CONCURRENCE_WEB", "1")
    monkeypatch.setenv(fp.VARIABLE_CLE, "SECRET-123")
    assert "SECRET-123" not in conc.etat_fournisseur().raison


@pytest.mark.parametrize("env,attendu", [(None, 2000), ("", 2000), ("150", 150), (" 7 ", 7), ("0", 0), ("-5", 0), ("beaucoup", 0)])
def test_plafond_mensuel_defaut_env_et_fermeture_en_cas_de_doute(monkeypatch, env, attendu):
    if env is not None:
        monkeypatch.setenv("RADAR_RECHERCHE_WEB_MAX_MOIS", env)
    assert conc.plafond_mensuel() == attendu


def test_requetes_composees_par_le_code_en_francais():
    q = conc.composer_requetes("Activités comptables", "Relance des clients")
    assert q == {"outils": "relance des clients logiciel activités comptables", "prestataires": "relance des clients activités comptables prestataire"}


def test_mentionne_zone_mot_entier_sans_accent_ni_casse():
    termes = cfg.concurrence()["termes_zone"]
    assert conc.mentionne_zone("Cabinet MERIGNAC conseil", termes) == "Mérignac"
    assert conc.mentionne_zone("Basés en gironde", termes) == "Gironde"
    assert conc.mentionne_zone("compta-bordeaux.example", termes) == "Bordeaux"
    assert conc.mentionne_zone("Bordeauxlogiciel", termes) is None     # pas un mot entier
    assert conc.mentionne_zone("Cabinet à Lyon", termes) is None


@pytest.mark.parametrize("url,attendu", [("https://a.example/x", True), ("http://a.example", False), ("ftp://a.example", False), ("a.example", False),
                                         (None, False), ("https://", False), ("https://a b.example", False)])
def test_url_https(url, attendu):
    assert conc.url_https(url) is attendu


def test_extraire_prix_cite_la_page_sans_rien_calculer():
    t = "Nos tarifs. Formule Pro : à partir de 49 € par mois HT, sans engagement. Contactez-nous."
    e = conc.extraire_prix(t)
    assert e is not None and "49 €" in e and "par mois" in e
    assert conc.extraire_prix("Aucun tarif public, sur devis uniquement.") is None
    assert conc.extraire_prix("Pack à 1 200 euros par an") is not None
    assert len(conc.extraire_prix("x " * 200 + "99 €" + " y" * 200, max_caracteres=50)) <= 50


# ------------------------------------------------------------------ classement des résultats -----

def test_classer_outils_marqueur_domaine_exclu_et_service_local():
    exclus = cfg.domaines_exclus_concurrents()
    outils, local = conc.classer_resultats(OUTILS, PRESTATAIRES_AVEC_LOCAL, domaines_exclus=exclus, termes_zone=cfg.concurrence()["termes_zone"])
    assert [o["url"] for o in outils] == ["https://relancepro.example/accueil", "https://impayes.example/"]
    assert all(o["prix"] is None and o["prix_non_trouve"] is True for o in outils)
    assert local["present"] is True and local["url"] == "https://relance-gironde.example/" and "Gironde" in local["preuve"]


def test_classer_sans_service_local_quand_la_zone_n_est_citee_que_par_une_page_http_ou_exclue():
    exclus = cfg.domaines_exclus_concurrents() | {"annuaire.example"}
    pres = PRESTATAIRES_SANS_LOCAL + [_r("https://annuaire.example/bordeaux", "Annuaire des prestataires de Bordeaux")]
    _, local = conc.classer_resultats(OUTILS, pres, domaines_exclus=exclus, termes_zone=cfg.concurrence()["termes_zone"])
    assert local == {"present": False}


def test_classer_aucun_outil_si_aucun_marqueur_d_offre():
    outils, _ = conc.classer_resultats([_r("https://blog.example/", "Conseils de recouvrement")], [], domaines_exclus=frozenset(), termes_zone=["Bordeaux"])
    assert outils == []


# ------------------------------------------------------------------ voie web : désactivée -----

def test_desactive_rien_n_est_appele_ni_ecrit(engine_test):
    f = FournisseurFaux(OUTILS, PRESTATAIRES_AVEC_LOCAL)
    res = conc.evaluer_couple_web(engine_test, code=NAF, tache_id=TACHE, fournisseur=f, maintenant=MAINT)
    assert res.statut == "non_evalue" and "désactivé" in res.motif and res.ecrit is False
    assert f.requetes == [] and _nb(engine_test, concurrence_secteur_tache) == 0 and _nb(engine_test, recherches_web) == 0


def test_desactive_evaluer_web_ne_fait_rien(engine_test):
    _poser_fiche(engine_test)
    f = FournisseurFaux(OUTILS, PRESTATAIRES_AVEC_LOCAL)
    assert conc.evaluer_web(engine_test, fournisseur=f, maintenant=MAINT) == [] and f.requetes == []


def test_aucun_appel_reseau_par_defaut_meme_sans_fournisseur_injecte(engine_test, monkeypatch):
    def interdit(*a, **k):
        raise AssertionError("appel réseau")
    monkeypatch.setattr(fp, "get_with_retry", interdit)
    assert conc.evaluer_couple_web(engine_test, code=NAF, tache_id=TACHE, maintenant=MAINT).statut == "non_evalue"


# ------------------------------------------------------------------ voie web : activée (fournisseur simulé) -----

def test_evaluation_complete_ecrit_outils_service_local_requetes_et_prix(engine_test, monkeypatch):
    _actif(monkeypatch)
    f = FournisseurFaux(OUTILS, PRESTATAIRES_AVEC_LOCAL)
    pages = {"https://relancepro.example/accueil": _page("https://relancepro.example/accueil", "RelancePro. Formule Pro : 49 € par mois HT."),
             "https://impayes.example/": None}
    vus = []

    def recuperer(resultat):
        vus.append(resultat.url)
        return pages[resultat.url]

    res = conc.evaluer_couple_web(engine_test, code=NAF, tache_id=TACHE, fournisseur=f, recuperer=recuperer, maintenant=MAINT)
    assert (res.statut, res.nb_requetes, res.outils, res.service_local, res.ecrit) == ("evalue", 2, 2, True, True)
    assert len(f.requetes) == 2
    assert f.requetes[0][0].endswith("activités comptables") and "logiciel" in f.requetes[0][0] and f.requetes[1][0].endswith("prestataire")
    assert all(limite == 10 for _, limite in f.requetes)
    ligne = repo.dernieres_concurrences(engine_test, "2")[(NAF, TACHE)]
    assert ligne["source"] == conc.SOURCE_WEB and ligne["statut"] == "evalue" and len(ligne["requetes_json"]) == 2
    avec_prix = next(o for o in ligne["outils_json"] if o["url"].startswith("https://relancepro"))
    assert avec_prix["prix"]["source_url"] == "https://relancepro.example/accueil" and "49 €" in avec_prix["prix"]["texte"] and avec_prix["prix_non_trouve"] is False
    sans_prix = next(o for o in ligne["outils_json"] if o["url"].startswith("https://impayes"))
    assert sans_prix["prix"] is None and sans_prix["prix_non_trouve"] is True
    assert ligne["service_local_json"]["present"] is True
    assert conc.concurrence_pour_score(ligne) == {"outils_dedies": 2, "service_local": True}
    assert repo.nombre_recherches_web(engine_test, "2026-10") == 2


def test_nombre_de_pages_de_prix_borne_par_la_config(engine_test, monkeypatch):
    _actif(monkeypatch)
    beaucoup = [_r(f"https://outil{i}.example/", f"Outil {i} : logiciel de relance") for i in range(6)]
    vus = []
    conc.evaluer_couple_web(engine_test, code=NAF, tache_id=TACHE, fournisseur=FournisseurFaux(beaucoup, PRESTATAIRES_SANS_LOCAL),
                            recuperer=lambda r: vus.append(r.url), maintenant=MAINT, reglages={**cfg.concurrence(), "max_pages_prix_par_couple": 2})
    assert len(vus) == 2


def test_plafond_mensuel_strict_la_requete_n_est_jamais_posee(engine_test, monkeypatch):
    _actif(monkeypatch, plafond=1)
    f = FournisseurFaux(OUTILS, PRESTATAIRES_AVEC_LOCAL)
    res = conc.evaluer_couple_web(engine_test, code=NAF, tache_id=TACHE, fournisseur=f, maintenant=MAINT)
    assert res.statut == "non_evalue" and "plafond mensuel de 1" in res.motif
    assert len(f.requetes) == 1 and _nb(engine_test, recherches_web) == 1          # jamais 2
    assert repo.dernieres_concurrences(engine_test, "2") == {}                      # une ligne non évaluée n'est pas une évaluation
    # le mois suivant, le compteur repart de zéro
    res2 = conc.evaluer_couple_web(engine_test, code=NAF, tache_id=TACHE, fournisseur=FournisseurFaux(OUTILS, PRESTATAIRES_SANS_LOCAL),
                                   recuperer=lambda r: None, maintenant=MAINT + timedelta(days=31), reglages=None)
    assert res2.statut == "non_evalue" and res2.nb_requetes == 1


def test_plafond_zero_ferme_tout(engine_test, monkeypatch):
    _actif(monkeypatch, plafond=0)
    f = FournisseurFaux(OUTILS, PRESTATAIRES_AVEC_LOCAL)
    assert conc.evaluer_couple_web(engine_test, code=NAF, tache_id=TACHE, fournisseur=f, maintenant=MAINT).statut == "non_evalue" and f.requetes == []


def test_reservation_atomique_ne_depasse_jamais(engine_test):
    ids = [repo.reserver_recherche_web(engine_test, fournisseur="f", mois="2026-10", requete=f"q{i}", code_naf=None, tache_id=None, maximum=3, maintenant=MAINT)
           for i in range(6)]
    assert sum(1 for i in ids if i) == 3 and repo.nombre_recherches_web(engine_test, "2026-10") == 3


@pytest.mark.parametrize("sans_resultat", ["outils", "prestataires"])
def test_une_requete_vide_n_est_pas_une_preuve_d_absence(engine_test, monkeypatch, sans_resultat):
    _actif(monkeypatch)
    f = FournisseurFaux([] if sans_resultat == "outils" else OUTILS, [] if sans_resultat == "prestataires" else PRESTATAIRES_SANS_LOCAL)
    res = conc.evaluer_couple_web(engine_test, code=NAF, tache_id=TACHE, fournisseur=f, recuperer=lambda r: None, maintenant=MAINT)
    assert res.statut == "non_evalue" and "pas une preuve d'absence" in res.motif and sans_resultat in res.motif
    assert repo.dernieres_concurrences(engine_test, "2") == {}
    assert len(repo.dernieres_concurrences(engine_test, "2", seulement_evaluees=False)) == 1
    assert conc.concurrence_pour_score(repo.dernieres_concurrences(engine_test, "2", seulement_evaluees=False)[(NAF, TACHE)]) is None


def test_panne_du_fournisseur_reste_non_evalue_sans_planter(engine_test, monkeypatch):
    _actif(monkeypatch)
    res = conc.evaluer_couple_web(engine_test, code=NAF, tache_id=TACHE, fournisseur=FournisseurFaux(erreur=RuntimeError("boum")), maintenant=MAINT)
    assert res.statut == "non_evalue" and repo.dernieres_concurrences(engine_test, "2") == {}


def test_aucun_outil_mais_deux_requetes_reussies_est_une_vraie_evaluation(engine_test, monkeypatch):
    _actif(monkeypatch)
    res = conc.evaluer_couple_web(engine_test, code=NAF, tache_id=TACHE, maintenant=MAINT, recuperer=lambda r: None,
                                  fournisseur=FournisseurFaux([_r("https://blog.example/", "Conseils")], PRESTATAIRES_SANS_LOCAL))
    assert (res.statut, res.outils, res.service_local) == ("evalue", 0, False)


def test_un_echec_ulterieur_n_efface_pas_une_evaluation(engine_test, monkeypatch):
    _actif(monkeypatch)
    conc.evaluer_couple_web(engine_test, code=NAF, tache_id=TACHE, fournisseur=FournisseurFaux(OUTILS, PRESTATAIRES_SANS_LOCAL), recuperer=lambda r: None, maintenant=MAINT)
    conc.evaluer_couple_web(engine_test, code=NAF, tache_id=TACHE, fournisseur=FournisseurFaux(erreur=RuntimeError("x")), maintenant=MAINT + timedelta(days=1))
    assert repo.dernieres_concurrences(engine_test, "2")[(NAF, TACHE)]["statut"] == "evalue"
    assert len(repo.dernieres_concurrences(engine_test, "2", seulement_evaluees=False)[(NAF, TACHE)]["requetes_json"]) in (0, 2)


def test_evaluer_web_traite_les_meilleures_fiches_hors_exclues_et_saute_les_recentes(engine_test, monkeypatch):
    _actif(monkeypatch)
    _poser_fiche(engine_test, tache="relance_impayes", score_prudent=70)
    _poser_fiche(engine_test, tache="facturation_clients", score_prudent=65)
    _poser_fiche(engine_test, tache="traduction", score_prudent=90, decision="exclue")
    f = FournisseurFaux(OUTILS, PRESTATAIRES_SANS_LOCAL)
    res = conc.evaluer_web(engine_test, max_couples=1, fournisseur=f, recuperer=lambda r: None, maintenant=MAINT)
    assert [(r.tache_id, r.statut) for r in res] == [("relance_impayes", "evalue")]
    res2 = conc.evaluer_web(engine_test, max_couples=5, fournisseur=f, recuperer=lambda r: None, maintenant=MAINT + timedelta(days=10))
    assert [r.tache_id for r in res2] == ["facturation_clients"]                     # la première est encore valide (90 jours)
    res3 = conc.evaluer_web(engine_test, max_couples=5, fournisseur=f, recuperer=lambda r: None, maintenant=MAINT + timedelta(days=100))
    assert sorted(r.tache_id for r in res3) == ["facturation_clients", "relance_impayes"]


def test_evaluer_web_s_arrete_au_plafond(engine_test, monkeypatch):
    _actif(monkeypatch, plafond=3)
    for i, t in enumerate(("relance_impayes", "facturation_clients", "traduction")):
        _poser_fiche(engine_test, tache=t, score_prudent=70 - i)
    f = FournisseurFaux(OUTILS, PRESTATAIRES_SANS_LOCAL)
    res = conc.evaluer_web(engine_test, fournisseur=f, recuperer=lambda r: None, maintenant=MAINT)
    assert len(f.requetes) == 3 and [r.statut for r in res] == ["evalue", "non_evalue"]


def test_brave_france_ajoute_pays_et_langue_et_la_classe_de_base_est_inchangee(monkeypatch):
    urls = []

    class Rep:
        def json(self):
            return {"web": {"results": []}}

    monkeypatch.setattr(fp, "get_with_retry", lambda url, **k: urls.append((url, k)) or Rep())
    monkeypatch.setenv(fp.VARIABLE_CLE, "cle-factice-de-test")
    conc.FournisseurBraveFrance().rechercher("relance logiciel", 10)
    fp.FournisseurBraveSearch().rechercher("relance logiciel", 10)
    assert "country=FR" in urls[0][0] and "search_lang=fr" in urls[0][0]
    assert "country" not in urls[1][0] and "search_lang" not in urls[1][0]
    assert urls[0][1]["headers"] == {"X-Subscription-Token": "cle-factice-de-test"}


# ------------------------------------------------------------------ score et fiches -----

def test_concurrence_pour_score_none_si_non_evalue():
    assert conc.concurrence_pour_score(None) is None
    assert conc.concurrence_pour_score({"statut": "non_evalue", "outils_json": [], "service_local_json": None}) is None
    assert conc.concurrence_pour_score({"statut": "evalue", "outils_json": [{}, {}], "service_local_json": {"present": False}}) == {"outils_dedies": 2, "service_local": False}


@pytest.mark.parametrize("conc_in,points", [(None, 0.0), ({"outils_dedies": 2, "service_local": False}, 20.0), ({"outils_dedies": 0, "service_local": False}, 10.0),
                                            ({"outils_dedies": 3, "service_local": True}, 0.0)])
def test_le_score_lit_la_concurrence_evaluee(conc_in, points):
    s = sc.calculer_score(tf._fort(), etablissements_rayon=300, declencheurs=tf._tous_declencheurs(), aujourdhui=tf.AUJOURDHUI,
                          acces=ResultatAccessibilite(True, None), delai=DelaiPremierRevenu.MOINS_DE_3_MOIS, concurrence=conc_in)
    assert s.criteres[3].points_prudent == points and s.criteres[3].statut == ("non_evalue" if conc_in is None else "prouve")
    assert (any(m.startswith("concurrence") for m in s.preuves_manquantes)) is (conc_in is None)


def test_precriblage_garde_un_couple_dont_la_concurrence_evaluee_le_rend_eligible(engine_test):
    cand = sel.evaluer_couple(tf._agregat(nb_offres_tache_estime=200, part_ic95_bas=0.12, nb_offres_tache_zone_estime=10), 300)[0]
    assert fch.precribler([cand], tf.AUJOURDHUI)[0] == []                                   # concurrence non évaluée : 59,7 < 60
    liste = {cand.cle: {"statut": "evalue", "outils_json": [{}], "service_local_json": {"present": False}}}
    assert fch.precribler([cand], tf.AUJOURDHUI, concurrences=liste)[0] == [cand]            # +20 points
    liste_locale = {cand.cle: {"statut": "evalue", "outils_json": [{}], "service_local_json": {"present": True}}}
    assert fch.precribler([cand], tf.AUJOURDHUI, concurrences=liste_locale)[0] == []         # service local établi : 0 point


def test_une_concurrence_evaluee_apres_la_fiche_declenche_le_recalcul_sans_changer_la_version(engine_test):
    tf._poser_couple(engine_test)
    cand = sel.evaluer_couple(tf._fort(), 300)[0]
    derniere = {"version_score": str(cfg.fiches()["version"]), "version_prompt": fch.VERSION_PROMPT, "calcule_le": MAINT,
                "agregats_json": fch.instantane_agregats(cand)}
    assert fch.raison_de_recalcul(cand, derniere, MAINT + timedelta(days=1)) is None
    assert fch.raison_de_recalcul(cand, derniere, MAINT + timedelta(days=1), concurrence_evaluee_le=MAINT - timedelta(hours=1)) is None
    r = fch.raison_de_recalcul(cand, derniere, MAINT + timedelta(days=1), concurrence_evaluee_le=MAINT + timedelta(hours=1))
    assert r is not None and "concurrence évaluée" in r
    naif = (MAINT + timedelta(hours=1)).replace(tzinfo=None)                                 # SQLite renvoie des dates sans fuseau
    assert fch.raison_de_recalcul(cand, derniere, MAINT + timedelta(days=1), concurrence_evaluee_le=naif) is not None


def test_v2_6_ne_change_pas_la_version_du_score():
    assert cfg.fiches()["version"] == "2026.10.1"          # V2.6 ne change PAS la version du score : aucune fiche existante n'est invalidée


def test_produire_fiches_calcule_le_score_avec_la_concurrence_evaluee(engine_test, tf_avec_cle):
    tf._poser_couple(engine_test)
    client = tf.ClientFaux()
    fch.produire_fiches(engine_test, client=client, maintenant=MAINT, aujourdhui=tf.AUJOURDHUI)
    avant = repo.dernieres_fiches(engine_test, "2")[(NAF, TACHE)]
    assert avant["score_json"]["criteres"][3]["statut"] == "non_evalue"
    repo.enregistrer_concurrence(engine_test, dict(code_naf=NAF, tache_id=TACHE, naf_version="2", source=conc.SOURCE_SESSION, statut="evalue", motif=None,
                                                   outils_json=[{"nom": "RelancePro", "url": "https://relancepro.example/", "prix": None, "prix_non_trouve": True}],
                                                   service_local_json={"present": False}, requetes_json=["a", "b"], evalue_le=MAINT + timedelta(hours=2)))
    a_produire, _, _ = fch.preparer(engine_test, maintenant=MAINT + timedelta(days=1), aujourdhui=tf.AUJOURDHUI)
    assert [raison for _, raison in a_produire] == ["concurrence évaluée depuis le dernier calcul (V2.6)"]
    fch.produire_fiches(engine_test, client=tf.ClientFaux(), maintenant=MAINT + timedelta(days=1), aujourdhui=tf.AUJOURDHUI)
    apres = repo.dernieres_fiches(engine_test, "2")[(NAF, TACHE)]
    assert apres["score_json"]["criteres"][3]["statut"] == "prouve" and apres["score_json"]["criteres"][3]["points_prudent"] == 20.0
    assert apres["score_prudent"] == pytest.approx(avant["score_prudent"] + 20.0, abs=0.05)
    a_produire2, _, _ = fch.preparer(engine_test, maintenant=MAINT + timedelta(days=2), aujourdhui=tf.AUJOURDHUI)
    assert a_produire2 == []                                                                 # recalculée une fois, puis à jour


@pytest.fixture
def tf_avec_cle(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "cle-factice-de-test")
    cfg.get_settings.cache_clear()


# ------------------------------------------------------------------ V2.6b : liste et import -----

def _fichier(**modif):
    evaluation = {
        "code_naf": NAF, "tache_id": TACHE, "date_recherche": "2026-11-05",
        "recherches": ["relance des impayés logiciel activités comptables", "relance des impayés activités comptables prestataire Bordeaux"],
        "outils": [{"nom": "RelancePro", "url": "https://relancepro.example/", "prix": {"texte": "49 € par mois HT", "citation": "à partir de 49 € par mois",
                                                                                      "source_url": "https://relancepro.example/tarifs"}},
                   {"nom": "Impayés Facile", "url": "https://impayes.example/", "prix": None, "prix_non_trouve": True}],
        "service_local": {"present": True, "nom": "Relance Gironde", "url": "https://relance-gironde.example/", "preuve": "cabinet basé à Mérignac, recouvrement pour TPE"},
        "note": "Deux outils français dédiés ; un prestataire local de recouvrement.",
    }
    evaluation.update(modif)
    return {"version_format": 1, "langue": "fr", "evaluations": [evaluation]}


def test_lister_pour_session_donne_les_meilleures_fiches_avec_requetes_francaises(engine_test):
    _poser_fiche(engine_test, tache="relance_impayes", score_prudent=70)
    _poser_fiche(engine_test, tache="facturation_clients", score_prudent=65)
    _poser_fiche(engine_test, tache="traduction", score_prudent=99, decision="exclue")
    lot = conc.lister_pour_session(engine_test, 1)
    assert lot["langue"] == "fr" and lot["version_format"] == 1 and len(lot["couples"]) == 1
    c = lot["couples"][0]
    assert (c["code_naf"], c["tache_id"], c["score_prudent"], c["deja_evalue"]) == (NAF, "relance_impayes", 70, False)
    assert "logiciel" in c["requetes_suggerees"]["outils"] and c["requetes_suggerees"]["prestataires"].endswith("prestataire") and "Bordeaux" in c["termes_zone"]
    assert len(conc.lister_pour_session(engine_test, 30)["couples"]) == 2
    json.dumps(lot, ensure_ascii=False)                                                      # sérialisable tel quel


def test_import_valide_ecrit_tout_avec_la_source_session(engine_test):
    _poser_fiche(engine_test)
    v, verifs = conc.importer_session(engine_test, _fichier(), maintenant=MAINT)
    assert v.erreurs == [] and verifs == {}
    ligne = repo.dernieres_concurrences(engine_test, "2")[(NAF, TACHE)]
    assert ligne["source"] == "session_claude" and ligne["motif"].startswith("Deux outils") and len(ligne["requetes_json"]) == 2
    assert ligne["outils_json"][0]["prix"]["source_url"] == "https://relancepro.example/tarifs" and ligne["outils_json"][0]["prix"]["verifie"] is None
    assert ligne["outils_json"][1]["prix"] is None and ligne["outils_json"][1]["prix_non_trouve"] is True
    assert conc.concurrence_pour_score(ligne) == {"outils_dedies": 2, "service_local": True}


def test_aucun_outil_et_aucun_service_local_est_valide_s_il_y_a_deux_recherches(engine_test):
    _poser_fiche(engine_test)
    v, _ = conc.importer_session(engine_test, _fichier(outils=[], service_local={"present": False}), maintenant=MAINT)
    assert v.erreurs == [] and conc.concurrence_pour_score(repo.dernieres_concurrences(engine_test, "2")[(NAF, TACHE)]) == {"outils_dedies": 0, "service_local": False}


@pytest.mark.parametrize("modif,fragment", [
    ({"code_naf": "99.99Z"}, "couple sans fiche"),
    ({"tache_id": "inventee"}, "couple sans fiche"),
    ({"date_recherche": "demain"}, "date_recherche"),
    ({"recherches": ["une seule requête"]}, "au moins 2 requêtes"),
    ({"recherches": []}, "au moins 2 requêtes"),
    ({"outils": "aucun"}, "outils : liste attendue"),
    ({"outils": [{"nom": "X", "url": "https://x.example/"}] * 1}, "nom et url https"),
    ({"outils": [{"nom": "RelancePro", "url": "http://relancepro.example/", "prix": None, "prix_non_trouve": True}]}, "url https"),
    ({"outils": [{"nom": "RelancePro", "url": "https://relancepro.example/", "prix": None}]}, "prix_non_trouve"),
    ({"outils": [{"nom": "RelancePro", "url": "https://relancepro.example/", "prix": {"texte": "49 €", "citation": "49 €", "source_url": "http://r.example"}}]}, "source_url https"),
    ({"outils": [{"nom": "RelancePro", "url": "https://relancepro.example/", "prix": {"texte": "49 €", "source_url": "https://r.example/t"}}]}, "citation"),
    ({"outils": [{"nom": "Aa", "url": "https://a.example/x", "prix": None, "prix_non_trouve": True}, {"nom": "Aaa", "url": "https://a.example/y", "prix": None, "prix_non_trouve": True}]}, "déjà listé"),
    ({"service_local": {"present": True, "nom": "X"}}, "service_local présent"),
    ({"service_local": {"present": True, "nom": "Relance Gironde", "url": "http://r.example", "preuve": "basés à Mérignac"}}, "service_local présent"),
    ({"service_local": {"present": "oui"}}, "service_local"),
    ({"service_local": None}, "service_local"),
    ({"note": 3}, "note"),
])
def test_import_refuse_et_n_ecrit_rien(engine_test, modif, fragment):
    _poser_fiche(engine_test)
    v, _ = conc.importer_session(engine_test, _fichier(**modif), maintenant=MAINT)
    assert any(fragment in e for e in v.erreurs), v.erreurs
    assert repo.dernieres_concurrences(engine_test, "2", seulement_evaluees=False) == {}


def test_import_refuse_langue_version_structure_et_doublon(engine_test):
    _poser_fiche(engine_test)
    for donnees, fragment in [({**_fichier(), "langue": "en"}, "langue"), ({**_fichier(), "version_format": 2}, "version_format"), ([], "objet JSON"),
                              ({"version_format": 1, "langue": "fr", "evaluations": []}, "non vide"), ({"version_format": 1, "langue": "fr"}, "non vide"),
                              ({"version_format": 1, "langue": "fr", "evaluations": ["x"]}, "objet attendu")]:
        v, _ = conc.importer_session(engine_test, donnees)
        assert any(fragment in e for e in v.erreurs), (fragment, v.erreurs)
    deux = _fichier()
    deux["evaluations"].append(dict(deux["evaluations"][0]))
    v, _ = conc.importer_session(engine_test, deux)
    assert any("en double" in e for e in v.erreurs) and repo.dernieres_concurrences(engine_test, "2", seulement_evaluees=False) == {}


def test_tout_ou_rien_une_erreur_sur_deux_evaluations_n_importe_aucune(engine_test):
    _poser_fiche(engine_test, tache="relance_impayes")
    _poser_fiche(engine_test, tache="facturation_clients")
    bon = _fichier(tache_id="relance_impayes")["evaluations"][0]
    mauvais = {**bon, "tache_id": "facturation_clients", "recherches": ["x"]}
    v, _ = conc.importer_session(engine_test, {"version_format": 1, "langue": "fr", "evaluations": [bon, mauvais]})
    assert v.erreurs and repo.dernieres_concurrences(engine_test, "2", seulement_evaluees=False) == {}


def test_toutes_les_erreurs_sont_listees_d_un_coup(engine_test):
    _poser_fiche(engine_test)
    v, _ = conc.importer_session(engine_test, _fichier(recherches=["x"], date_recherche="?", service_local={"present": True}))
    assert len(v.erreurs) >= 3


def test_verification_des_sources_citation_retrouvee_ou_non_sans_reseau(engine_test):
    _poser_fiche(engine_test)
    pages = {"https://relancepro.example/tarifs": _page("https://relancepro.example/tarifs", "Tarifs. Offre Pro : À PARTIR DE 49 €   par mois HT.")}
    v, verifs = conc.importer_session(engine_test, _fichier(), maintenant=MAINT, verifier=True, recuperer=lambda r: pages.get(r.url))
    assert verifs == {"verifie": 1, "non_verifie": 0}
    assert repo.dernieres_concurrences(engine_test, "2")[(NAF, TACHE)]["outils_json"][0]["prix"]["verifie"] is True
    repo.enregistrer_concurrence(engine_test, dict(code_naf=NAF, tache_id=TACHE, naf_version="2", source="x", statut="non_evalue", motif=None, outils_json=[],
                                                   service_local_json=None, requetes_json=[], evalue_le=MAINT + timedelta(days=1)))
    pages["https://relancepro.example/tarifs"] = _page("https://relancepro.example/tarifs", "Page sans prix.")
    _, verifs2 = conc.importer_session(engine_test, _fichier(), maintenant=MAINT + timedelta(days=2), verifier=True, recuperer=lambda r: pages.get(r.url))
    assert verifs2 == {"verifie": 0, "non_verifie": 1}
    _, verifs3 = conc.importer_session(engine_test, _fichier(), maintenant=MAINT + timedelta(days=3), verifier=True, recuperer=lambda r: None)
    assert verifs3 == {"verifie": 0, "non_verifie": 1}                                       # page illisible : non vérifié, jamais bloquant
    assert len(repo.dernieres_concurrences(engine_test, "2", seulement_evaluees=False)) == 1


# ------------------------------------------------------------------ métriques et CLI -----

def test_metriques_concurrence(engine_test, monkeypatch):
    m = calculer_metriques(engine_test, MAINT.date())["concurrence_v2"]
    assert m["couples_evalues"] == 0 and m["recherches_web_ce_mois"] == 0 and m["plafond_mensuel"] == 2000 and "désactivé" in m["fournisseur_web"]
    _poser_fiche(engine_test)
    conc.importer_session(engine_test, _fichier(), maintenant=MAINT)
    repo.reserver_recherche_web(engine_test, fournisseur="f", mois=datetime.now(timezone.utc).strftime("%Y-%m"), requete="q", code_naf=None, tache_id=None,
                                maximum=10, maintenant=MAINT)
    m = conc.metriques_concurrence(engine_test)
    assert m["couples_evalues"] == 1 and m["par_source"] == {"session_claude": 1} and m["avec_service_local"] == 1 and m["recherches_web_ce_mois"] == 1
    assert m["cout_estime_eur_ce_mois"] > 0
    with engine_test.begin() as cx:
        recherches_web.drop(cx)
    assert calculer_metriques(engine_test, MAINT.date())["concurrence_v2"] is None


@pytest.fixture
def base_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'cli.db'}")
    cfg.get_settings.cache_clear()
    from app.storage.db import get_engine, migrer
    get_engine.cache_clear()                      # `get_engine` est en cache : sans cela, deux tests partageraient la même base
    moteur = get_engine()
    migrer(moteur)
    _poser_fiche(moteur)
    yield moteur
    get_engine.cache_clear()


def test_cli_evaluer_web_desactive_ne_fait_rien(base_cli, capsys):
    assert cli.main(["concurrence", "--evaluer-web"]) == 0
    sortie = capsys.readouterr().out
    assert "désactivé" in sortie and "non évalué" in sortie and _nb(base_cli, recherches_web) == 0


def test_cli_etat(base_cli, capsys):
    assert cli.main(["concurrence", "--etat"]) == 0
    assert json.loads(capsys.readouterr().out)["plafond_mensuel"] == 2000


def test_cli_lister_puis_importer(base_cli, tmp_path, capsys):
    sortie = tmp_path / "travail.json"
    assert cli.main(["concurrence", "--lister-session", "--top", "30", "--sortie", str(sortie)]) == 0
    assert json.loads(sortie.read_text(encoding="utf-8"))["couples"][0]["tache_id"] == TACHE
    fichier = tmp_path / "rempli.json"
    fichier.write_text(json.dumps(_fichier(), ensure_ascii=False), encoding="utf-8")
    assert cli.main(["concurrence", "--importer", str(fichier)]) == 0
    assert "1 évaluations importées" in capsys.readouterr().out
    assert len(repo.dernieres_concurrences(base_cli, "2")) == 1


def test_cli_importer_refuse_un_fichier_invalide_ou_illisible(base_cli, tmp_path, capsys):
    mauvais = tmp_path / "mauvais.json"
    mauvais.write_text(json.dumps(_fichier(recherches=["x"])), encoding="utf-8")
    assert cli.main(["concurrence", "--importer", str(mauvais)]) == 2
    assert "IMPORT REFUSÉ, rien n'a été écrit" in capsys.readouterr().out and repo.dernieres_concurrences(base_cli, "2", seulement_evaluees=False) == {}
    cassé = tmp_path / "casse.json"
    cassé.write_text("{pas du json", encoding="utf-8")
    assert cli.main(["concurrence", "--importer", str(cassé)]) == 2
    assert cli.main(["concurrence", "--importer", str(tmp_path / "absent.json")]) == 2


# ------------------------------------------------------------------ la procédure écrite reste valide -----

def test_l_exemple_json_de_la_procedure_passe_la_validation_du_code():
    """Si le format change dans le code sans que PROCEDURE-V2.6b.md suive, ce test casse : la session suivrait un mode d'emploi faux."""
    import re
    from pathlib import Path

    texte = (Path(__file__).resolve().parent.parent / "PROCEDURE-V2.6b.md").read_text(encoding="utf-8")
    bloc = re.search(r"```json\n(.*?)\n```", texte, re.DOTALL).group(1)
    v = conc.valider_import(json.loads(bloc), {("69.20Z", "relance_impayes")})
    assert v.erreurs == [] and len(v.lignes) == 1
    assert "python -m app.cli concurrence --lister-session" in texte and "--importer" in texte and "--verifier-sources" in texte


def test_les_options_de_cli_citees_par_la_procedure_existent():
    parser = cli._construire_parser()
    for argv in (["concurrence", "--etat"], ["concurrence", "--lister-session", "--top", "30", "--sortie", "x.json"],
                 ["concurrence", "--importer", "x.json", "--verifier-sources"], ["fiches", "--estimer"], ["fiches", "--max-fiches", "30"]):
        parser.parse_args(argv)


# ------------------------------------------------------------------ décision : service local établi (Mathéo, 2026-10-01) -----

LOCAL = {"present": True, "nom": "Relance Gironde", "url": "https://relance-gironde.example/", "preuve": "basé à Mérignac"}


def test_service_local_etabli_envoie_en_a_verifier_avec_nom_et_source():
    (decision, motifs), s = tf._decider(critique=tf._critique())
    assert decision == "eligible_prospection"                                          # sans service local : inchangé
    s2 = sc.ResultatScore(criteres=s.criteres, score_brut=s.score_brut, score_prudent=s.score_prudent)
    decision, motifs = fch.decider(s2, tf._critique(), ResultatAccessibilite(True, None), "deux", 40, service_local=LOCAL)
    assert decision == "a_verifier"
    assert any("service local identifié : Relance Gironde" in m and "https://relance-gironde.example/" in m for m in motifs)


@pytest.mark.parametrize("local", [None, {"present": False}, {}])
def test_sans_service_local_la_decision_ne_change_pas(local):
    (_, _), s = tf._decider(critique=tf._critique())
    assert fch.decider(s, tf._critique(), ResultatAccessibilite(True, None), "deux", 40, service_local=local)[0] == "eligible_prospection"


def test_service_local_ne_masque_pas_une_exclusion():
    (_, _), s = tf._decider(critique=tf._critique())
    assert fch.decider(s, tf._critique(), ResultatAccessibilite(False, "x"), "deux", 40, service_local=LOCAL)[0] == "exclue"


def test_produire_fiches_applique_le_service_local_evalue(engine_test, tf_avec_cle):
    tf._poser_couple(engine_test)
    repo.enregistrer_concurrence(engine_test, dict(code_naf=NAF, tache_id=TACHE, naf_version="2", source=conc.SOURCE_SESSION, statut="evalue", motif=None,
                                                   outils_json=[], service_local_json=LOCAL, requetes_json=["a", "b"], evalue_le=MAINT - timedelta(days=1)))
    fch.produire_fiches(engine_test, client=tf.ClientFaux(), maintenant=MAINT, aujourdhui=tf.AUJOURDHUI)
    f = repo.dernieres_fiches(engine_test, "2")[(NAF, TACHE)]
    assert f["decision"] == "a_verifier" and any("Relance Gironde" in m for m in f["motifs_json"])
