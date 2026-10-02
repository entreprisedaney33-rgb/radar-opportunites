"""V2.8b (RADAR-V2.md) : établissements mesurés hors de Render et importés par le worker ; diagnostic réseau SIRENE ; réessai lent.
Aucun réseau, aucune dépense : fichiers d'import SYNTHÉTIQUES (SIRET, noms et adresses inventés), exceptions réseau fabriquées."""
from __future__ import annotations

import json
import socket
import ssl
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import requests
from sqlalchemy import func, select
from urllib3 import exceptions as u3

from app import cycle_v2 as cyc
from app import etablissements as etab
from app import import_etablissements as imp
from app import referentiels
from app.adapters import http as http_module
from app.adapters import recherche_entreprises as api
from app.storage import repo
from app.storage.schema import etablissements_secteur, journal_http, prospection

NAF = "69.20Z"
NAF_VERSION = referentiels.secteurs_tpe().naf_version
MESURE = datetime(2026, 10, 2, 7, 0, tzinfo=timezone.utc)
URL = f"{api.URL_RECHERCHE}?activite_principale={NAF}&departement=33"


# ------------------------------------------------------------------ fabrique de fichiers -----

def _prospect(n, dep="33", lat=44.84, lon=-0.58):
    return [f"{123456789:09d}{n:05d}", f"CABINET FICTIF N{n}", f"{n} RUE DU TEST", f"{dep}000", f"{dep}063", "VILLE FICTIVE",
            lat, lon, "02", "PME", n == 1]


def _paire(code=NAF, dep="33", n=3, mesure=MESURE, **kw):
    prospects = [] if dep == "FR" else [_prospect(i, dep) for i in range(1, n + 1)]
    p = {"code_naf": code, "departement": dep, "mesure_le": mesure.isoformat(), "nb_entreprises_actives": 1145,
         "comptage_plafonne": False, "nb_etablissements_listes": None if dep == "FR" else len(prospects),
         "echantillon_complet": None if dep == "FR" else False, "plafond_echantillon": None if dep == "FR" else 100,
         "requetes": 1 if dep == "FR" else 4, "source_url": URL, "prospects": prospects}
    p.update(kw)
    return p


def _fichier(tmp_path, paires, **entete):
    contenu = {"format": imp.FORMAT, "naf_version": NAF_VERSION, "genere_le": MESURE.isoformat(), "plafond_echantillon_par_paire": 100,
               "colonnes_prospect": list(imp.COLONNES_PROSPECT), "paires": paires, **entete}
    chemin = tmp_path / "etablissements_import.json"
    chemin.write_text(json.dumps(contenu, ensure_ascii=False), encoding="utf-8")
    return chemin


def _compte(engine, table):
    with engine.connect() as cx:
        return cx.execute(select(func.count()).select_from(table)).scalar_one()


# ------------------------------------------------------------------ import -----

def test_import_ecrit_comptage_horodate_a_la_mesure_et_prospects_avec_distance(engine_test, tmp_path):
    r = imp.importer(engine_test, _fichier(tmp_path, [_paire(n=3), _paire(dep="FR")]))
    assert r.erreur is None and r.paires_importees == 2 and r.prospects_nouveaux == 3
    derniers = repo.derniers_comptages_etablissements(engine_test, NAF_VERSION)
    ligne = derniers[(NAF, "33")]
    assert ligne["nb_entreprises_actives"] == 1145 and ligne["nb_etablissements_listes"] == 3 and ligne["source_url"] == URL
    assert ligne["horodatage"].replace(tzinfo=timezone.utc) == MESURE  # date de la MESURE, pas de l'import
    assert derniers[(NAF, "FR")]["nb_etablissements_listes"] is None
    with engine_test.connect() as cx:
        lignes = cx.execute(select(prospection)).mappings().all()
    assert all(lp["distance_centre_km"] is not None and lp["distance_centre_km"] < 5 for lp in lignes)
    assert {lp["siren"] for lp in lignes} == {"123456789"}
    assert repo.etablissements_dans_le_rayon_par_code(engine_test, NAF_VERSION, rayon_km=100) == {NAF: 3}


