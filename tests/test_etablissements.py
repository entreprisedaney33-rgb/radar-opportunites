"""Sous-étape V2.2 (RADAR-V2.md) : adaptateur API Recherche d'entreprises + rafraîchissement des
établissements. Aucun réseau (`requests.get` simulé), aucune dépense : fixtures SYNTHÉTIQUES (noms,
SIRET et adresses inventés) calquées sur la forme réelle d'une réponse de l'API (relevée le 2026-10-01)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from sqlalchemy import func, inspect, select

from app import etablissements as etab
from app import referentiels
from app.adapters import http as http_module
from app.adapters import recherche_entreprises as api
from app.metriques import calculer_metriques
from app.storage import repo
from app.storage.schema import etablissements_secteur, journal_http, prospection

BORDEAUX = (44.8378, -0.5792)
NAF = "69.20Z"


# ------------------------------------------------------------------ faux serveur -----

def _etablissement(code, dept, n, *, etat="A", naf=None, commune=None, lat=44.84, lon=-0.58, siege=False):
    return {
        "activite_principale": naf or code, "activite_principale_naf25": None, "adresse": f"{n} RUE DU TEST {dept}000 VILLE",
        "code_postal": f"{dept}000", "commune": commune or f"{dept}{100 + n}", "libelle_commune": f"VILLE{n}",
        "date_fermeture": None, "est_siege": siege, "etat_administratif": etat, "latitude": str(lat), "longitude": str(lon),
        "siret": f"{abs(hash((code, dept))) % 10**9:09d}{n:05d}", "tranche_effectif_salarie": "02",
    }


def _entreprise(code, dept, n, **kw):
    etab_ok = _etablissement(code, dept, n, **kw)
    return {
        "siren": etab_ok["siret"][:9], "nom_complet": f"CABINET FICTIF {code} {dept} N{n}", "nom_raison_sociale": "X",
        "activite_principale": code, "categorie_entreprise": "PME", "etat_administratif": "A",
        "matching_etablissements": [etab_ok],
    }


class FauxAPI:
    """Sert de `requests.get`. `totaux[(code, dept)]` = nombre d'entreprises ; défaut `defaut`."""

    def __init__(self, defaut=30, totaux=None, pannes=None):
        self.defaut, self.totaux, self.pannes = defaut, totaux or {}, pannes or set()
        self.appels: list[dict] = []

    def __call__(self, url, **kw):
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        self.appels.append({"url": url, "q": q, "kw": kw})
        code, dept = q["activite_principale"], q.get("departement")
        if (code, dept) in self.pannes:
            return _Reponse(500, {})
        total = self.totaux.get((code, dept), self.defaut)
        page, par_page = int(q["page"]), int(q["per_page"])
        debut = (page - 1) * par_page
        n_pages = max(1, -(-total // par_page))
        entreprises = [_entreprise(code, dept or "33", i) for i in range(debut, min(total, debut + par_page))]
        return _Reponse(200, {"results": entreprises, "total_results": total, "page": page, "per_page": par_page, "total_pages": n_pages})


class _Reponse:
    def __init__(self, status, corps):
        self.status_code, self._corps = status, corps

    def json(self):
        if isinstance(self._corps, Exception):
            raise self._corps
        return self._corps

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


@pytest.fixture
def faux_api(monkeypatch):
    serveur = FauxAPI()
    monkeypatch.setattr(http_module.requests, "get", serveur)
    monkeypatch.setattr(http_module.time, "sleep", lambda s: None)
    monkeypatch.setitem(http_module.DELAIS_MIN_PAR_HOTE_SECONDES, api.HOTE, 0.0)
    return serveur


# ------------------------------------------------------------------ construction d'URL -----

def test_url_departement_et_parametres():
    q = {k: v[0] for k, v in parse_qs(urlsplit(api.construire_url("69.20Z", "33", page=3)).query).items()}
    assert q["activite_principale"] == "69.20Z" and q["departement"] == "33"
    assert q["etat_administratif"] == "A" and q["page"] == "3" and q["per_page"] == "25"
    assert q["minimal"] == "true" and q["include"] == "matching_etablissements"


def test_url_france_entiere_sans_departement():
    assert "departement" not in api.construire_url("69.20Z", None)


@pytest.mark.parametrize("par_page", [0, 26])
def test_url_par_page_hors_bornes_refuse(par_page):
    with pytest.raises(ValueError):
        api.construire_url("69.20Z", "33", par_page=par_page)


def test_url_ne_contient_aucun_secret_et_vise_le_bon_hote():
    url = api.construire_url("69.20Z", "33")
    assert urlsplit(url).hostname == "recherche-entreprises.api.gouv.fr"
    assert "key" not in url.lower() and "token" not in url.lower()


def test_debit_reste_sous_les_7_requetes_par_seconde_de_l_api():
    assert 1 / http_module.DELAIS_MIN_PAR_HOTE_SECONDES[api.HOTE] <= 5


# ------------------------------------------------------------------ lecture et validation -----

def test_lire_page_parse_les_totaux(faux_api):
    page = api.lire_page(NAF, "33", page=1)
    assert (page.total_resultats, page.total_pages, page.page) == (30, 2, 1)
    assert len(page.entreprises) == 25


def test_compter_demande_une_seule_entreprise(faux_api):
    page = api.compter_entreprises_actives(NAF, "33")
    assert page.total_resultats == 30
    assert faux_api.appels[0]["q"]["per_page"] == "1" and faux_api.appels[0]["q"]["limite_matching_etablissements"] == "1"


def test_reponse_non_json_levee_en_erreur_de_collecte(monkeypatch, faux_api):
    monkeypatch.setattr(http_module.requests, "get", lambda *a, **k: _Reponse(200, ValueError("pas json")))
    with pytest.raises(http_module.ErreurCollecte, match="non JSON"):
        api.lire_page(NAF, "33")


@pytest.mark.parametrize("corps, message", [
    ({"total_results": 1, "total_pages": 1}, "`results` absent"),
    ({"results": [], "total_pages": 1}, "total_results"),
    ({"results": [], "total_results": "12", "total_pages": 1}, "total_results"),
    ({"results": [], "total_results": 0}, "total_pages"),
    ([], "`results` absent"),
])
def test_reponse_hors_format_refusee(monkeypatch, faux_api, corps, message):
    monkeypatch.setattr(http_module.requests, "get", lambda *a, **k: _Reponse(200, corps))
    with pytest.raises(http_module.ErreurCollecte, match=message):
        api.lire_page(NAF, "33")


def test_erreur_http_remontee_en_erreur_de_collecte(monkeypatch, faux_api):
    monkeypatch.setattr(http_module.requests, "get", lambda *a, **k: _Reponse(500, {}))
    with pytest.raises(http_module.ErreurCollecte):
        api.lire_page(NAF, "33")


def test_429_persistant_leve_trop_de_requetes(monkeypatch, faux_api):
    monkeypatch.setattr(http_module.requests, "get", lambda *a, **k: _Reponse(429, {}))
    with pytest.raises(http_module.TropDeRequetes):
        api.lire_page(NAF, "33")


def test_appel_journalise_dans_journal_http(engine_test, faux_api):
    api.lire_page(NAF, "33", engine=engine_test)
    with engine_test.connect() as cx:
        lignes = cx.execute(select(journal_http.c.hote, journal_http.c.flux_ou_fournisseur, journal_http.c.code_http)).all()
    assert lignes == [(api.HOTE, "recherche_entreprises:sirene", 200)]


# ------------------------------------------------------------------ extraction des prospects -----

def _page(entreprises):
    return api.PageEntreprises(url="u", page=1, total_resultats=len(entreprises), total_pages=1, entreprises=entreprises)


def test_extraction_garde_actifs_meme_code_meme_departement():
    page = _page([_entreprise(NAF, "33", 1), _entreprise(NAF, "33", 2)])
    prospects = api.extraire_prospects(page, NAF, "33")
    assert [p.raison_sociale for p in prospects] == ["CABINET FICTIF 69.20Z 33 N1", "CABINET FICTIF 69.20Z 33 N2"]
    p = prospects[0]
    assert (p.code_naf, p.categorie_entreprise, p.tranche_effectif_salarie, p.est_siege) == (NAF, "PME", "02", False)
    assert (p.latitude, p.longitude) == (44.84, -0.58) and p.code_commune.startswith("33")


@pytest.mark.parametrize("etab_modifie", [
    {"etat": "F"},                      # établissement fermé
    {"naf": "45.3A"},                   # autre activité de l'établissement
    {"commune": "75056"},               # autre département
])
def test_extraction_ecarte_ferme_autre_activite_autre_departement(etab_modifie):
    assert api.extraire_prospects(_page([_entreprise(NAF, "33", 1, **etab_modifie)]), NAF, "33") == []


def test_extraction_ignore_sans_siret_sans_nom_et_dedoublonne():
    bon = _entreprise(NAF, "33", 1)
    sans_siret = _entreprise(NAF, "33", 2)
    sans_siret["matching_etablissements"][0]["siret"] = None
    sans_nom = _entreprise(NAF, "33", 3)
    sans_nom["nom_complet"] = sans_nom["nom_raison_sociale"] = None
    doublon = {**bon}
    prospects = api.extraire_prospects(_page([bon, sans_siret, sans_nom, doublon, "pas un dict"]), NAF, "33")
    assert len(prospects) == 1


def test_extraction_tolere_coordonnees_absentes_ou_illisibles():
    e = _entreprise(NAF, "33", 1)
    e["matching_etablissements"][0].update(latitude=None, longitude="abc")
    (p,) = api.extraire_prospects(_page([e]), NAF, "33")
    assert p.latitude is None and p.longitude is None


# ------------------------------------------------------------------ échantillon -----

def test_echantillon_complet_sur_deux_pages(faux_api):
    ech = api.collecter_echantillon(NAF, "33", plafond=500)
    assert (ech.total_entreprises, ech.pages_lues, len(ech.prospects), ech.complet) == (30, 2, 30, True)
    assert "departement=33" in ech.url_premiere_page


def test_echantillon_coupe_au_plafond(faux_api):
    ech = api.collecter_echantillon(NAF, "33", plafond=10)
    assert len(ech.prospects) == 10 and ech.complet is False and ech.pages_lues == 1


def test_echantillon_coupe_au_garde_fou_de_pages(faux_api):
    faux_api.totaux[(NAF, "33")] = 400
    ech = api.collecter_echantillon(NAF, "33", plafond=500, pages_max=3)
    assert ech.pages_lues == 3 and len(ech.prospects) == 75 and ech.complet is False


def test_echantillon_secteur_vide(faux_api):
    faux_api.totaux[(NAF, "33")] = 0
    ech = api.collecter_echantillon(NAF, "33")
    assert (ech.total_entreprises, ech.prospects, ech.complet, ech.pages_lues) == (0, [], True, 1)


def test_distance_haversine():
    assert api.distance_km(*BORDEAUX, *BORDEAUX) == pytest.approx(0, abs=1e-6)
    assert api.distance_km(*BORDEAUX, 48.8566, 2.3522) == pytest.approx(499, abs=10)  # Bordeaux-Paris


# ------------------------------------------------------------------ une paire -----

def test_paire_departement_ecrit_comptage_et_prospects_avec_distance(engine_test, faux_api):
    faux_api.totaux[(NAF, "33")] = 3
    requetes, nouveaux, connus = etab.rafraichir_paire(engine_test, NAF, "33")
    assert (requetes, nouveaux, connus) == (1, 3, 0)
    with engine_test.connect() as cx:
        mesure = cx.execute(select(etablissements_secteur)).mappings().one()
        lignes = cx.execute(select(prospection)).mappings().all()
    assert mesure["naf_version"] == "2" and mesure["departement"] == "33"
    assert (mesure["nb_entreprises_actives"], mesure["nb_etablissements_listes"], mesure["echantillon_complet"]) == (3, 3, True)
    assert mesure["plafond_echantillon"] == 500 and mesure["requetes"] == 1
    assert "activite_principale=69.20Z" in mesure["source_url"] and "departement=33" in mesure["source_url"]
    assert len(lignes) == 3 and all(l["naf_version"] == "2" and l["code_naf"] == NAF for l in lignes)
    assert all(l["distance_centre_km"] < 5 for l in lignes)  # coordonnées de test proches du centre


def test_paire_france_comptage_seul(engine_test, faux_api):
    requetes, nouveaux, connus = etab.rafraichir_paire(engine_test, NAF, "FR")
    assert (requetes, nouveaux, connus) == (1, 0, 0)
    assert "departement" not in faux_api.appels[0]["q"]
    with engine_test.connect() as cx:
        mesure = cx.execute(select(etablissements_secteur)).mappings().one()
        assert cx.execute(select(func.count()).select_from(prospection)).scalar_one() == 0
    assert mesure["departement"] == "FR" and mesure["nb_entreprises_actives"] == 30
    assert mesure["nb_etablissements_listes"] is None and mesure["echantillon_complet"] is None


def test_deuxieme_mesure_ajoute_une_ligne_et_conserve_premiere_collecte(engine_test, faux_api):
    faux_api.totaux[(NAF, "33")] = 2
    t1 = datetime(2026, 10, 1, tzinfo=timezone.utc)
    t2 = datetime(2026, 11, 5, tzinfo=timezone.utc)
    etab.rafraichir_paire(engine_test, NAF, "33", maintenant=t1)
    requetes, nouveaux, connus = etab.rafraichir_paire(engine_test, NAF, "33", maintenant=t2)
    assert (nouveaux, connus) == (0, 2)
    with engine_test.connect() as cx:
        assert cx.execute(select(func.count()).select_from(etablissements_secteur)).scalar_one() == 2
        for l in cx.execute(select(prospection)).mappings():
            assert l["premiere_collecte"].replace(tzinfo=timezone.utc) == t1
            assert l["derniere_vue"].replace(tzinfo=timezone.utc) == t2
        assert cx.execute(select(func.count()).select_from(prospection)).scalar_one() == 2


def test_prospect_disparu_n_est_jamais_supprime(engine_test, faux_api):
    faux_api.totaux[(NAF, "33")] = 3
    etab.rafraichir_paire(engine_test, NAF, "33")
    faux_api.totaux[(NAF, "33")] = 1
    etab.rafraichir_paire(engine_test, NAF, "33")
    with engine_test.connect() as cx:
        assert cx.execute(select(func.count()).select_from(prospection)).scalar_one() == 3


def test_paire_en_echec_n_ecrit_rien(engine_test, faux_api):
    faux_api.pannes.add((NAF, "33"))
    with pytest.raises(http_module.ErreurCollecte):
        etab.rafraichir_paire(engine_test, NAF, "33")
    with engine_test.connect() as cx:
        assert cx.execute(select(func.count()).select_from(etablissements_secteur)).scalar_one() == 0
        assert cx.execute(select(func.count()).select_from(prospection)).scalar_one() == 0


# ------------------------------------------------------------------ paires à rafraîchir -----

def test_paires_a_rafraichir_secteurs_exclus_jamais_inclus(engine_test):
    paires = etab.paires_a_rafraichir(engine_test)
    codes = {c for c, _ in paires}
    refs = referentiels.secteurs_tpe()
    assert codes == {s.code for s in refs.non_exclus()}
    assert not codes & {s.code for s in refs.secteurs if s.exclusion}
    departements = {d for _, d in paires}
    assert departements == set(referentiels.zone().departements_zone()) | {"FR"}
    assert len(paires) == len(codes) * len(departements)


def test_paire_recente_sautee_ancienne_reprise(engine_test, faux_api):
    maintenant = datetime(2026, 10, 1, tzinfo=timezone.utc)
    etab.rafraichir_paire(engine_test, NAF, "FR", maintenant=maintenant - timedelta(days=10))
    etab.rafraichir_paire(engine_test, "69.10Z", "FR", maintenant=maintenant - timedelta(days=40))
    paires = etab.paires_a_rafraichir(engine_test, maintenant=maintenant)
    assert (NAF, "FR") not in paires and ("69.10Z", "FR") in paires


def test_jamais_mesurees_d_abord_puis_les_plus_anciennes(engine_test, faux_api):
    maintenant = datetime(2026, 10, 1, tzinfo=timezone.utc)
    etab.rafraichir_paire(engine_test, "69.10Z", "FR", maintenant=maintenant - timedelta(days=40))
    etab.rafraichir_paire(engine_test, NAF, "FR", maintenant=maintenant - timedelta(days=90))
    paires = etab.paires_a_rafraichir(engine_test, maintenant=maintenant)
    assert paires[-2:] == [(NAF, "FR"), ("69.10Z", "FR")]  # la plus ancienne en dernier des déjà mesurées, après toutes les jamais mesurées


def test_filtres_code_et_departement(engine_test):
    assert etab.paires_a_rafraichir(engine_test, code=NAF, departement="33") == [(NAF, "33")]
    assert len(etab.paires_a_rafraichir(engine_test, code=NAF)) == len(referentiels.zone().departements_zone()) + 1


def test_code_exclu_inconnu_ou_departement_hors_zone_refuses(engine_test):
    with pytest.raises(ValueError, match="inconnu ou exclu"):
        etab.paires_a_rafraichir(engine_test, code="47.73Z")  # pharmacie : exclue
    with pytest.raises(ValueError, match="inconnu ou exclu"):
        etab.paires_a_rafraichir(engine_test, code="99.99Z")
    with pytest.raises(ValueError, match="hors zone"):
        etab.paires_a_rafraichir(engine_test, departement="75")


# ------------------------------------------------------------------ passe complète -----

def test_passe_plafonnee_en_requetes_puis_reprise(engine_test, faux_api):
    faux_api.defaut = 3
    resume = etab.rafraichir_etablissements(engine_test, max_requetes=5, code=NAF)
    assert resume.paires_mesurees == 5 and resume.requetes == 5
    assert "plafond de 5 requêtes" in resume.arret
    reprise = etab.rafraichir_etablissements(engine_test, max_requetes=50, code=NAF)
    assert reprise.paires_prevues == 2 and reprise.paires_mesurees == 2 and reprise.arret is None


def test_disjoncteur_apres_trois_echecs_consecutifs(engine_test, faux_api):
    for dep in referentiels.zone().departements_zone():
        faux_api.pannes.add((NAF, dep))
    resume = etab.rafraichir_etablissements(engine_test, code=NAF)
    assert resume.paires_en_echec == 3 and resume.paires_mesurees == 0
    assert "3 échecs consécutifs" in resume.arret
    assert resume.paires_prevues == 7  # les 4 autres n'ont jamais été tentées


def test_un_succes_remet_le_compteur_d_echecs_a_zero(engine_test, faux_api):
    # Ordre de passage (tri par département) : 16 17 24 33 40 47 FR -> échec échec succès échec échec succès succès.
    faux_api.defaut = 1
    faux_api.pannes.update({(NAF, "16"), (NAF, "17"), (NAF, "33"), (NAF, "40")})
    resume = etab.rafraichir_etablissements(engine_test, code=NAF)
    assert resume.arret is None and resume.paires_en_echec == 4 and resume.paires_mesurees == 3


def test_max_requetes_par_passe_lit_l_environnement(monkeypatch):
    monkeypatch.delenv(etab.VARIABLE_MAX_REQUETES, raising=False)
    assert etab.max_requetes_par_passe() == 2000
    monkeypatch.setenv(etab.VARIABLE_MAX_REQUETES, "150")
    assert etab.max_requetes_par_passe() == 150
    monkeypatch.setenv(etab.VARIABLE_MAX_REQUETES, "beaucoup")
    assert etab.max_requetes_par_passe() == 2000
    monkeypatch.setenv(etab.VARIABLE_MAX_REQUETES, "0")
    assert etab.max_requetes_par_passe() == 1


# ------------------------------------------------------------------ métriques et schéma -----

def test_metriques_secteurs_couverts_et_etablissements_en_zone(engine_test, faux_api):
    faux_api.defaut = 2
    for dep in ("33", "24", "FR"):
        etab.rafraichir_paire(engine_test, NAF, dep)
    etab.rafraichir_paire(engine_test, "69.10Z", "33")
    m = calculer_metriques(engine_test, datetime(2026, 10, 1).date())["etablissements_v2"]
    assert m["naf_version"] == "2"
    assert m["secteurs_couverts"] == 2 and m["mesures_secteur_departement"] == 4
    assert m["entreprises_actives_departements_zone"] == 6  # 33 + 24 + (69.10Z) 33, 2 chacune, FR exclu
    assert m["prospects_total"] == 6 and m["prospects_dans_le_rayon"] == 6  # 3 mesures départementales x 2
    assert m["derniere_mesure"] is not None


def test_prospect_hors_rayon_non_compte_dans_le_rayon(engine_test, faux_api, monkeypatch):
    faux_api.defaut = 1
    original = api.lire_page

    def lointain(*a, **k):  # établissement à Lyon : ~440 km de Bordeaux
        page = original(*a, **k)
        for e in page.entreprises:
            e["matching_etablissements"][0].update(latitude="45.76", longitude="4.84")
        return page

    monkeypatch.setattr(api, "lire_page", lointain)
    etab.rafraichir_paire(engine_test, NAF, "33")
    m = calculer_metriques(engine_test, datetime(2026, 10, 1).date())["etablissements_v2"]
    assert m["prospects_total"] == 1 and m["prospects_dans_le_rayon"] == 0


def test_metriques_sans_tables_v2_renvoient_none_sans_erreur(engine_test):
    with engine_test.begin() as cx:
        prospection.drop(cx)
        etablissements_secteur.drop(cx)
    assert calculer_metriques(engine_test, datetime(2026, 10, 1).date())["etablissements_v2"] is None


def test_metriques_base_vide_zero_partout(engine_test):
    m = calculer_metriques(engine_test, datetime(2026, 10, 1).date())["etablissements_v2"]
    assert m["secteurs_couverts"] == 0 and m["prospects_total"] == 0 and m["derniere_mesure"] is None


def test_tables_creees_avec_naf_version_et_migration_idempotente(engine_test):
    from app.storage.db import migrer

    migrer(engine_test)
    migrer(engine_test)
    inspecteur = inspect(engine_test)
    for table in ("etablissements_secteur", "prospection"):
        assert "naf_version" in {c["name"] for c in inspecteur.get_columns(table)}


def test_une_meme_installation_ne_cree_qu_une_ligne_par_siret_et_version(engine_test):
    ligne = {"siret": "1" * 14, "code_naf": NAF, "departement": "33", "siren": None, "raison_sociale": "X", "adresse": None,
             "code_postal": None, "code_commune": None, "commune": None, "latitude": None, "longitude": None,
             "distance_centre_km": None, "tranche_effectif_salarie": None, "categorie_entreprise": None, "est_siege": None}
    assert repo.enregistrer_prospects(engine_test, [ligne], naf_version="2") == (1, 0)
    assert repo.enregistrer_prospects(engine_test, [ligne], naf_version="2") == (0, 1)
    assert repo.enregistrer_prospects(engine_test, [ligne], naf_version="2.1") == (1, 0)  # autre nomenclature : autre ligne
    assert repo.enregistrer_prospects(engine_test, [], naf_version="2") == (0, 0)


# ------------------------------------------------------------------ plafond de 10 000 de l'API -----

@pytest.mark.parametrize("total, plafonne", [(0, False), (9999, False), (10000, True), (12000, True)])
def test_total_plafonne_par_l_api_est_signale(total, plafonne):
    page = api.PageEntreprises(url="u", page=1, total_resultats=total, total_pages=1, entreprises=[])
    assert page.plafonne is plafonne


def test_comptage_plafonne_enregistre_comme_borne_basse(engine_test, faux_api):
    faux_api.totaux[(NAF, None)] = 10000
    faux_api.totaux[(NAF, "33")] = 4
    etab.rafraichir_paire(engine_test, NAF, "FR")
    etab.rafraichir_paire(engine_test, NAF, "33")
    derniers = repo.derniers_comptages_etablissements(engine_test, "2")
    assert derniers[(NAF, "FR")]["comptage_plafonne"] is True and derniers[(NAF, "FR")]["nb_entreprises_actives"] == 10000
    assert derniers[(NAF, "33")]["comptage_plafonne"] is False


def test_echantillon_departement_plafonne_signale(faux_api):
    faux_api.totaux[(NAF, "33")] = 10000
    ech = api.collecter_echantillon(NAF, "33", plafond=10)
    assert ech.total_plafonne is True and ech.complet is False


def test_metriques_comptent_les_mesures_plafonnees(engine_test, faux_api):
    faux_api.totaux[(NAF, None)] = 10000
    etab.rafraichir_paire(engine_test, NAF, "FR")
    etab.rafraichir_paire(engine_test, NAF, "33")
    m = calculer_metriques(engine_test, datetime(2026, 10, 1).date())["etablissements_v2"]
    assert m["mesures_plafonnees_api"] == 1


def test_etablissements_des_agences_d_interim_jamais_mesures(engine_test):
    codes = {c for c, _ in etab.paires_a_rafraichir(engine_test)}
    assert len(codes) == 158 and not codes & {"78.10Z", "78.20Z"}
    with pytest.raises(ValueError, match="inconnu ou exclu"):
        etab.paires_a_rafraichir(engine_test, code="78.20Z")
