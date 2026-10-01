"""Sous-étape V2.3 (RADAR-V2.md) : adaptateur France Travail (jeton OAuth, recherche paginée, découpage par
dates), table `offres_emploi`, collecte, métriques. Aucun réseau (`requests` simulé), 0 € : fixtures
SYNTHÉTIQUES calquées sur la forme documentée de l'API (documentation officielle lue le 2026-10-01)."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from sqlalchemy import func, inspect, select

from app import offres as off
from app import referentiels
from app.adapters import france_travail as ft
from app.adapters import http as http_module
from app.metriques import calculer_metriques
from app.storage import repo
from app.storage.schema import collectes_offres, journal_http, offres_emploi

NAF = "69.20Z"
MAINTENANT = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
SECRET_DE_TEST = "faux-secret-de-test"


# ------------------------------------------------------------------ faux serveurs -----

def _offre(n, cree, *, naf=NAF, commune="33063", salaire="Mensuel de 2000.00 Euros sur 12 mois"):
    return {
        "id": f"OFF{n:06d}", "intitule": f"Assistant administratif {n} (H/F)", "description": "Relance des clients et saisie des factures.",
        "dateCreation": cree.strftime("%Y-%m-%dT%H:%M:%SZ"), "dateActualisation": cree.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "lieuTravail": {"libelle": "33 - BORDEAUX", "latitude": 44.84, "longitude": -0.58, "codePostal": "33000", "commune": commune},
        "romeCode": "M1602", "typeContrat": "CDI", "entreprise": {"nom": "Cabinet Fictif"}, "salaire": {"libelle": salaire},
        "codeNAF": naf, "trancheEffectifEtab": "1 à 5 employés",
    }


class _Rep:
    def __init__(self, status, corps=None, headers=None):
        self.status_code, self._corps, self.headers = status, corps, headers or {}

    def json(self):
        if isinstance(self._corps, Exception):
            raise self._corps
        return self._corps

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


def _dt(texte):
    return datetime.strptime(texte, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


class FauxFT:
    """Sert de `requests.get` / `requests.post`. `offres[code]` = liste d'offres ; filtre sur les dates et le range."""

    def __init__(self):
        self.offres: dict[str, list[dict]] = {}
        self.appels_get: list[dict] = []
        self.appels_post: list[dict] = []
        self.panne_codes: set[str] = set()
        self.reponse_jeton = lambda: _Rep(200, {"access_token": "jeton-factice", "expires_in": 1500})
        self.partiel_header = True

    def post(self, url, data=None, **kw):
        self.appels_post.append({"url": url, "data": dict(data or {}), "kw": kw})
        return self.reponse_jeton()

    def get(self, url, **kw):
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        self.appels_get.append({"url": url, "q": q, "headers": kw.get("headers", {})})
        code = q["codeNAF"]
        if code in self.panne_codes:
            return _Rep(500, {})
        debut, fin = _dt(q["minCreationDate"]), _dt(q["maxCreationDate"])
        trouvees = sorted((o for o in self.offres.get(code, []) if debut <= _dt(o["dateCreation"]) <= fin),
                          key=lambda o: o["dateCreation"], reverse=True)
        if not trouvees:
            return _Rep(204)
        p, d = (int(x) for x in q["range"].split("-"))
        page = trouvees[p:d + 1]
        statut = 200 if len(page) == len(trouvees) else 206
        return _Rep(statut, {"resultats": page}, {"Content-Range": f"offres {p}-{p + len(page) - 1}/{len(trouvees)}"})


@pytest.fixture
def faux(monkeypatch):
    serveur = FauxFT()
    monkeypatch.setattr(http_module.requests, "get", serveur.get)
    monkeypatch.setattr(http_module.requests, "post", serveur.post)
    monkeypatch.setattr(http_module.time, "sleep", lambda s: None)
    monkeypatch.setitem(http_module.DELAIS_MIN_PAR_HOTE_SECONDES, ft.HOTE_API, 0.0)
    monkeypatch.setitem(http_module.DELAIS_MIN_PAR_HOTE_SECONDES, ft.HOTE_JETON, 0.0)
    monkeypatch.setenv(ft.VARIABLE_CLIENT_ID, "client-de-test")
    monkeypatch.setenv(ft.VARIABLE_CLIENT_SECRET, SECRET_DE_TEST)
    monkeypatch.setattr(ft, "FICHIER_ENV", ft.Path("/inexistant/env"))
    return serveur


def _jeton_fixe():
    j = ft.Jeton("jeton-factice", time.monotonic() + 3600)
    return lambda: j


# ------------------------------------------------------------------ identifiants -----