def test_reimporter_le_meme_fichier_ne_fait_rien(engine_test, tmp_path):
    chemin = _fichier(tmp_path, [_paire()])
    imp.importer(engine_test, chemin)
    avant = (_compte(engine_test, etablissements_secteur), _compte(engine_test, prospection))
    r = imp.importer(engine_test, chemin)
    assert r.fichier_inchange and r.paires_importees == 0  # fichier inchangé : pas même relu
    r = imp.importer(engine_test, chemin, forcer=True)
    assert r.paires_deja_a_jour == 1 and r.paires_importees == 0
    imp.reinitialiser_cache()  # comme un redémarrage du worker
    assert imp.importer(engine_test, chemin).paires_deja_a_jour == 1
    assert (_compte(engine_test, etablissements_secteur), _compte(engine_test, prospection)) == avant


def test_une_mesure_plus_recente_en_base_n_est_jamais_ecrasee(engine_test, tmp_path):
    repo.enregistrer_comptage_etablissements(
        engine_test, code_naf=NAF, naf_version=NAF_VERSION, departement="33", nb_entreprises_actives=9, comptage_plafonne=False,
        nb_etablissements_listes=0, echantillon_complet=True, plafond_echantillon=500, requetes=1, source_url=URL,
        horodatage=MESURE + timedelta(hours=1))
    r = imp.importer(engine_test, _fichier(tmp_path, [_paire()]))
    assert r.paires_deja_a_jour == 1 and r.paires_importees == 0 and _compte(engine_test, prospection) == 0
    assert repo.derniers_comptages_etablissements(engine_test, NAF_VERSION)[(NAF, "33")]["nb_entreprises_actives"] == 9


def test_une_mesure_plus_recente_dans_le_fichier_s_ajoute_sans_rien_supprimer(engine_test, tmp_path):
    imp.importer(engine_test, _fichier(tmp_path, [_paire(n=3)]))
    imp.reinitialiser_cache()
    r = imp.importer(engine_test, _fichier(tmp_path, [_paire(n=2, mesure=MESURE + timedelta(days=31), nb_entreprises_actives=1200)]))
    assert r.paires_importees == 1 and r.prospects_deja_connus == 2 and r.prospects_nouveaux == 0
    assert _compte(engine_test, etablissements_secteur) == 2 and _compte(engine_test, prospection) == 3  # le prospect 3 reste
    assert repo.derniers_comptages_etablissements(engine_test, NAF_VERSION)[(NAF, "33")]["nb_entreprises_actives"] == 1200


def test_paires_importees_ne_sont_plus_a_mesurer_par_sirene(engine_test, tmp_path):
    zone = referentiels.zone()
    paires = [_paire(dep=d) for d in zone.departements_zone()] + [_paire(dep="FR")]
    imp.importer(engine_test, _fichier(tmp_path, paires))
    assert (NAF, "33") not in etab.paires_a_rafraichir(engine_test, maintenant=MESURE + timedelta(days=1), code=NAF)
    assert etab.paires_a_rafraichir(engine_test, maintenant=MESURE + timedelta(days=1), code=NAF) == []


def test_secteur_exclu_inconnu_ou_hors_zone_ignore_et_compte(engine_test, tmp_path):
    exclu = next(s.code for s in referentiels.secteurs_tpe().secteurs if s.exclusion)
    paires = [_paire(), _paire(code=exclu), _paire(code="99.99Z"), _paire(dep="75")]
    r = imp.importer(engine_test, _fichier(tmp_path, paires))
    assert r.erreur is None and r.paires_importees == 1 and r.paires_hors_referentiel == 3
    assert {c for c, _d in repo.derniers_comptages_etablissements(engine_test, NAF_VERSION)} == {NAF}


