"""Sous-étape 3.4 (AMELIORATIONS.md) : orchestration de l'Enquêteur pour UNE
opportunité (`app.enqueteur.enqueteur`), qui relie gabarits (3.1),
fournisseurs (3.1/3.2) et fetch/stockage (3.3) -- jusque-là autonomes -- sans
aucun réseau (fournisseur simulé, `app.enqueteur.fournisseurs.FournisseurRechercheSimule`).

Sous-étape 3.4b : identification de concurrents par du code
(`app.enqueteur.concurrents`) et enquête de prix qui en découle
(`app.enqueteur.enqueteur._enqueter_prix`) -- voir en bas de fichier."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine

from app.adapters.http import ErreurCollecte
from app.enqueteur import enqueteur as enqueteur_module
from app.enqueteur import fetch as fetch_module
from app.enqueteur.concurrents import Concurrent
from app.enqueteur.enqueteur import _enqueter_prix, enqueter_opportunite
from app.enqueteur.fetch import ETIQUETTE_PREUVE_ENQUETE, ETIQUETTE_PREUVE_PRIX
from app.enqueteur.fournisseurs import (
    DefinitionFournisseur,
    FournisseurRechercheSimule,
    RegistreFournisseurs,
    ResultatRecherche,
)
from app.enqueteur.gabarits import HypotheseEnqueteur
from app.pipeline.budget import BudgetTracker
from app.storage import repo
from app.storage.db import migrer

QUOTAS = {
    "enqueteur_resultats_par_requete": 5,
    "max_resultats_enquete_par_opportunite": 8,
    "enqueteur_fetch_delai_secondes": 0,
}

HYPOTHESE = HypotheseEnqueteur(
    acheteur="PME e-commerce", douleur="rapprochement bancaire manuel", mecanisme="agent de rapprochement",
)


@pytest.fixture
def engine_test(tmp_path):
    chemin = tmp_path / "test.db"
    moteur = create_engine(f"sqlite:///{chemin}", future=True, connect_args={"check_same_thread": False})
    migrer(moteur)
    return moteur


@pytest.fixture
def budget(engine_test):
    return BudgetTracker(
        engine_test, "run-test", plafond_eur=25.0, plafond_appels_approfondis=1000,
        plafond_requetes_recherche_par_jour=600, plafond_fetchs_pages_par_jour=400,
    )


def _resultat_fixe(fournisseur: str, suffixe: str, jour: int = 20) -> ResultatRecherche:
    return ResultatRecherche(
        url=f"https://{fournisseur}.example/{suffixe}", titre=f"Résultat {suffixe}",
        extrait=f"Extrait {suffixe} du fournisseur {fournisseur}.",
        horodatage_source=datetime(2026, 9, jour, tzinfo=timezone.utc), fournisseur=fournisseur,
    )


def _registre_deux_fournisseurs_simules() -> RegistreFournisseurs:
    """Deux fournisseurs simulés, chacun renvoyant un résultat FIXE (pas
    dérivé de la requête) -- pour compter précisément combien de fois
    `rechercher` est appelé, sur quelles requêtes."""
    appels: list[tuple[str, str]] = []

    def _fabrique(nom: str):
        def _fournisseur():
            class _F:
                pass

            f = _F()
            f.nom = nom

            def rechercher(requete, limite):
                appels.append((nom, requete))
                return [_resultat_fixe(nom, requete.replace(" ", "_"))]

            f.rechercher = rechercher
            return f

        return _fournisseur

    registre = RegistreFournisseurs()
    registre.enregistrer(DefinitionFournisseur(nom="fournisseur_a", fabrique=_fabrique("fournisseur_a")))
    registre.enregistrer(DefinitionFournisseur(nom="fournisseur_b", fabrique=_fabrique("fournisseur_b")))
    return registre, appels


# ---------------------------------------------- _rechercher_toutes_familles --

def test_utilise_uniquement_les_familles_demande_et_concurrence(engine_test, budget):
    """Sous-étape 3.4 : la famille `prix` n'est jamais utilisée (aucun
    mécanisme d'identification de concurrents dans le périmètre écrit de
    cette sous-étape, voir la docstring du module et le Journal)."""
    registre, appels = _registre_deux_fournisseurs_simules()

    resultats = enqueteur_module._rechercher_toutes_familles(
        HYPOTHESE, registre=registre, budget=budget, limite_par_requete=5,
    )

    requetes = {requete for _fournisseur, requete in appels}
    # gabarits.yaml : 2 requêtes "demande" + 3 "concurrence" = 5, jamais de "pricing".
    assert len(requetes) == 5
    assert not any("pricing" in r for r in requetes)
    assert len(appels) == 5 * 2  # 5 requêtes x 2 fournisseurs
    assert len(resultats) == 5 * 2


def test_chaque_resultat_porte_la_requete_qui_l_a_trouve(engine_test, budget):
    registre, _appels = _registre_deux_fournisseurs_simules()
    resultats = enqueteur_module._rechercher_toutes_familles(
        HYPOTHESE, registre=registre, budget=budget, limite_par_requete=5,
    )
    for resultat in resultats:
        assert resultat.requete_origine is not None
        # bâti à partir de la même requête, voir _resultat_fixe/_fabrique ci-dessus.
        assert resultat.requete_origine.replace(" ", "_") in resultat.url


def test_fournisseur_en_panne_n_arrete_pas_les_autres(engine_test, budget):
    registre = RegistreFournisseurs()

    class _FournisseurCasse:
        nom = "casse"

        def rechercher(self, requete, limite):
            raise RuntimeError("panne simulée")

    registre.enregistrer(DefinitionFournisseur(nom="casse", fabrique=_FournisseurCasse))
    registre.enregistrer(
        DefinitionFournisseur(
            nom="simule",
            fabrique=lambda: FournisseurRechercheSimule(resultats=[_resultat_fixe("simule", "x")]),
        )
    )

    resultats = enqueteur_module._rechercher_toutes_familles(
        HYPOTHESE, registre=registre, budget=budget, limite_par_requete=5,
    )
    # 5 requêtes (demande + concurrence), une par le fournisseur "simule"
    # seulement (le fournisseur "casse" ne produit jamais de résultat).
    assert len(resultats) == 5
    assert all(r.fournisseur == "simule" for r in resultats)


def test_plafond_de_requetes_de_recherche_arrete_proprement(engine_test):
    """Le plafond de requêtes/jour (posé en 3.1) atteint arrête la recherche
    pour cette opportunité, sans jamais lever d'exception -- les résultats
    déjà trouvés avant le plafond restent utilisables."""
    budget_serre = BudgetTracker(
        engine_test, "run-test", plafond_eur=25.0, plafond_appels_approfondis=1000,
        plafond_requetes_recherche_par_jour=3, plafond_fetchs_pages_par_jour=400,
    )
    registre, appels = _registre_deux_fournisseurs_simules()

    resultats = enqueteur_module._rechercher_toutes_familles(
        HYPOTHESE, registre=registre, budget=budget_serre, limite_par_requete=5,
    )

    assert len(appels) == 3  # arrêté net au plafond (pas 10)
    assert len(resultats) == 3


class _FournisseurSansReseauSimule:
    """Double d'un fournisseur qui déclare `sans_reseau = True` (le magasin
    interne réel, `app.enqueteur.fournisseurs_gratuits.FournisseurMagasinInterne`,
    en a un — sous-étape 3.6)."""

    nom = "magasin_interne"
    sans_reseau = True

    def rechercher(self, requete, limite):
        return [_resultat_fixe(self.nom, requete.replace(" ", "_"))]


def test_fournisseur_sans_reseau_ne_consomme_pas_le_plafond_requetes_recherche(engine_test, budget):
    """Sous-étape 3.6, point 1 : le magasin interne ne fait aucun appel
    réseau -- il ne doit jamais engager ni journaliser
    `max_requetes_recherche_par_jour`, un plafond pensé pour protéger des
    services tiers (Algolia HN, Reddit)."""
    registre = RegistreFournisseurs()
    registre.enregistrer(DefinitionFournisseur(nom="magasin_interne", fabrique=_FournisseurSansReseauSimule))

    resultats = enqueteur_module._rechercher_toutes_familles(
        HYPOTHESE, registre=registre, budget=budget, limite_par_requete=5,
    )

    assert len(resultats) == 5  # 2 "demande" + 3 "concurrence", comme d'habitude
    assert budget.requetes_recherche_jour_engagees() == 0


def test_fournisseur_sans_reseau_continue_meme_le_plafond_reseau_deja_atteint(engine_test):
    """Un plafond de requêtes réseau à 0 (déjà atteint) n'a aucune raison
    d'arrêter un fournisseur qui ne consomme jamais ce compteur."""
    budget_sans_marge = BudgetTracker(
        engine_test, "run-test", plafond_eur=25.0, plafond_appels_approfondis=1000,
        plafond_requetes_recherche_par_jour=0, plafond_fetchs_pages_par_jour=400,
    )
    registre = RegistreFournisseurs()
    registre.enregistrer(DefinitionFournisseur(nom="magasin_interne", fabrique=_FournisseurSansReseauSimule))

    resultats = enqueteur_module._rechercher_toutes_familles(
        HYPOTHESE, registre=registre, budget=budget_sans_marge, limite_par_requete=5,
    )

    assert len(resultats) == 5
    assert budget_sans_marge.requetes_recherche_jour_engagees() == 0