def test_identifiants_depuis_l_environnement(faux):
    assert ft.lire_identifiants() == ("client-de-test", SECRET_DE_TEST)


def test_identifiants_depuis_le_fichier_hors_depot(monkeypatch, tmp_path):
    monkeypatch.delenv(ft.VARIABLE_CLIENT_ID, raising=False)
    monkeypatch.delenv(ft.VARIABLE_CLIENT_SECRET, raising=False)
    fichier = tmp_path / "env"
    fichier.write_text("RADAR_DATABASE_URL=sqlite:///x\n# commentaire\nRADAR_FT_CLIENT_ID=id-fichier\nRADAR_FT_CLIENT_SECRET=secret-fichier\n")
    monkeypatch.setattr(ft, "FICHIER_ENV", fichier)
    assert ft.lire_identifiants() == ("id-fichier", "secret-fichier")


def test_environnement_prime_sur_le_fichier(faux, monkeypatch, tmp_path):
    fichier = tmp_path / "env"
    fichier.write_text("RADAR_FT_CLIENT_ID=id-fichier\nRADAR_FT_CLIENT_SECRET=secret-fichier\n")
    monkeypatch.setattr(ft, "FICHIER_ENV", fichier)
    assert ft.lire_identifiants()[0] == "client-de-test"


def test_identifiants_absents_message_clair_sans_valeur(monkeypatch):
    monkeypatch.delenv(ft.VARIABLE_CLIENT_ID, raising=False)
    monkeypatch.delenv(ft.VARIABLE_CLIENT_SECRET, raising=False)
    monkeypatch.setattr(ft, "FICHIER_ENV", ft.Path("/inexistant/env"))
    with pytest.raises(ft.IdentifiantsAbsents) as exc:
        ft.lire_identifiants()
    assert ft.VARIABLE_CLIENT_ID in str(exc.value) and ft.VARIABLE_CLIENT_SECRET in str(exc.value)


def test_un_seul_identifiant_present_ne_suffit_pas(faux, monkeypatch):
    monkeypatch.delenv(ft.VARIABLE_CLIENT_SECRET)
    with pytest.raises(ft.IdentifiantsAbsents):
        ft.lire_identifiants()


# ------------------------------------------------------------------ jeton -----

def test_jeton_requete_client_credentials(faux):
    jeton = ft.obtenir_jeton()
    assert jeton.valeur == "jeton-factice" and jeton.valide()
    appel = faux.appels_post[0]
    assert appel["url"] == ft.URL_JETON and "realm=%2Fpartenaire" in appel["url"]
    assert appel["data"] == {"grant_type": "client_credentials", "client_id": "client-de-test",
                             "client_secret": SECRET_DE_TEST, "scope": "api_offresdemploiv2 o2dsoffre"}


def test_scope_modifiable_par_l_environnement(faux, monkeypatch):
    monkeypatch.setenv(ft.VARIABLE_SCOPE, "api_offresdemploiv2")
    ft.obtenir_jeton()
    assert faux.appels_post[0]["data"]["scope"] == "api_offresdemploiv2"


def test_jeton_ne_s_affiche_jamais(faux):
    jeton = ft.obtenir_jeton()
    assert "jeton-factice" not in repr(jeton) and "jeton-factice" not in str(jeton)


def test_jeton_expire_avec_marge():
    assert not ft.Jeton("x", time.monotonic() + 10).valide()  # moins de 60 s : à renouveler
    assert ft.Jeton("x", time.monotonic() + 600).valide()


@pytest.mark.parametrize("corps", [{}, {"access_token": None}, "pas un dict"])
def test_reponse_de_jeton_inattendue_refusee(faux, corps):
    faux.reponse_jeton = lambda: _Rep(200, corps)
    with pytest.raises(http_module.ErreurCollecte, match="access_token absent"):
        ft.obtenir_jeton()


def test_echec_du_service_de_jeton_ne_fuit_pas_le_secret(faux):
    faux.reponse_jeton = lambda: _Rep(500, {})
    with pytest.raises(http_module.ErreurCollecte) as exc:
        ft.obtenir_jeton()
    assert SECRET_DE_TEST not in str(exc.value) and "client-de-test" not in str(exc.value)


def test_429_persistant_du_jeton_leve_trop_de_requetes(faux):
    faux.reponse_jeton = lambda: _Rep(429, {})
    with pytest.raises(http_module.TropDeRequetes):
        ft.obtenir_jeton()


def test_post_retente_puis_reussit(monkeypatch, faux):
    reponses = iter([_Rep(500), _Rep(200, {"access_token": "t", "expires_in": 100})])
    faux.reponse_jeton = lambda: next(reponses)
    assert ft.obtenir_jeton().valeur == "t" and len(faux.appels_post) == 2