def test_fichier_absent_n_est_pas_une_erreur(engine_test, tmp_path):
    r = imp.importer(engine_test, tmp_path / "nulle_part.json")
    assert r.absent and r.erreur is None and r.paires_importees == 0


@pytest.mark.parametrize("modifier, message", [
    (lambda c: c.update(format="autre"), "format"),
    (lambda c: c.update(naf_version="2.1"), "naf_version"),
    (lambda c: c.update(colonnes_prospect=["siret"]), "colonnes_prospect"),
    (lambda c: c.update(genere_le="hier"), "genere_le"),
    (lambda c: c["paires"][1]["prospects"][0].__setitem__(0, "123"), "SIRET invalide"),
    (lambda c: c["paires"][1]["prospects"][1].__setitem__(0, c["paires"][1]["prospects"][0][0]), "SIRET en double"),
    (lambda c: c["paires"][1]["prospects"][0].__setitem__(1, " "), "raison sociale"),
    (lambda c: c["paires"][1]["prospects"][0].__setitem__(4, "75056"), "hors du département"),
    (lambda c: c["paires"][1]["prospects"][0].__setitem__(6, "44.8"), "latitude"),
    (lambda c: c["paires"][1].update(mesure_le="2026-10-02T07:00:00"), "fuseau"),
    (lambda c: c["paires"][1].update(nb_entreprises_actives=-1), "entier positif"),
    (lambda c: c["paires"][1].update(nb_etablissements_listes=99), "≠ prospects"),
    (lambda c: c["paires"][1].update(source_url="https://exemple.invalid/x"), "source_url"),
    (lambda c: c["paires"][0].update(prospects=[_prospect(1)]), "comptage seul"),
    (lambda c: c["paires"].append(dict(c["paires"][1])), "en double"),
])
def test_fichier_invalide_n_importe_rien_du_tout(engine_test, tmp_path, modifier, message):
    chemin = _fichier(tmp_path, [_paire(dep="FR"), _paire(n=2)])
    contenu = json.loads(chemin.read_text(encoding="utf-8"))
    modifier(contenu)
    chemin.write_text(json.dumps(contenu), encoding="utf-8")
    r = imp.importer(engine_test, chemin)
    assert r.erreur and message in r.erreur and r.paires_importees == 0
    assert _compte(engine_test, etablissements_secteur) == 0 and _compte(engine_test, prospection) == 0


def test_json_illisible_refuse(engine_test, tmp_path):
    chemin = tmp_path / "x.json"
    chemin.write_text("{pas du json", encoding="utf-8")
    r = imp.importer(engine_test, chemin)
    assert r.erreur and "illisible" in r.erreur


def test_un_fichier_refuse_est_relu_quand_il_est_corrige(engine_test, tmp_path):
    chemin = _fichier(tmp_path, [_paire()], format="autre")
    assert imp.importer(engine_test, chemin).erreur
    _fichier(tmp_path, [_paire()])
    assert imp.importer(engine_test, chemin).paires_importees == 1


def test_base_indisponible_au_milieu_ne_leve_pas_et_reprend_ensuite(engine_test, tmp_path, monkeypatch):
    chemin = _fichier(tmp_path, [_paire(dep="16"), _paire(dep="33")])
    vrai = repo.enregistrer_comptage_etablissements
    appels = []

    def panne_a_la_deuxieme(*a, **kw):
        appels.append(kw["departement"])
        if len(appels) == 2:
            raise RuntimeError("base coupée")
        return vrai(*a, **kw)
    monkeypatch.setattr(repo, "enregistrer_comptage_etablissements", panne_a_la_deuxieme)
    r = imp.importer(engine_test, chemin)
    assert r.erreur and "interrompu" in r.erreur and r.paires_importees == 1
    monkeypatch.setattr(repo, "enregistrer_comptage_etablissements", vrai)
    r = imp.importer(engine_test, chemin)  # empreinte non retenue après une erreur : le fichier est relu
    assert r.paires_importees == 1 and r.paires_deja_a_jour == 1