# --------------------------------------------------------- enqueter_opportunite

def test_enqueter_opportunite_rattache_les_sources_trouvees(engine_test, budget, monkeypatch):
    opp_id = repo.creer_opportunite(
        engine_test, titre="t", acheteur=HYPOTHESE.acheteur, probleme=HYPOTHESE.douleur,
        mecanisme_ia=HYPOTHESE.mecanisme, secteur="e_commerce", statut="nouveau", cluster_id=None,
    )
    registre, _appels = _registre_deux_fournisseurs_simules()
    # Aucun réseau : le fetch de chaque page trouvée par les fournisseurs
    # simulés ci-dessus est lui aussi simulé (même pattern que
    # tests/test_enqueteur_fetch.py).
    monkeypatch.setattr(fetch_module, "get_with_retry", lambda url, **kw: type("R", (), {"text": "", "status_code": 404})())
    monkeypatch.setattr(
        fetch_module, "get_avec_limite_taille",
        lambda url, **kw: b"<html><body><p>Contenu suffisant pour ne pas etre vide.</p></body></html>",
    )
    monkeypatch.setattr(fetch_module.time, "sleep", lambda *_a, **_kw: None)

    source_ids = enqueter_opportunite(
        engine_test, opp_id, HYPOTHESE, registre=registre, budget=budget, quotas=QUOTAS,
    )

    assert len(source_ids) > 0
    citees = repo.sources_deja_citees(engine_test, opp_id)
    assert citees == set(source_ids)