def test_journal_http_du_jeton_sans_corps_ni_secret(engine_test, faux):
    ft.obtenir_jeton(engine=engine_test)
    with engine_test.connect() as cx:
        lignes = cx.execute(select(journal_http)).mappings().all()
    assert [(l["hote"], l["flux_ou_fournisseur"], l["code_http"]) for l in lignes] == [(ft.HOTE_JETON, "france_travail:jeton", 200)]
    assert SECRET_DE_TEST not in repr(lignes)


# ------------------------------------------------------------------ recherche -----

def test_url_de_recherche_parametres():
    debut, fin = MAINTENANT - timedelta(days=90), MAINTENANT
    q = {k: v[0] for k, v in parse_qs(urlsplit(ft.construire_url(NAF, debut, fin, premier=150, dernier=299)).query).items()}
    assert urlsplit(ft.construire_url(NAF, debut, fin)).hostname == "api.francetravail.io"
    assert q == {"codeNAF": NAF, "minCreationDate": "2026-07-03T12:00:00Z", "maxCreationDate": "2026-10-01T12:00:00Z",
                 "range": "150-299", "sort": "1"}


@pytest.mark.parametrize("premier, dernier", [(0, 150), (-1, 10), (10, 5)])
def test_range_invalide_refuse(premier, dernier):
    with pytest.raises(ValueError):
        ft.construire_url(NAF, MAINTENANT, MAINTENANT, premier=premier, dernier=dernier)


def test_recherche_envoie_le_jeton_en_bearer_et_lit_content_range(faux):
    faux.offres[NAF] = [_offre(i, MAINTENANT - timedelta(hours=i)) for i in range(200)]
    page = ft.rechercher(ft.Jeton("jeton-factice", time.monotonic() + 999), NAF, MAINTENANT - timedelta(days=30), MAINTENANT)
    assert faux.appels_get[0]["headers"]["Authorization"] == "Bearer jeton-factice"
    assert (page.total, page.premier, page.dernier, len(page.offres)) == (200, 0, 149, 150)


def test_204_aucune_offre(faux):
    page = ft.rechercher(ft.Jeton("j", time.monotonic() + 999), NAF, MAINTENANT - timedelta(days=5), MAINTENANT)
    assert page.offres == [] and page.total == 0


def test_200_sans_content_range_total_deduit(monkeypatch, faux):
    monkeypatch.setattr(http_module.requests, "get", lambda *a, **k: _Rep(200, {"resultats": [_offre(1, MAINTENANT)]}))
    page = ft.rechercher(ft.Jeton("j", time.monotonic() + 999), NAF, MAINTENANT, MAINTENANT)
    assert page.total == 1


@pytest.mark.parametrize("corps, message", [({"autre": 1}, "`resultats` absent"), (ValueError("x"), "non JSON"), ([], "`resultats` absent")])
def test_reponse_de_recherche_hors_format_refusee(monkeypatch, faux, corps, message):
    monkeypatch.setattr(http_module.requests, "get", lambda *a, **k: _Rep(200, corps))
    with pytest.raises(http_module.ErreurCollecte, match=message):
        ft.rechercher(ft.Jeton("j", time.monotonic() + 999), NAF, MAINTENANT, MAINTENANT)


def test_erreur_http_de_recherche_remontee(monkeypatch, faux):
    monkeypatch.setattr(http_module.requests, "get", lambda *a, **k: _Rep(500, {}))
    with pytest.raises(http_module.ErreurCollecte):
        ft.rechercher(ft.Jeton("j", time.monotonic() + 999), NAF, MAINTENANT, MAINTENANT)


# ------------------------------------------------------------------ collecte d'un code -----

def test_collecte_une_page(faux):
    faux.offres[NAF] = [_offre(i, MAINTENANT - timedelta(hours=i)) for i in range(120)]
    c = ft.collecter_code(_jeton_fixe(), NAF, MAINTENANT - timedelta(days=30), MAINTENANT)
    assert (len(c.offres), c.requetes, c.fenetres_tronquees) == (120, 1, 0)


def test_collecte_plusieurs_pages_avec_les_bons_ranges(faux):
    faux.offres[NAF] = [_offre(i, MAINTENANT - timedelta(minutes=i)) for i in range(400)]
    c = ft.collecter_code(_jeton_fixe(), NAF, MAINTENANT - timedelta(days=30), MAINTENANT)
    assert len(c.offres) == 400 and len({o["id"] for o in c.offres}) == 400
    assert [a["q"]["range"] for a in faux.appels_get] == ["0-149", "150-299", "300-399"]
    assert c.requetes == 3


