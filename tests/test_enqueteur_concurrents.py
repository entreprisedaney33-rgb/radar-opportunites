"""Sous-étape 3.4b (AMELIORATIONS.md) : identification des concurrents par
du code (`app.enqueteur.concurrents`). Fonction pure, aucun réseau, aucun
modèle -- opère sur des `ResultatRecherche` déjà collectés (fixtures ici)."""
from __future__ import annotations

from datetime import datetime, timezone

from app.enqueteur.concurrents import Concurrent, identifier_concurrents
from app.enqueteur.fournisseurs import ResultatRecherche


def _resultat(url: str, titre: str, *, fournisseur: str = "reddit") -> ResultatRecherche:
    return ResultatRecherche(
        url=url, titre=titre, extrait=f"Extrait pour {titre}.",
        horodatage_source=datetime(2026, 9, 20, tzinfo=timezone.utc), fournisseur=fournisseur,
    )


def test_aucun_candidat_ne_donne_aucun_concurrent():
    assert identifier_concurrents([], []) == []


def test_identifie_un_concurrent_via_le_magasin_interne():
    """Point (a) du texte de 3.4b : nom = titre du résultat, domaine = URL."""
    magasin_interne = [_resultat("https://www.concurrentx.example/blog/1", "ConcurrentX", fournisseur="magasin_interne")]

    concurrents = identifier_concurrents(magasin_interne, [])

    assert concurrents == [Concurrent(nom="ConcurrentX", domaine="www.concurrentx.example")]


def test_identifie_un_concurrent_via_marqueur_d_offre_dans_le_titre_concurrence():
    """Point (b) : uniquement les résultats de la famille `concurrence` dont
    le TITRE contient un marqueur d'offre explicite."""
    resultats_concurrence = [
        _resultat("https://outilconcu.example/page", "OutilConcu, le meilleur tool pour ça"),
        _resultat("https://simple-article.example/page", "Comment gérer cette douleur sans outil"),
    ]

    concurrents = identifier_concurrents([], resultats_concurrence)

    assert concurrents == [Concurrent(nom="OutilConcu, le meilleur tool pour ça", domaine="outilconcu.example")]


def test_reconnait_chaque_marqueur_d_offre():
    marqueurs = ["tool", "software", "logiciel", "platform", "app", "SaaS"]
    resultats_concurrence = [
        _resultat(f"https://exemple{i}.example/x", f"Un {marqueur} formidable")
        for i, marqueur in enumerate(marqueurs)
    ]

    concurrents = identifier_concurrents([], resultats_concurrence)

    assert len(concurrents) == 3  # tronqué à MAX_CONCURRENTS, voir test dédié
    domaines = {c.domaine for c in concurrents}
    assert domaines == {"exemple0.example", "exemple1.example", "exemple2.example"}


def test_dedoublonne_par_domaine_entre_les_deux_sources():
    """Le même domaine trouvé par les deux sources ne compte qu'une fois --
    la première occurrence (magasin interne, point (a)) est conservée."""
    magasin_interne = [_resultat("https://meme-domaine.example/a", "Nom du magasin interne", fournisseur="magasin_interne")]
    resultats_concurrence = [_resultat("https://meme-domaine.example/pricing", "Meme Domaine, un vrai software")]

    concurrents = identifier_concurrents(magasin_interne, resultats_concurrence)

    assert concurrents == [Concurrent(nom="Nom du magasin interne", domaine="meme-domaine.example")]


def test_plafonne_a_trois_concurrents():
    magasin_interne = [
        _resultat(f"https://interne{i}.example/x", f"Interne {i}", fournisseur="magasin_interne") for i in range(5)
    ]

    concurrents = identifier_concurrents(magasin_interne, [])

    assert len(concurrents) == 3


def test_ignore_un_resultat_sans_titre():
    magasin_interne = [_resultat("https://sans-titre.example/x", "   ", fournisseur="magasin_interne")]
    assert identifier_concurrents(magasin_interne, []) == []


def test_ignore_un_resultat_sans_domaine_exploitable():
    magasin_interne = [_resultat("pas-une-url-valide", "Un nom quand même", fournisseur="magasin_interne")]
    assert identifier_concurrents(magasin_interne, []) == []


def test_identification_est_pure_memes_entrees_memes_sorties():
    magasin_interne = [_resultat("https://a.example/x", "A", fournisseur="magasin_interne")]
    resultats_concurrence = [_resultat("https://b.example/x", "B, un vrai tool")]

    premier = identifier_concurrents(magasin_interne, resultats_concurrence)
    second = identifier_concurrents(magasin_interne, resultats_concurrence)

    assert premier == second