def test_enqueter_opportunite_sans_resultat_ne_touche_pas_au_fetch(engine_test, budget):
    """Si aucun fournisseur actif ne renvoie de résultat, `collecter_preuves`
    n'est même pas appelée -- aucun compteur de fetch consommé."""
    registre = RegistreFournisseurs()
    registre.enregistrer(
        DefinitionFournisseur(nom="vide", fabrique=lambda: FournisseurRechercheSimule(resultats=[]))
    )
    opp_id = repo.creer_opportunite(
        engine_test, titre="t", acheteur="a", probleme="p", mecanisme_ia="m",
        secteur="e_commerce", statut="nouveau", cluster_id=None,
    )

    source_ids = enqueter_opportunite(
        engine_test, opp_id, HYPOTHESE, registre=registre, budget=budget, quotas=QUOTAS,
    )

    assert source_ids == []
    assert budget.fetchs_pages_jour_engages() == 0


# ---------------------------------------------------------- sous-étape 3.4b --
# Identification des concurrents par du code + enquête de la famille `prix`.

def _creer_opportunite(engine_test) -> str:
    return repo.creer_opportunite(
        engine_test, titre="t", acheteur=HYPOTHESE.acheteur, probleme=HYPOTHESE.douleur,
        mecanisme_ia=HYPOTHESE.mecanisme, secteur="e_commerce", statut="nouveau", cluster_id=None,
    )