def test_collecte_aucune_offre(faux):
    c = ft.collecter_code(_jeton_fixe(), NAF, MAINTENANT - timedelta(days=30), MAINTENANT)
    assert (c.offres, c.requetes) == ([], 1)


def test_fenetre_coupee_en_deux_quand_le_total_depasse_le_plafond(faux):
    faux.offres[NAF] = [_offre(i, MAINTENANT - timedelta(hours=i)) for i in range(500)]  # 500 h ~ 21 jours
    c = ft.collecter_code(_jeton_fixe(), NAF, MAINTENANT - timedelta(days=30), MAINTENANT, plafond=100)
    assert len(c.offres) == 500 and len({o["id"] for o in c.offres}) == 500 and c.fenetres_tronquees == 0
    assert c.requetes > 3  # la fenêtre a bien été découpée


def test_fenetre_d_une_heure_trop_pleine_est_marquee_tronquee_sans_boucle(faux):
    faux.offres[NAF] = [_offre(i, MAINTENANT - timedelta(minutes=10)) for i in range(300)]  # toutes à la même seconde
    c = ft.collecter_code(_jeton_fixe(), NAF, MAINTENANT - timedelta(days=2), MAINTENANT, plafond=100)
    assert c.fenetres_tronquees == 1 and len(c.offres) == 100


def test_collecte_s_arrete_si_l_api_ne_renvoie_plus_rien(monkeypatch, faux):
    premiere = _Rep(206, {"resultats": [_offre(i, MAINTENANT) for i in range(150)]}, {"Content-Range": "offres 0-149/400"})
    vide = _Rep(204)
    reponses = iter([premiere, vide])
    monkeypatch.setattr(http_module.requests, "get", lambda *a, **k: next(reponses))
    c = ft.collecter_code(_jeton_fixe(), NAF, MAINTENANT - timedelta(days=1), MAINTENANT)
    assert len(c.offres) == 150 and c.requetes == 2


def test_jeton_renouvele_au_besoin_pendant_une_collecte(faux):
    faux.offres[NAF] = [_offre(i, MAINTENANT - timedelta(minutes=i)) for i in range(300)]
    appels = []

    def fournisseur():
        appels.append(1)
        return ft.Jeton(f"jeton-{len(appels)}", time.monotonic() + 999)

    ft.collecter_code(fournisseur, NAF, MAINTENANT - timedelta(days=1), MAINTENANT)
    assert [a["headers"]["Authorization"] for a in faux.appels_get] == ["Bearer jeton-1", "Bearer jeton-2"]  # 300 offres = 2 pages, un jeton demandé par requête


# ------------------------------------------------------------------ salaire -----

@pytest.mark.parametrize("libelle, attendu", [
    ("Mensuel de 1923.00 Euros sur 12 mois", (23076.0, 23076.0)),
    ("Mensuel de 2000.00 Euros sur 13 mois", (26000.0, 26000.0)),
    ("Mensuel de 1800 Euros à 2200 Euros sur 12 mois", (21600.0, 26400.0)),
    ("Annuel de 27000.00 Euros sur 12 mois", (27000.0, 27000.0)),
    ("Annuel de 27000 Euros à 32000 Euros", (27000.0, 32000.0)),
    ("Horaire de 12.50 Euros sur 12 mois", (22750.0, 22750.0)),
    ("Horaire de 11,88 Euros", (21621.6, 21621.6)),
    ("Cachet de 100 Euros", (None, None)),
    ("Selon expérience", (None, None)),
    ("Mensuel de 5 Euros sur 12 mois", (None, None)),          # aberrant (60 € par an)
    ("Mensuel de 9999999 Euros sur 12 mois", (None, None)),    # aberrant
    ("", (None, None)),
    (None, (None, None)),
])
def test_salaire_annuel(libelle, attendu):
    assert ft.salaire_annuel_eur(libelle) == attendu


# ------------------------------------------------------------------ normalisation -----

def test_normaliser_offre_complete():
    ligne = ft.normaliser_offre(_offre(7, MAINTENANT - timedelta(days=3)))
    assert ligne["id_offre"] == "OFF000007" and ligne["code_naf"] == NAF and ligne["departement"] == "33"
    assert ligne["commune"] == "33063" and ligne["code_postal"] == "33000" and ligne["type_contrat"] == "CDI"
    assert ligne["salaire_libelle"] == "Mensuel de 2000.00 Euros sur 12 mois" and ligne["salaire_annuel_min_eur"] == 24000.0
    assert ligne["entreprise_nom"] == "Cabinet Fictif" and ligne["tranche_effectif_etab"] == "1 à 5 employés"
    assert ligne["date_creation"] == MAINTENANT - timedelta(days=3) and ligne["latitude"] == 44.84


