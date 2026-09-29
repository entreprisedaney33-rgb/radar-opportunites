"""Sous-étape 3.17 (AMELIORATIONS.md) : contrôles de qualité des pages de
l'Enquêteur, sans réseau -- page d'erreur ou trop courte jamais stockée, page
« prix » sans marqueur de prix qui perd son étiquette, plafond de rattachement
d'une même source à `max_dossiers_par_source` dossiers, seuil de similarité
du magasin interne."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app import config as cfg
from app.enqueteur import fetch as fetch_module
from app.enqueteur.fetch import (
    ETIQUETTE_PREUVE_ENQUETE,
    ETIQUETTE_PREUVE_PRIX,
    collecter_preuves,
    recuperer_page,
)
from app.enqueteur.fournisseurs import ResultatRecherche
from app.enqueteur.fournisseurs_gratuits import FournisseurMagasinInterne
from app.enqueteur.qualite_page import contient_marqueur_prix, motif_page_inexploitable
from app.pipeline import dedupe
from app.pipeline.budget import BudgetTracker
from app.storage import repo
from app.storage.schema import sources

TEXTE_LONG = "Un vrai article, long et détaillé sur un sujet précis. " * 12  # ~ 680 caractères


@pytest.fixture(autouse=True)
def _vrais_quotas(monkeypatch):
    """La suite ramène le seuil de longueur à 1 (tests/conftest.py) ; les tests
    de ce fichier veulent les VRAIES valeurs de `config/quotas.yaml`."""
    reel = cfg._load_yaml("quotas.yaml")
    monkeypatch.setattr(cfg, "quotas", lambda: reel)
    return reel


def _resultat(url, **kw):
    kw.setdefault("titre", "Titre")
    kw.setdefault("extrait", "Extrait")
    kw.setdefault("horodatage_source", datetime(2026, 9, 20, tzinfo=timezone.utc))
    kw.setdefault("fournisseur", "algolia_hn")
    kw.setdefault("requete_origine", "requete")
    return ResultatRecherche(url=url, **kw)


class _Robots:
    status_code = 404
    text = ""


def _servir(monkeypatch, html_par_url: dict[str, str] | str, appels: list | None = None):
    """Simule le réseau : robots.txt absent, pages servies depuis `html_par_url`
    (ou le même HTML pour toute URL)."""
    monkeypatch.setattr(fetch_module, "get_with_retry", lambda url, **kw: _Robots())

    def _page(url, **kw):
        if appels is not None:
            appels.append(url)
        html = html_par_url if isinstance(html_par_url, str) else html_par_url[url]
        return html.encode("utf-8")

    monkeypatch.setattr(fetch_module, "get_avec_limite_taille", _page)
    monkeypatch.setattr(fetch_module.time, "sleep", lambda *_a, **_kw: None)


def _page_html(corps: str, titre: str = "Article") -> str:
    return f"<html><head><title>{titre}</title></head><body><p>{corps}</p></body></html>"


def _lignes_sources(engine) -> list[dict]:
    with engine.connect() as cx:
        return [dict(r) for r in cx.execute(select(sources)).mappings().all()]


# ---------------------------------------------------- fonctions pures ----

@pytest.mark.parametrize("titre,texte", [
    ("404 Not Found", TEXTE_LONG),
    ("Article", "Page introuvable. " + TEXTE_LONG),
    ("Oops", "Sorry, page not found. " + TEXTE_LONG),
    ("Article", "Accès refusé à cette ressource. " + TEXTE_LONG),
    ("Article", "Access denied. " + TEXTE_LONG),
])
def test_page_avec_marqueur_d_erreur_est_inexploitable(titre, texte):
    assert motif_page_inexploitable(titre, texte, longueur_min=300) is not None


def test_page_trop_courte_est_inexploitable():
    assert "trop courte" in motif_page_inexploitable("Titre", "A required part of this site couldn't load.", longueur_min=300)


def test_page_normale_est_exploitable():
    assert motif_page_inexploitable("Un bon article", TEXTE_LONG, longueur_min=300) is None


def test_un_long_article_qui_mentionne_404_loin_dans_le_texte_reste_exploitable():
    """Les marqueurs d'erreur ne sont cherchés qu'en tête de page : un article
    de fond qui explique ce qu'est une erreur 404 n'est pas une page d'erreur."""
    texte = TEXTE_LONG * 5 + " Et l'erreur 404 est introuvable dans ce cas."
    assert motif_page_inexploitable("Guide", texte, longueur_min=300) is None