def test_le_fichier_n_entraine_aucune_requete_reseau(engine_test, tmp_path, monkeypatch):
    monkeypatch.setattr(http_module.requests, "get", lambda *a, **k: pytest.fail("aucun appel réseau pendant l'import"))
    assert imp.importer(engine_test, _fichier(tmp_path, [_paire()])).paires_importees == 1


# ------------------------------------------------------------------ script de comptage local (écriture, fusion, reprise) -----

def test_script_ecrit_un_fichier_que_le_worker_accepte_et_fusionne(tmp_path, monkeypatch):
    from scripts import compter_etablissements_local as script
    mesurees = []

    def faux_mesurer(code, dep, plafond):
        mesurees.append((code, dep))
        return _paire(code=code, dep=dep, n=0 if dep == "FR" else 2, mesure=datetime.now(timezone.utc))
    monkeypatch.setattr(script, "mesurer", faux_mesurer)
    sortie = tmp_path / "data" / "etablissements_import.json"
    assert script.main(["--paires", "69.20Z/33,69.20Z/FR", "--sortie", str(sortie)]) == 0
    assert imp.lire_fichier(sortie)["paires"][0]["code_naf"] == NAF
    # Fusion : une autre paire s'ajoute, les deux premières (mesurées il y a moins d'un jour) sont sautées.
    assert script.main(["--paires", "69.20Z/33,69.20Z/16", "--sortie", str(sortie)]) == 0
    assert mesurees == [(NAF, "33"), (NAF, "FR"), (NAF, "16")]
    assert {(p["code_naf"], p["departement"]) for p in imp.lire_fichier(sortie)["paires"]} == {(NAF, "33"), (NAF, "FR"), (NAF, "16")}
    # --reprendre-jours 0 : tout est remesuré
    assert script.main(["--paires", "69.20Z/33", "--sortie", str(sortie), "--reprendre-jours", "0"]) == 0
    assert mesurees[-1] == (NAF, "33")


def test_script_refuse_une_paire_hors_referentiel(tmp_path):
    from scripts import compter_etablissements_local as script
    with pytest.raises(SystemExit):
        script.main(["--paires", "99.99Z/33", "--sortie", str(tmp_path / "x.json")])


def test_script_s_arrete_apres_trois_echecs_et_garde_ce_qui_est_mesure(tmp_path, monkeypatch):
    from scripts import compter_etablissements_local as script

    def faux_mesurer(code, dep, plafond):
        if dep != "16":
            raise http_module.ErreurCollecte("panne")
        return _paire(code=code, dep=dep, mesure=datetime.now(timezone.utc))
    monkeypatch.setattr(script, "mesurer", faux_mesurer)
    sortie = tmp_path / "x.json"
    assert script.main(["--paires", "69.20Z/16,69.20Z/17,69.20Z/24,69.20Z/33,69.20Z/40", "--sortie", str(sortie)]) == 1
    assert [p["departement"] for p in imp.lire_fichier(sortie)["paires"]] == ["16"]


def test_script_mesurer_convertit_l_echantillon_au_format_du_fichier(monkeypatch):
    from scripts import compter_etablissements_local as script
    prospect = api.Prospect(siret="12345678900011", siren="123456789", raison_sociale="CABINET FICTIF", code_naf=NAF, adresse="1 RUE",
                            code_postal="33000", code_commune="33063", commune="BORDEAUX", latitude=44.8, longitude=-0.6,
                            tranche_effectif_salarie="02", categorie_entreprise="PME", est_siege=True)
    monkeypatch.setattr(api, "collecter_echantillon", lambda code, dep, plafond: api.Echantillon(
        code_naf=code, departement=dep, total_entreprises=1145, prospects=[prospect], pages_lues=4, url_premiere_page=URL))
    ligne = script.mesurer(NAF, "33", 100)
    assert ligne["prospects"] == [["12345678900011", "CABINET FICTIF", "1 RUE", "33000", "33063", "BORDEAUX", 44.8, -0.6, "02", "PME", True]]
    assert ligne["nb_etablissements_listes"] == 1 and ligne["requetes"] == 4 and ligne["plafond_echantillon"] == 100