def test_sans_concurrent_identifie_aucune_enquete_prix_n_est_declenchee(engine_test, budget, monkeypatch):
    """Ni le fournisseur magasin interne ni un marqueur d'offre dans un titre
    `concurrence` : aucun concurrent identifié, `_enqueter_prix` jamais
    appelée -- exactement les 5 requêtes demande+concurrence (2+3, voir
    gabarits.yaml), jamais de requête `pricing`/`tarifs` en plus."""

    class _FournisseurNeutre:
        nom = "fournisseur_neutre"

        def rechercher(self, requete, limite):
            return [
                ResultatRecherche(
                    url="https://neutre.example/article",
                    titre="Un article neutre sur le sujet, sans rapport commercial",
                    extrait="Rien de commercial ici.",
                    horodatage_source=None,
                    fournisseur=self.nom,
                )
            ]

    registre = RegistreFournisseurs()
    registre.enregistrer(DefinitionFournisseur(nom="fournisseur_neutre", fabrique=_FournisseurNeutre))
    opp_id = _creer_opportunite(engine_test)
    monkeypatch.setattr(fetch_module, "get_with_retry", lambda url, **kw: type("R", (), {"text": "", "status_code": 404})())
    monkeypatch.setattr(
        fetch_module, "get_avec_limite_taille",
        lambda url, **kw: b"<html><body><p>Contenu suffisant pour ne pas etre vide.</p></body></html>",
    )
    monkeypatch.setattr(fetch_module.time, "sleep", lambda *_a, **_kw: None)

    enqueter_opportunite(engine_test, opp_id, HYPOTHESE, registre=registre, budget=budget, quotas=QUOTAS)

    assert budget.requetes_recherche_jour_engagees() == 5  # 2 "demande" + 3 "concurrence", jamais de "prix"


def test_concurrent_identifie_via_magasin_interne_declenche_l_enquete_prix(engine_test, budget, monkeypatch):
    """Point (a) du texte de 3.4b : un résultat du fournisseur `magasin_interne`
    identifie un concurrent, qui déclenche la famille `prix` -- les pages
    obtenues sont étiquetées `prix`, jamais `preuve_enquete`."""

    class _FournisseurMagasinInterneSimule:
        nom = "magasin_interne"

        def rechercher(self, requete, limite):
            return [
                ResultatRecherche(
                    url="https://concurrent-simule.example/produit",
                    titre="ConcurrentSimulé",
                    extrait="Un concurrent déjà connu du magasin de preuves.",
                    horodatage_source=datetime(2026, 9, 20, tzinfo=timezone.utc),
                    fournisseur=self.nom,
                )
            ]

    registre = RegistreFournisseurs()
    registre.enregistrer(DefinitionFournisseur(nom="magasin_interne", fabrique=_FournisseurMagasinInterneSimule))
    opp_id = _creer_opportunite(engine_test)
    monkeypatch.setattr(fetch_module, "get_with_retry", lambda url, **kw: type("R", (), {"text": "", "status_code": 404})())
    monkeypatch.setattr(
        fetch_module, "get_avec_limite_taille",
        lambda url, **kw: b"<html><body><p>Page de tarification suffisante.</p></body></html>",
    )
    monkeypatch.setattr(fetch_module.time, "sleep", lambda *_a, **_kw: None)

    source_ids = enqueter_opportunite(engine_test, opp_id, HYPOTHESE, registre=registre, budget=budget, quotas=QUOTAS)

    assert source_ids
    from sqlalchemy import select

    from app.storage.schema import sources as sources_table

    with engine_test.connect() as cx:
        rows = cx.execute(select(sources_table).where(sources_table.c.id.in_(set(source_ids)))).mappings().all()
    etiquettes = {row["etiquette"] for row in rows}
    assert ETIQUETTE_PREUVE_PRIX in etiquettes
    assert ETIQUETTE_PREUVE_ENQUETE in etiquettes  # le résultat "produit" initial, lui, reste une preuve d'enquête normale
    assert any(row["url_canonique"] == "https://concurrent-simule.example/pricing" for row in rows)
    # rattachées à l'opportunité comme n'importe quelle autre preuve de l'Enquêteur.
    citees = repo.sources_deja_citees(engine_test, opp_id)
    assert citees == set(source_ids)