@pytest.mark.parametrize("texte", [
    "Offre Pro : 49 € par mois", "Only $99/year", "49 EUR /mois", "Starting at €29",
    "Choisissez votre plan", "Nos tarifs", "Pricing plans", "$20 per month",
])
def test_marqueurs_de_prix_reconnus(texte):
    assert contient_marqueur_prix(texte)


@pytest.mark.parametrize("texte", [
    "Un article sur des agents d'achat.", "Le planning de la semaine", "An explanation of the market",
    "Coût en $ inconnu",  # un « $ » sans montant n'est pas un prix
])
def test_absence_de_marqueur_de_prix(texte):
    assert not contient_marqueur_prix(texte)


# ------------------------------------------------ recuperer / collecter --

def test_recuperer_page_d_erreur_servie_en_200_n_est_jamais_renvoyee(monkeypatch):
    _servir(monkeypatch, _page_html("Erreur 404 : cette page est introuvable. " + TEXTE_LONG, titre="404"))
    assert recuperer_page(_resultat("https://a.example/x")) is None


def test_page_courte_du_type_lemonde_pricing_n_est_pas_renvoyee(monkeypatch):
    court = "A required part of this site couldn't load. This may be due to a browser extension, network issues, or browser settings."
    assert len(court) < 300
    _servir(monkeypatch, _page_html(court, titre="Le Monde"))
    assert recuperer_page(_resultat("https://www.lemonde.fr/pricing")) is None


def test_collecter_preuves_ne_stocke_pas_une_page_d_erreur(engine_test, monkeypatch):
    _servir(monkeypatch, _page_html("Page introuvable. " + TEXTE_LONG, titre="Erreur"))

    ids = collecter_preuves(engine_test, [_resultat("https://a.example/x")])

    assert ids == []
    assert _lignes_sources(engine_test) == []


def test_collecter_preuves_ne_stocke_pas_une_page_trop_courte(engine_test, monkeypatch):
    _servir(monkeypatch, _page_html("Trop court."))

    assert collecter_preuves(engine_test, [_resultat("https://a.example/x")]) == []
    assert _lignes_sources(engine_test) == []


def test_collecter_preuves_stocke_une_vraie_page(engine_test, monkeypatch):
    _servir(monkeypatch, _page_html(TEXTE_LONG))

    ids = collecter_preuves(engine_test, [_resultat("https://a.example/x")])

    assert len(ids) == 1


def test_page_prix_sans_marqueur_de_prix_perd_l_etiquette_prix(engine_test, monkeypatch):
    _servir(monkeypatch, _page_html(TEXTE_LONG, titre="Un article d'actualité"))

    ids = collecter_preuves(engine_test, [_resultat("https://a.example/pricing")], etiquette=ETIQUETTE_PREUVE_PRIX)

    assert len(ids) == 1
    assert _lignes_sources(engine_test)[0]["etiquette"] == ETIQUETTE_PREUVE_ENQUETE


def test_page_prix_avec_marqueur_de_prix_garde_l_etiquette_prix(engine_test, monkeypatch):
    _servir(monkeypatch, _page_html(TEXTE_LONG + " Offre Pro : 49 € par mois.", titre="Tarifs"))

    ids = collecter_preuves(engine_test, [_resultat("https://a.example/pricing")], etiquette=ETIQUETTE_PREUVE_PRIX)

    assert len(ids) == 1
    assert _lignes_sources(engine_test)[0]["etiquette"] == ETIQUETTE_PREUVE_PRIX