# ------------------------------------------------------------------ diagnostic réseau (type exact d'erreur) -----

def _chaine_urllib3(raison: BaseException) -> requests.ConnectionError:
    """Comme requests : ConnectionError(MaxRetryError(reason=NewConnectionError)) dont la cause est l'erreur socket."""
    nouvelle = u3.NewConnectionError(None, f"Failed to establish a new connection: {raison}")
    nouvelle.__cause__ = raison
    return requests.ConnectionError(u3.MaxRetryError(None, "/search", reason=nouvelle))


@pytest.mark.parametrize("exc, attendu", [
    (_chaine_urllib3(socket.gaierror(-2, "Name or service not known")), "dns"),
    (requests.ConnectionError(u3.MaxRetryError(None, "/", reason=u3.NameResolutionError("h", None, socket.gaierror(-3, "x")))), "dns"),
    (_chaine_urllib3(ConnectionRefusedError(111, "Connection refused")), "refus"),
    (_chaine_urllib3(ConnectionResetError(104, "Connection reset by peer")), "reinitialisation"),
    (requests.exceptions.SSLError(u3.MaxRetryError(None, "/", reason=u3.SSLError(ssl.SSLError("certificate verify failed")))), "tls"),
    (requests.ConnectionError(u3.MaxRetryError(None, "/", reason=u3.SSLError("x"))), "tls"),
    (requests.exceptions.ConnectTimeout("connect timeout=10"), "delai_connexion"),
    (requests.exceptions.ReadTimeout("read timeout=10"), "delai_lecture"),
    (requests.exceptions.ProxyError("proxy"), "proxy"),
    (requests.ConnectionError("Failed to resolve 'recherche-entreprises.api.gouv.fr'"), "dns"),
    (requests.ConnectionError("panne réseau"), "autre"),
])
def test_classement_du_type_d_erreur_reseau(exc, attendu):
    assert http_module.classer_erreur_reseau(exc) == attendu


def test_new_connection_error_n_est_pas_pris_pour_un_delai(monkeypatch):
    # urllib3 2.x : NewConnectionError hérite de ConnectTimeoutError ; un refus ne doit jamais être classé « délai ».
    assert http_module.classer_erreur_reseau(_chaine_urllib3(ConnectionRefusedError(111, "x"))) == "refus"


def test_sirene_journalise_le_detail_de_l_erreur_reseau(engine_test, monkeypatch):
    def _get(*a, **kw):
        raise _chaine_urllib3(ConnectionRefusedError(111, "Connection refused"))
    monkeypatch.setattr(http_module.requests, "get", _get)
    monkeypatch.setattr(http_module.time, "sleep", lambda *_a, **_k: None)
    with pytest.raises(http_module.ErreurCollecte) as info:
        api.lire_page(NAF, "33", engine=engine_test)
    assert info.value.type_erreur == "erreur_reseau:refus" and "[erreur_reseau:refus]" in str(info.value)
    with engine_test.connect() as cx:
        assert cx.execute(select(journal_http.c.erreur)).scalar_one() == "erreur_reseau:refus"


def test_les_autres_appelants_gardent_le_libelle_court(engine_test, monkeypatch):
    def _get(*a, **kw):
        raise _chaine_urllib3(ConnectionRefusedError(111, "Connection refused"))
    monkeypatch.setattr(http_module.requests, "get", _get)
    monkeypatch.setattr(http_module.time, "sleep", lambda *_a, **_k: None)
    with pytest.raises(http_module.ErreurCollecte) as info:
        http_module.get_with_retry("https://exemple.invalid/x", engine=engine_test, contexte="rss:x")
    assert info.value.type_erreur == "erreur_reseau" and not str(info.value).startswith("[")