@pytest.mark.parametrize("modif", [{"id": None}, {"id": "  "}, {"dateCreation": None}, {"dateCreation": "pas une date"}])
def test_offre_inexploitable_ignoree(modif):
    brute = {**_offre(1, MAINTENANT), **modif}
    assert ft.normaliser_offre(brute) is None
    assert ft.normaliser_offre("pas un dict") is None


@pytest.mark.parametrize("commune, dep", [("33063", "33"), ("97411", "974"), ("2A004", "2A"), ("75056", "75"), ("12", None), (None, None)])
def test_departement_depuis_commune(commune, dep):
    assert ft.departement_depuis_commune(commune) == dep


def test_offre_minimale_tolerante():
    ligne = ft.normaliser_offre({"id": "X1", "dateCreation": "2026-09-01T10:00:00Z"})
    assert ligne["intitule"] == "(sans intitulé)" and ligne["salaire_libelle"] is None and ligne["departement"] is None


# ------------------------------------------------------------------ fenêtres -----

def test_premiere_collecte_90_jours_puis_reprise_avec_chevauchement():
    debut, fin = off.fenetre_pour_code(None, MAINTENANT)
    assert fin == MAINTENANT and debut == MAINTENANT - timedelta(days=90)
    precedente = MAINTENANT - timedelta(days=1)
    debut, fin = off.fenetre_pour_code(precedente, MAINTENANT)
    assert debut == precedente - timedelta(days=1)


def test_jours_impose_un_recul_fixe():
    debut, _ = off.fenetre_pour_code(MAINTENANT - timedelta(hours=1), MAINTENANT, jours=7)
    assert debut == MAINTENANT - timedelta(days=7)


def test_fenetre_jamais_dans_le_futur():
    debut, fin = off.fenetre_pour_code(MAINTENANT + timedelta(days=5), MAINTENANT)
    assert debut <= fin


# ------------------------------------------------------------------ collecte complète -----

def test_codes_exclus_jamais_collectes_et_plus_anciens_d_abord(engine_test):
    codes = off.codes_a_collecter(engine_test)
    refs = referentiels.secteurs_tpe()
    assert set(codes) == {s.code for s in refs.non_exclus()} and not set(codes) & {s.code for s in refs.secteurs if s.exclusion}
    repo.enregistrer_collecte_offres(engine_test, code_naf=codes[0], naf_version="2", debut=MAINTENANT, fin=MAINTENANT,
                                     nb_offres=0, nb_nouvelles=0, requetes=1, fenetres_tronquees=0, horodatage=MAINTENANT)
    assert off.codes_a_collecter(engine_test)[-1] == codes[0]


def test_code_exclu_ou_inconnu_refuse(engine_test):
    with pytest.raises(ValueError, match="inconnu ou exclu"):
        off.codes_a_collecter(engine_test, code="47.73Z")
    with pytest.raises(ValueError, match="inconnu ou exclu"):
        off.codes_a_collecter(engine_test, code="99.99Z")


def test_collecte_complete_ecrit_offres_et_journal_de_collecte(engine_test, faux):
    faux.offres[NAF] = [_offre(i, MAINTENANT - timedelta(days=i)) for i in range(10)]
    r = off.collecter_offres(engine_test, code=NAF, maintenant=MAINTENANT)
    assert (r.codes_collectes, r.offres_lues, r.offres_nouvelles, r.requetes, r.arret) == (1, 10, 10, 1, None)
    with engine_test.connect() as cx:
        assert cx.execute(select(func.count()).select_from(offres_emploi)).scalar_one() == 10
        collecte = cx.execute(select(collectes_offres)).mappings().one()
    assert (collecte["code_naf"], collecte["nb_offres"], collecte["nb_nouvelles"], collecte["naf_version"]) == (NAF, 10, 10, "2")
    assert faux.appels_post[0]["data"]["grant_type"] == "client_credentials"
    assert faux.appels_get[0]["q"]["minCreationDate"] == "2026-07-03T12:00:00Z"  # 90 jours à la première collecte