def test_enqueter_prix_genere_pricing_et_tarifs_par_concurrent(engine_test, budget, monkeypatch):
    """Les requêtes de la famille `prix` (`app/enqueteur/gabarits.yaml`) sont
    bien générées pour chaque concurrent, via les fournisseurs actifs."""
    appels: list[str] = []

    class _FournisseurPrix:
        nom = "fournisseur_prix"

        def rechercher(self, requete, limite):
            appels.append(requete)
            return []

    registre = RegistreFournisseurs()
    registre.enregistrer(DefinitionFournisseur(nom="fournisseur_prix", fabrique=_FournisseurPrix))
    opp_id = _creer_opportunite(engine_test)
    monkeypatch.setattr(fetch_module, "get_with_retry", lambda url, **kw: type("R", (), {"text": "", "status_code": 404})())
    monkeypatch.setattr(fetch_module, "get_avec_limite_taille", lambda url, **kw: (_ for _ in ()).throw(ErreurCollecte("404")))
    monkeypatch.setattr(fetch_module.time, "sleep", lambda *_a, **_kw: None)

    _enqueter_prix(
        engine_test, opp_id, HYPOTHESE,
        [Concurrent(nom="Alpha", domaine="alpha.example"), Concurrent(nom="Beta", domaine="beta.example")],
        registre=registre, budget=budget, quotas=QUOTAS,
    )

    assert appels == ["Alpha pricing", "Alpha tarifs", "Beta pricing", "Beta tarifs"]


def test_enqueter_prix_respecte_robots_txt_sur_le_fetch_direct(engine_test, budget, monkeypatch):
    """Une page `<domaine>/pricing` interdite par robots.txt n'est jamais
    stockée -- même garde-fou que pour n'importe quelle autre page
    (`app.enqueteur.fetch.recuperer_page`)."""
    registre = RegistreFournisseurs()  # aucun fournisseur actif : seul le fetch direct est en jeu
    opp_id = _creer_opportunite(engine_test)
    monkeypatch.setattr(
        fetch_module, "get_with_retry",
        lambda url, **kw: type("R", (), {"text": "User-agent: *\nDisallow: /", "status_code": 200})(),
    )
    appele = {"n": 0}

    def _jamais_appele(url, **kw):
        appele["n"] += 1
        return b"<html></html>"

    monkeypatch.setattr(fetch_module, "get_avec_limite_taille", _jamais_appele)
    monkeypatch.setattr(fetch_module.time, "sleep", lambda *_a, **_kw: None)

    source_ids = _enqueter_prix(
        engine_test, opp_id, HYPOTHESE, [Concurrent(nom="ConcurrentInterdit", domaine="interdit.example")],
        registre=registre, budget=budget, quotas=QUOTAS,
    )

    assert source_ids == []
    assert appele["n"] == 0  # robots.txt refuse : jamais fetché, donc jamais stocké