def test_erreur_http_porte_son_code_dans_type_erreur(monkeypatch):
    class Rep:
        status_code = 503

        def raise_for_status(self):
            raise requests.HTTPError("503")
    monkeypatch.setattr(http_module.requests, "get", lambda *a, **k: Rep())
    monkeypatch.setattr(http_module.time, "sleep", lambda *_a, **_k: None)
    with pytest.raises(http_module.ErreurCollecte) as info:
        http_module.get_with_retry("https://exemple.invalid/x")
    assert info.value.type_erreur == "http_503"


# ------------------------------------------------------------------ réessai lent de SIRENE -----

class Horloge:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def sirene_en_panne(monkeypatch):
    appels = []

    def _get(url, **kw):
        appels.append(url)
        raise _chaine_urllib3(ConnectionRefusedError(111, "Connection refused"))
    monkeypatch.setattr(http_module.requests, "get", _get)
    monkeypatch.setattr(http_module.time, "sleep", lambda *_a, **_k: None)
    horloge = Horloge(MESURE)
    monkeypatch.setattr(etab, "_maintenant", horloge)
    return SimpleNamespace(appels=appels, horloge=horloge)


def test_apres_le_disjoncteur_sirene_attend_30_minutes_sans_aucune_requete(engine_test, sirene_en_panne):
    r = etab.rafraichir_etablissements(engine_test, code=NAF)
    assert "3 échecs consécutifs" in r.arret and "erreur_reseau:refus" in r.arret and "réessai lent" in r.arret
    assert etab.sirene_en_attente(MESURE) == MESURE + timedelta(minutes=30)
    n = len(sirene_en_panne.appels)
    sirene_en_panne.horloge.t = MESURE + timedelta(minutes=29)
    r = etab.rafraichir_etablissements(engine_test, code=NAF)
    assert r.arret.startswith("SIRENE en attente") and r.requetes == 0 and r.paires_en_echec == 0
    assert len(sirene_en_panne.appels) == n  # aucune requête pendant l'attente


def test_a_l_echeance_une_seule_paire_sonde_et_relance_l_attente(engine_test, sirene_en_panne):
    etab.rafraichir_etablissements(engine_test, code=NAF)
    sirene_en_panne.horloge.t = MESURE + timedelta(minutes=31)
    r = etab.rafraichir_etablissements(engine_test, code=NAF)
    assert r.paires_en_echec == 1 and "sonde" in r.arret
    assert etab.sirene_en_attente(sirene_en_panne.horloge.t) == MESURE + timedelta(minutes=61)


def test_une_sonde_reussie_rouvre_la_passe_normale(engine_test, sirene_en_panne, monkeypatch):
    etab.rafraichir_etablissements(engine_test, code=NAF)
    sirene_en_panne.horloge.t = MESURE + timedelta(minutes=31)

    class Rep:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"results": [], "total_results": 0, "total_pages": 0}
    monkeypatch.setattr(http_module.requests, "get", lambda *a, **k: Rep())
    r = etab.rafraichir_etablissements(engine_test, code=NAF)
    assert r.arret is None and r.paires_mesurees == 7
    assert etab.sirene_en_attente(sirene_en_panne.horloge.t) is None


def test_le_delai_du_reessai_lent_vient_de_la_config(monkeypatch):
    from app import config as cfg
    reel = cfg.cycle_v2()
    assert reel["etablissements"]["reessai_lent_minutes"] == 30
    monkeypatch.setattr(cfg, "cycle_v2", lambda: {**reel, "etablissements": {"reessai_lent_minutes": 5}})
    assert etab.reessai_lent_minutes() == 5