def test_page_d_enquete_ordinaire_n_est_pas_soumise_au_controle_de_prix(engine_test, monkeypatch):
    _servir(monkeypatch, _page_html(TEXTE_LONG))

    collecter_preuves(engine_test, [_resultat("https://a.example/x")], etiquette=ETIQUETTE_PREUVE_ENQUETE)

    assert _lignes_sources(engine_test)[0]["etiquette"] == ETIQUETTE_PREUVE_ENQUETE


# ------------------------------------------------- plafond de rattachement --

URL_PARTAGEE = "https://partagee.example/article"


def _dossier(engine) -> str:
    return repo.creer_opportunite(
        engine, titre="t", acheteur="a", probleme="p", mecanisme_ia="m", secteur="intersectoriel",
        statut="enquete_terminee", cluster_id=None,
    )


def _rattacher(engine, opportunity_id: str, url: str, *, claim: str = "Enquête (algolia_hn) : x") -> str:
    source_id, _ = repo.upsert_source(
        engine, url_canonique=dedupe.canonicaliser_url(url), domaine="partagee.example", date_publication=None,
        type_source="page_web", extrait=TEXTE_LONG, empreinte=dedupe.empreinte_contenu(TEXTE_LONG + url),
        droits_collecte="test",
    )
    repo.inserer_evidence(engine, opportunity_id=opportunity_id, source_id=source_id, claim=claim,
                          type_="non_verifie", independant=True)
    return source_id


def _budget(engine) -> BudgetTracker:
    return BudgetTracker(
        engine, "run-test", plafond_eur=25.0, plafond_appels_approfondis=1000,
        plafond_requetes_recherche_par_jour=600, plafond_fetchs_pages_par_jour=400,
    )


def test_le_quatrieme_dossier_ne_recoit_pas_une_source_deja_rattachee_a_trois(engine_test, monkeypatch):
    for _ in range(3):
        _rattacher(engine_test, _dossier(engine_test), URL_PARTAGEE)
    quatrieme = _dossier(engine_test)
    appels: list = []
    _servir(monkeypatch, _page_html(TEXTE_LONG), appels)
    budget = _budget(engine_test)

    ids = collecter_preuves(engine_test, [_resultat(URL_PARTAGEE)], opportunity_id=quatrieme, budget=budget)

    assert ids == []
    assert appels == []  # jamais refetchée : aucun fetch, aucun compteur consommé
    assert repo.sources_deja_citees(engine_test, quatrieme) == set()
    jour = datetime.now(timezone.utc).date()
    assert repo.nombre_evenements_role_jour_utc(engine_test, jour, role="enqueteur_fetch") == 0


def test_le_troisieme_dossier_recoit_encore_la_source(engine_test, monkeypatch):
    for _ in range(2):
        _rattacher(engine_test, _dossier(engine_test), URL_PARTAGEE)
    troisieme = _dossier(engine_test)
    _servir(monkeypatch, _page_html(TEXTE_LONG))

    ids = collecter_preuves(engine_test, [_resultat(URL_PARTAGEE)], opportunity_id=troisieme)

    assert len(ids) == 1
    assert repo.sources_deja_citees(engine_test, troisieme) == set(ids)


def test_le_signal_d_origine_ne_compte_pas_dans_le_plafond(engine_test, monkeypatch):
    """Une page qui est le signal d'origine de trois dossiers (preuve
    « Scout: … ») reste rattachable à trois autres en tant que preuve."""
    for _ in range(3):
        _rattacher(engine_test, _dossier(engine_test), URL_PARTAGEE, claim="Scout: un signal d'origine")
    nouveau = _dossier(engine_test)
    _servir(monkeypatch, _page_html(TEXTE_LONG))

    ids = collecter_preuves(engine_test, [_resultat(URL_PARTAGEE)], opportunity_id=nouveau)

    assert len(ids) == 1