def test_deuxieme_collecte_reprend_a_la_fin_moins_un_jour_et_dedoublonne(engine_test, faux):
    faux.offres[NAF] = [_offre(i, MAINTENANT - timedelta(hours=i)) for i in range(5)]
    off.collecter_offres(engine_test, code=NAF, maintenant=MAINTENANT)
    plus_tard = MAINTENANT + timedelta(days=1)
    faux.offres[NAF].append(_offre(99, plus_tard - timedelta(hours=2)))
    r = off.collecter_offres(engine_test, code=NAF, maintenant=plus_tard)
    assert faux.appels_get[-1]["q"]["minCreationDate"] == "2026-09-30T12:00:00Z"  # fin précédente (1er oct. 12h) - 1 jour
    assert r.offres_lues == 6 and r.offres_nouvelles == 1
    with engine_test.connect() as cx:
        assert cx.execute(select(func.count()).select_from(offres_emploi)).scalar_one() == 6
        for l in cx.execute(select(offres_emploi.c.premiere_collecte, offres_emploi.c.id_offre)).mappings():
            if l["id_offre"] != "OFF000099":
                assert l["premiere_collecte"].replace(tzinfo=timezone.utc) == MAINTENANT


def test_offre_disparue_n_est_jamais_supprimee(engine_test, faux):
    faux.offres[NAF] = [_offre(i, MAINTENANT - timedelta(hours=i)) for i in range(4)]
    off.collecter_offres(engine_test, code=NAF, maintenant=MAINTENANT)
    faux.offres[NAF] = faux.offres[NAF][:1]
    off.collecter_offres(engine_test, code=NAF, maintenant=MAINTENANT + timedelta(days=1))
    with engine_test.connect() as cx:
        assert cx.execute(select(func.count()).select_from(offres_emploi)).scalar_one() == 4


def test_offres_inexploitables_comptees_et_ignorees(engine_test, faux):
    faux.offres[NAF] = [_offre(1, MAINTENANT - timedelta(hours=1)), {**_offre(2, MAINTENANT - timedelta(hours=2)), "id": None}]
    r = off.collecter_offres(engine_test, code=NAF, maintenant=MAINTENANT)
    assert (r.offres_lues, r.offres_ignorees) == (1, 1)


def test_jours_explicite_remplace_la_reprise(engine_test, faux):
    off.collecter_offres(engine_test, code=NAF, maintenant=MAINTENANT)
    off.collecter_offres(engine_test, code=NAF, maintenant=MAINTENANT + timedelta(days=1), jours=7)
    assert faux.appels_get[-1]["q"]["minCreationDate"] == "2026-09-25T12:00:00Z"


def test_sans_identifiants_arret_immediat_et_clair(engine_test, faux, monkeypatch):
    monkeypatch.delenv(ft.VARIABLE_CLIENT_ID)
    monkeypatch.delenv(ft.VARIABLE_CLIENT_SECRET)
    r = off.collecter_offres(engine_test, maintenant=MAINTENANT)
    assert r.codes_collectes == 0 and ft.VARIABLE_CLIENT_ID in r.arret and faux.appels_get == [] and faux.appels_post == []
    with engine_test.connect() as cx:
        assert cx.execute(select(func.count()).select_from(collectes_offres)).scalar_one() == 0


def test_disjoncteur_apres_trois_echecs_consecutifs(engine_test, faux):
    codes = off.codes_a_collecter(engine_test)
    faux.panne_codes.update(codes[:3])
    r = off.collecter_offres(engine_test, maintenant=MAINTENANT, fournisseur_de_jeton=_jeton_fixe())
    assert r.codes_en_echec == 3 and r.codes_collectes == 0 and "3 échecs consécutifs" in r.arret


def test_un_succes_remet_le_compteur_d_echecs_a_zero(engine_test, faux):
    codes = off.codes_a_collecter(engine_test)
    faux.panne_codes.update({codes[0], codes[1], codes[3], codes[4]})
    r = off.collecter_offres(engine_test, maintenant=MAINTENANT, fournisseur_de_jeton=_jeton_fixe(), max_requetes=len(codes) + 10)
    assert r.codes_en_echec == 4 and r.arret is None and r.codes_collectes == len(codes) - 4


def test_plafond_de_requetes_et_reprise(engine_test, faux):
    r = off.collecter_offres(engine_test, maintenant=MAINTENANT, fournisseur_de_jeton=_jeton_fixe(), max_requetes=5)
    assert r.codes_collectes == 5 and "plafond de 5 requêtes" in r.arret
    r2 = off.collecter_offres(engine_test, maintenant=MAINTENANT, fournisseur_de_jeton=_jeton_fixe(), max_requetes=5)
    codes_premiere_passe = {c["q"]["codeNAF"] for c in faux.appels_get[:5]}
    codes_deuxieme_passe = {c["q"]["codeNAF"] for c in faux.appels_get[5:]}
    assert not codes_premiere_passe & codes_deuxieme_passe  # la reprise traite d'autres codes