# ------------------------------------------------------------------ branchement au cycle du worker -----

def _ops(journal, *, arret_etab=None):
    def faire(nom, **defauts):
        def op(engine, **kw):
            journal.append(nom)
            return SimpleNamespace(**defauts)
        return op
    return cyc.Operations(
        collecter_offres=faire("offres", requetes=3, codes_en_echec=0, arret=None),
        rafraichir_etablissements=faire("etablissements", requetes=0, paires_en_echec=0, arret=arret_etab),
        etiqueter_offres=faire("etiquetage", offres_prevues=0, offres_traitees=0, arret=None),
        calculer_agregats=faire("agregation"), produire_fiches=faire("fiches", arret=None, fiches_ecrites=0),
        importer_etablissements=faire("import", paires_importees=2, erreur=None, absent=False, fichier_inchange=False, genere_le="x",
                                      paires_fichier=2, paires_deja_a_jour=0, paires_hors_referentiel=0, prospects_nouveaux=5,
                                      prospects_deja_connus=0),
    )


def test_le_regime_quotidien_importe_avant_de_mesurer_et_d_agreger(engine_test):
    journal = []
    cyc.cycle_quotidien(engine_test, _ops(journal), horloge=lambda: MESURE)
    assert journal[0] == "import" and journal.index("import") < journal.index("etablissements") < journal.index("agregation")
    run = cyc._dernier_run(engine_test, cyc.MODE_QUOTIDIEN)
    assert "import_etablissements" in run["resume_json"]["etapes"]


def test_le_worker_importe_au_demarrage(engine_test):
    journal = []
    cyc.executer_cycle_v2(engine_test, ops=_ops(journal), une_iteration=True, horloge=lambda: MESURE, environ={})
    assert journal[:2] == ["import", "import"]  # au démarrage, puis au début de la passe quotidienne


def test_une_erreur_d_import_n_arrete_pas_le_cycle(engine_test):
    journal = []
    ops = _ops(journal)

    def casse(engine, **kw):
        raise RuntimeError("fichier verrouillé")
    ops.importer_etablissements = casse
    bilan = cyc.cycle_quotidien(engine_test, ops, horloge=lambda: MESURE)
    assert "agregation" in journal and bilan is not None


def test_import_reel_par_defaut_depuis_le_chemin_de_la_config(engine_test, tmp_path, monkeypatch):
    chemin = _fichier(tmp_path, [_paire()])
    monkeypatch.setattr(imp, "chemin_fichier", lambda: chemin)
    r = cyc.importer_etablissements(engine_test, cyc.Operations())
    assert r.paires_importees == 1


def test_paires_en_attente_de_sirene_ne_retiennent_pas_la_cartographie_initiale(engine_test, monkeypatch):
    monkeypatch.setattr(cyc, "reste_a_faire", lambda engine, codes: {"paires_etablissements": 132, "codes_sans_collecte": 0,
                                                                     "offres_a_etiqueter": 0, "fiches_a_produire": 0})
    monkeypatch.setattr(cyc, "avancement", lambda engine: {})
    run_id = repo.creer_run(engine_test, mode=cyc.MODE_INITIALE, version_code="x", version_config="x", quotas={})
    suivi = cyc.Suivi(engine_test, run_id, {})
    journal = []
    terminee, _ = cyc.une_passe_initiale(engine_test, _ops(journal), suivi, enveloppe=10.0, priorite=1)
    assert terminee is False  # SIRENE n'est pas en attente : les paires retiennent la cartographie (comportement V2.8)
    monkeypatch.setattr(etab, "_sirene_en_attente_jusqua", datetime.now(timezone.utc) + timedelta(minutes=20))
    terminee, _ = cyc.une_passe_initiale(engine_test, _ops(journal), suivi, enveloppe=10.0, priorite=1)
    assert terminee is True
    assert any("en attente de SIRENE" in ligne for ligne in suivi.etat["journal"])