def test_un_dossier_qui_porte_deja_la_source_la_garde_meme_au_plafond(engine_test):
    """Reprise d'une enquête : le dossier déjà rattaché n'est jamais privé de
    sa propre source par le plafond."""
    dossiers = [_dossier(engine_test) for _ in range(3)]
    for d in dossiers:
        _rattacher(engine_test, d, URL_PARTAGEE)

    assert fetch_module._source_saturee(engine_test, URL_PARTAGEE, dossiers[0], 3) is False
    assert fetch_module._source_saturee(engine_test, URL_PARTAGEE, _dossier(engine_test), 3) is True


def test_une_source_saturee_ne_prend_pas_la_place_d_une_autre_page(engine_test, monkeypatch):
    """La source saturée est écartée AVANT la sélection : elle ne consomme pas
    une des places de `max_resultats`."""
    for _ in range(3):
        _rattacher(engine_test, _dossier(engine_test), URL_PARTAGEE)
    nouveau = _dossier(engine_test)
    _servir(monkeypatch, _page_html(TEXTE_LONG))

    ids = collecter_preuves(
        engine_test, [_resultat(URL_PARTAGEE), _resultat("https://autre.example/page")],
        max_resultats=1, opportunity_id=nouveau,
    )

    assert len(ids) == 1
    with engine_test.connect() as cx:
        url = cx.execute(select(sources.c.url_canonique).where(sources.c.id == ids[0])).scalar_one()
    assert url == "https://autre.example/page"


def test_sans_opportunity_id_le_plafond_ne_s_applique_pas(engine_test, monkeypatch):
    for _ in range(3):
        _rattacher(engine_test, _dossier(engine_test), URL_PARTAGEE)
    _servir(monkeypatch, _page_html(TEXTE_LONG))

    assert len(collecter_preuves(engine_test, [_resultat(URL_PARTAGEE)])) == 1


# ------------------------------------------- seuil du magasin interne -----

EXTRAIT_NVIDIA = (
    "Nvidia launches new platform for reining in rogue AI agents — Nvidia CEO Jensen Huang on Monday introduced a "
    "toolkit of software and hardware products that add independent security layers around AI agents to ensure they "
    "stay within their test environments even if they attempt to break out."
)


def _inserer_signal_concurrence(engine, url, extrait):
    repo.upsert_source(
        engine, url_canonique=url, domaine="TechCrunch", date_publication=None, type_source="rss", extrait=extrait,
        empreinte=dedupe.empreinte_contenu(extrait), droits_collecte="test", etiquette="signal_concurrence",
    )


def test_seuil_du_magasin_interne_calibre_sur_le_rapport_du_29_09(engine_test):
    """Requêtes RÉELLES du rapport 3.16 contre l'extrait RÉEL (le plus court
    des trois stockés) de l'article Nvidia : les dossiers sur le sujet (surveillance d'agents IA) restent
    au-dessus du seuil ; les dossiers sans rapport (génération de code, forage
    pétrolier) tombent en dessous. Avant 3.17 (seuil 0,15) tous passaient."""
    _inserer_signal_concurrence(engine_test, "https://techcrunch.com/nvidia", EXTRAIT_NVIDIA)
    fournisseur = FournisseurMagasinInterne(engine_test)
    assert fournisseur.seuil == 0.21

    sur_le_sujet = ["AI agent monitoring oversight security software", "AI agent security sandboxing isolation software"]
    sans_rapport = [
        "AI code generation software development automation software",
        "AI well optimization drilling efficiency software",
        "AI training costs infrastructure optimization software",
        "AI hype bubble vendor claims evaluation software",
    ]
    for requete in sur_le_sujet:
        assert len(fournisseur.rechercher(requete, limite=5)) == 1, requete
    for requete in sans_rapport:
        assert fournisseur.rechercher(requete, limite=5) == [], requete


def test_seuils_et_plafonds_de_la_sous_etape_3_17(_vrais_quotas):
    assert _vrais_quotas["seuil_similarite_magasin_interne"] == 0.21
    assert _vrais_quotas["max_dossiers_par_source"] == 3
    assert _vrais_quotas["enqueteur_page_longueur_min_caracteres"] == 300
    assert _vrais_quotas["echantillon_rejetes_pour_controle"] == 0.02
    assert _vrais_quotas["max_tirages_controle_par_jour"] == 5