def test_max_requetes_lit_l_environnement(monkeypatch):
    monkeypatch.delenv(off.VARIABLE_MAX_REQUETES, raising=False)
    assert off.max_requetes_par_passe() == 3000
    monkeypatch.setenv(off.VARIABLE_MAX_REQUETES, "40")
    assert off.max_requetes_par_passe() == 40
    monkeypatch.setenv(off.VARIABLE_MAX_REQUETES, "x")
    assert off.max_requetes_par_passe() == 3000


def test_fenetre_tronquee_enregistree_dans_le_journal_de_collecte(engine_test, faux, monkeypatch):
    original = ft.collecter_code
    monkeypatch.setattr(off.ft, "collecter_code", lambda *a, **k: original(*a, **{**k, "plafond": 100}))
    faux.offres[NAF] = [_offre(i, MAINTENANT - timedelta(minutes=10)) for i in range(160)]  # même seconde : indécoupable
    r = off.collecter_offres(engine_test, code=NAF, maintenant=MAINTENANT)
    assert r.fenetres_tronquees == 1 and r.offres_lues == 100
    with engine_test.connect() as cx:
        assert cx.execute(select(collectes_offres.c.fenetres_tronquees)).scalar_one() == 1


# ------------------------------------------------------------------ métriques et schéma -----

def test_metriques_offres(engine_test, faux):
    faux.offres[NAF] = [
        _offre(1, MAINTENANT - timedelta(days=2)),
        _offre(2, MAINTENANT - timedelta(days=5), commune="75056"),
        _offre(3, MAINTENANT - timedelta(days=40), salaire="Selon expérience"),
    ]
    off.collecter_offres(engine_test, code=NAF, maintenant=MAINTENANT)
    m = calculer_metriques(engine_test, MAINTENANT.date())["offres_v2"]
    assert m["offres_total"] == 3 and m["offres_creees_90_jours"] == 3
    assert m["offres_dans_les_departements_de_la_zone"] == 2  # 33 en zone ; 75 hors zone ; 33 pour la 3e aussi
    assert m["offres_avec_salaire_lisible"] == 2 and m["codes_naf_avec_offres"] == 1
    assert m["fenetres_tronquees_cumulees"] == 0 and m["derniere_collecte"] is not None


def test_metriques_offres_base_vide(engine_test):
    m = calculer_metriques(engine_test, MAINTENANT.date())["offres_v2"]
    assert m["offres_total"] == 0 and m["derniere_collecte"] is None


def test_metriques_sans_tables_v2_renvoient_none(engine_test):
    with engine_test.begin() as cx:
        offres_emploi.drop(cx)
        collectes_offres.drop(cx)
    assert calculer_metriques(engine_test, MAINTENANT.date())["offres_v2"] is None


def test_tables_creees_avec_naf_version_et_unicite_de_l_offre(engine_test):
    from app.storage.db import migrer

    migrer(engine_test)
    migrer(engine_test)
    inspecteur = inspect(engine_test)
    for table in ("offres_emploi", "collectes_offres"):
        assert "naf_version" in {c["name"] for c in inspecteur.get_columns(table)}
    ligne = ft.normaliser_offre(_offre(1, MAINTENANT))
    assert repo.enregistrer_offres(engine_test, [ligne], naf_version="2") == (1, 0)
    assert repo.enregistrer_offres(engine_test, [ligne, ligne], naf_version="2") == (0, 1)  # doublon dans le lot
    assert repo.enregistrer_offres(engine_test, [], naf_version="2") == (0, 0)


# ------------------------------------------------------------------ script d'enregistrement des identifiants -----

def _charger_script():
    import importlib.util
    from pathlib import Path

    chemin = Path(__file__).resolve().parent.parent / "scripts" / "enregistrer_identifiants_france_travail.py"
    spec = importlib.util.spec_from_file_location("enregistrer_identifiants_ft", chemin)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_script_ecrit_en_600_garde_les_autres_lignes_et_remplace_les_anciennes(monkeypatch, tmp_path, capsys):
    script = _charger_script()
    fichier = tmp_path / "radar" / "env"
    fichier.parent.mkdir()
    fichier.write_text("RADAR_DATABASE_URL=sqlite:///x\nRADAR_FT_CLIENT_ID=ancien\nRADAR_FT_CLIENT_SECRET=ancien-secret\n")
    monkeypatch.setattr(script, "FICHIER", fichier)
    monkeypatch.setattr("builtins.input", lambda *_: "nouvel-id")
    monkeypatch.setattr(script.getpass, "getpass", lambda *_: "nouveau-secret")
    assert script.main() == 0
    assert fichier.read_text().splitlines() == ["RADAR_DATABASE_URL=sqlite:///x", "RADAR_FT_CLIENT_ID=nouvel-id", "RADAR_FT_CLIENT_SECRET=nouveau-secret"]
    assert oct(fichier.stat().st_mode & 0o777) == "0o600"
    sortie = capsys.readouterr()
    assert "nouveau-secret" not in sortie.out + sortie.err  # le secret n'est jamais affiché


