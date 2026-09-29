"""Sous-étape 3.17 (AMELIORATIONS.md), sans réseau : infos de sources pour le
score, tirage de contrôle (2 %, plafonné à 5 par jour), domaines de presse
exclus des concurrents. (Point 3, température 0 : impossible, l'API la refuse
sur Sonnet 5 -- voir le Journal de la sous-étape.)"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update

from app import config as cfg
from app.config import domaines_exclus_concurrents
from app.enqueteur.concurrents import identifier_concurrents
from app.enqueteur.fournisseurs import ResultatRecherche
from app.pipeline import orchestrator as orch
from app.storage import repo
from app.storage.schema import tirages_controle_rejetes


def _dossier(engine, statut="rejete") -> str:
    return repo.creer_opportunite(
        engine, titre="t", acheteur="a", probleme="p", mecanisme_ia="m", secteur="intersectoriel",
        statut=statut, cluster_id=None,
    )


# ----------------------------------------------- infos de sources (score) --

def test_infos_sources_du_dossier_distingue_le_signal_d_origine(engine_test):
    opp = _dossier(engine_test, "enquete_terminee")
    origine, _ = repo.upsert_source(
        engine_test, url_canonique="https://news.ycombinator.com/item?id=1", domaine="news.ycombinator.com",
        date_publication=None, type_source="rss", extrait="signal", empreinte="e1", droits_collecte="t",
    )
    autre, _ = repo.upsert_source(
        engine_test, url_canonique="https://www.exemple.test/a", domaine="www.exemple.test",
        date_publication=None, type_source="page_web", extrait="page", empreinte="e2", droits_collecte="t",
    )
    repo.inserer_evidence(engine_test, opportunity_id=opp, source_id=origine, claim="Scout: un signal",
                          type_="non_verifie", independant=True)
    repo.inserer_evidence(engine_test, opportunity_id=opp, source_id=origine, claim="Une affirmation de l'Analyst",
                          type_="observe", independant=True)
    repo.inserer_evidence(engine_test, opportunity_id=opp, source_id=autre, claim="Enquête (x) : page",
                          type_="non_verifie", independant=True)

    infos = repo.infos_sources_du_dossier(engine_test, opp)

    assert infos == {
        origine: {"domaine": "news.ycombinator.com", "origine": True},
        autre: {"domaine": "www.exemple.test", "origine": False},
    }


# ---------------------------------------------------- tirage de contrôle --

def test_nombre_de_tirages_arrondit_au_hasard_jamais_a_zero_pour_toujours():
    attendu = 15 * 0.02  # 0,3
    assert orch._nombre_de_tirages(attendu, 5, aleatoire=lambda: 0.29) == 1
    assert orch._nombre_de_tirages(attendu, 5, aleatoire=lambda: 0.31) == 0


def test_nombre_de_tirages_espérance_egale_a_la_fraction_demandee():
    import random

    rng = random.Random(2026)
    total = sum(orch._nombre_de_tirages(0.3, 100, aleatoire=rng.random) for _ in range(20000))
    assert total / 20000 == pytest.approx(0.3, abs=0.02)


def test_nombre_de_tirages_respecte_le_restant_du_plafond_journalier():
    assert orch._nombre_de_tirages(3.0, 2, aleatoire=lambda: 0.0) == 2
    assert orch._nombre_de_tirages(3.0, 0, aleatoire=lambda: 0.0) == 0
    assert orch._nombre_de_tirages(0.3, -4, aleatoire=lambda: 0.0) == 0


def _tirer_aujourdhui(engine, n: int) -> None:
    run_id = repo.creer_run(engine, mode="reel", version_code="t", version_config="t", quotas={})
    for _ in range(n):
        repo.inserer_tirage_controle_rejete(
            engine, opportunity_id=_dossier(engine, "incertain"), run_id=run_id,
            decision_avant="rejete", decision_apres="incertain",
        )


def test_le_plafond_journalier_de_tirages_bloque_tout_nouveau_tirage(engine_test):
    rejetes = [_dossier(engine_test) for _ in range(10)]
    _tirer_aujourdhui(engine_test, 5)

    selection = orch._selectionner_pour_analyse(
        engine_test, max_analyses=15, fraction_echantillon_rejetes=1.0, max_tirages_par_jour=5,
    )

    assert [o for o in selection if o.get("tirage_controle")] == []
    assert rejetes  # il y a bien des rejetés tirables : c'est le plafond qui bloque


def test_il_reste_du_plafond_journalier_le_tirage_est_borne_au_restant(engine_test):
    for _ in range(10):
        _dossier(engine_test)
    _tirer_aujourdhui(engine_test, 3)

    selection = orch._selectionner_pour_analyse(
        engine_test, max_analyses=15, fraction_echantillon_rejetes=1.0, max_tirages_par_jour=5,
    )

    assert len([o for o in selection if o.get("tirage_controle")]) == 2  # 5 - 3 déjà faits


def test_les_tirages_d_hier_ne_comptent_pas_dans_le_plafond_d_aujourd_hui(engine_test):
    for _ in range(10):
        _dossier(engine_test)
    _tirer_aujourdhui(engine_test, 5)
    hier = datetime.now(timezone.utc) - timedelta(days=1)
    with engine_test.begin() as cx:
        cx.execute(update(tirages_controle_rejetes).values(date_creation=hier))

    selection = orch._selectionner_pour_analyse(
        engine_test, max_analyses=15, fraction_echantillon_rejetes=1.0, max_tirages_par_jour=5,
    )

    assert len([o for o in selection if o.get("tirage_controle")]) == 5


def test_sans_plafond_le_comportement_historique_est_inchange(engine_test):
    for _ in range(4):
        _dossier(engine_test)
    selection = orch._selectionner_pour_analyse(engine_test, max_analyses=10, fraction_echantillon_rejetes=1.0)
    assert len([o for o in selection if o.get("tirage_controle")]) == 4


def test_la_config_reelle_ne_tire_plus_aucun_controle():
    # 3.17 avait posé 2 % / 5 par jour ; 4.1 (mode économe, 29/09) les passe à 0.
    quotas = cfg._load_yaml("quotas.yaml")
    assert quotas["echantillon_rejetes_pour_controle"] == 0.0
    assert quotas["max_tirages_controle_par_jour"] == 0


# --------------------------------------------------- domaines de presse ----

@pytest.mark.parametrize("domaine", [
    "lemonde.fr", "lesechos.fr", "nytimes.com", "theverge.com", "reuters.com", "bbc.co.uk", "arstechnica.com",
])
def test_les_domaines_de_presse_sont_exclus_des_concurrents(domaine):
    assert domaine in domaines_exclus_concurrents()


def test_un_article_de_presse_avec_un_marqueur_d_offre_n_est_jamais_un_concurrent():
    """Le faux positif réel du rapport 3.16 : « CEO of Mistral: AI is
    software. It can be controlled » (lemonde.fr, marqueur *software*)."""
    article = ResultatRecherche(
        url="https://www.lemonde.fr/en/economy/article/2026/09/24/arthur-mensch.html",
        titre="Arthur Mensch, CEO of Mistral AI: AI is software. It can be controlled",
        extrait="...", horodatage_source=None, fournisseur="algolia_hn",
    )
    assert identifier_concurrents([article], [article], domaines_exclus=domaines_exclus_concurrents()) == []