def test_script_refuse_un_secret_vide_sans_rien_ecrire(monkeypatch, tmp_path):
    script = _charger_script()
    fichier = tmp_path / "env"
    monkeypatch.setattr(script, "FICHIER", fichier)
    monkeypatch.setattr("builtins.input", lambda *_: "un-id")
    monkeypatch.setattr(script.getpass, "getpass", lambda *_: "  ")
    assert script.main() == 1 and not fichier.exists()


def test_script_cree_le_fichier_et_son_dossier_s_ils_n_existent_pas(monkeypatch, tmp_path):
    script = _charger_script()
    fichier = tmp_path / "neuf" / "dossier" / "env"
    monkeypatch.setattr(script, "FICHIER", fichier)
    monkeypatch.setattr("builtins.input", lambda *_: "id")
    monkeypatch.setattr(script.getpass, "getpass", lambda *_: "secret")
    assert script.main() == 0 and oct(fichier.stat().st_mode & 0o777) == "0o600"


# ------------------------------------------------------------------ filtre codeNAF ignoré par l'API -----

def test_filtre_naf_ignore_par_l_api_refuse_la_collecte(monkeypatch, faux):
    """Constat réel du 2026-10-01 : un code inconnu n'est pas refusé, il est ignoré (toutes les offres reviennent)."""
    melange = [_offre(1, MAINTENANT, naf=NAF), _offre(2, MAINTENANT, naf="10.71C")]
    monkeypatch.setattr(http_module.requests, "get",
                        lambda *a, **k: _Rep(200, {"resultats": melange}, {"Content-Range": "offres 0-1/2"}))
    with pytest.raises(http_module.ErreurCollecte, match="Filtre codeNAF ignoré"):
        ft.collecter_code(_jeton_fixe(), NAF, MAINTENANT - timedelta(days=1), MAINTENANT)


def test_filtre_naf_verifie_aussi_les_pages_suivantes(monkeypatch, faux):
    bonnes = [_offre(i, MAINTENANT) for i in range(150)]
    reponses = iter([_Rep(206, {"resultats": bonnes}, {"Content-Range": "offres 0-149/300"}),
                     _Rep(206, {"resultats": [_offre(999, MAINTENANT, naf="10.71C")]}, {"Content-Range": "offres 150-150/300"})])
    monkeypatch.setattr(http_module.requests, "get", lambda *a, **k: next(reponses))
    with pytest.raises(http_module.ErreurCollecte, match="Filtre codeNAF ignoré"):
        ft.collecter_code(_jeton_fixe(), NAF, MAINTENANT - timedelta(days=1), MAINTENANT)


def test_code_ignore_par_l_api_ne_range_aucune_offre_en_base(engine_test, faux, monkeypatch):
    faux.offres[NAF] = []
    monkeypatch.setattr(http_module.requests, "get",
                        lambda *a, **k: _Rep(200, {"resultats": [_offre(1, MAINTENANT, naf="10.71C")]}, {"Content-Range": "offres 0-0/1"}))
    r = off.collecter_offres(engine_test, code=NAF, maintenant=MAINTENANT)
    assert r.codes_en_echec == 1 and r.offres_lues == 0
    with engine_test.connect() as cx:
        assert cx.execute(select(func.count()).select_from(offres_emploi)).scalar_one() == 0


# ------------------------------------------------------------------ décision du 2026-10-01 : offres d'intérim non collectées -----

@pytest.mark.parametrize("code", ["78.10Z", "78.20Z"])
def test_offres_des_agences_d_interim_jamais_collectees(engine_test, faux, code):
    assert code not in off.codes_a_collecter(engine_test)
    with pytest.raises(ValueError, match="inconnu ou exclu"):
        off.codes_a_collecter(engine_test, code=code)


def test_collecte_complete_n_interroge_jamais_les_codes_d_agence(engine_test, faux):
    off.collecter_offres(engine_test, maintenant=MAINTENANT, fournisseur_de_jeton=_jeton_fixe(), max_requetes=10_000)
    interroges = {a["q"]["codeNAF"] for a in faux.appels_get}
    assert len(interroges) == 158 and not interroges & {"78.10Z", "78.20Z"}
