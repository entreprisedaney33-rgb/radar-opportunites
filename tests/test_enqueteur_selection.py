"""Sous-étape 3.3 (AMELIORATIONS.md) : `selectionner_resultats` — fonction
pure, aucun réseau. « max 8 par opportunité, priorité aux résultats les plus
récents et aux domaines non encore représentés »."""
from __future__ import annotations

from datetime import datetime, timezone

from app.enqueteur.fournisseurs import ResultatRecherche
from app.enqueteur.selection import selectionner_resultats


def _resultat(url, *, jours_avant=0, fournisseur="test", requete_origine=None):
    horodatage = None
    if jours_avant is not None:
        horodatage = datetime(2026, 9, 25, tzinfo=timezone.utc).replace(day=25 - jours_avant)
    return ResultatRecherche(
        url=url, titre=f"Titre {url}", extrait=f"Extrait {url}",
        horodatage_source=horodatage, fournisseur=fournisseur, requete_origine=requete_origine,
    )


def test_respecte_max_resultats():
    resultats = [_resultat(f"https://a.example/{i}", jours_avant=i) for i in range(20)]
    selection = selectionner_resultats(resultats, max_resultats=8)
    assert len(selection) == 8


def test_renvoie_tout_si_moins_que_le_maximum():
    resultats = [_resultat(f"https://a.example/{i}", jours_avant=i) for i in range(3)]
    selection = selectionner_resultats(resultats, max_resultats=8)
    assert len(selection) == 3


def test_priorite_a_la_diversite_de_domaine_puis_a_la_recence():
    # 3 domaines, chacun avec plusieurs résultats à des dates différentes.
    resultats = [
        _resultat("https://a.example/vieux", jours_avant=10, fournisseur="a"),
        _resultat("https://a.example/recent", jours_avant=1, fournisseur="a"),
        _resultat("https://b.example/moyen", jours_avant=5, fournisseur="b"),
        _resultat("https://c.example/tres_vieux", jours_avant=20, fournisseur="c"),
    ]
    selection = selectionner_resultats(resultats, max_resultats=3)
    urls = [r.url for r in selection]
    # Un par domaine : le plus récent de chaque (jamais "vieux" pour a.example,
    # puisque "recent" du même domaine est disponible).
    assert urls == [
        "https://a.example/recent",
        "https://b.example/moyen",
        "https://c.example/tres_vieux",
    ]


def test_remplit_avec_le_reste_par_recence_quand_moins_de_domaines_que_le_max():
    resultats = [
        _resultat("https://a.example/1", jours_avant=1, fournisseur="a"),
        _resultat("https://a.example/2", jours_avant=2, fournisseur="a"),
        _resultat("https://a.example/3", jours_avant=3, fournisseur="a"),
        _resultat("https://b.example/1", jours_avant=5, fournisseur="b"),
    ]
    selection = selectionner_resultats(resultats, max_resultats=3)
    urls = {r.url for r in selection}
    # 1 par domaine d'abord (a/1, b/1), puis le reste par récence (a/2 avant a/3).
    assert urls == {"https://a.example/1", "https://b.example/1", "https://a.example/2"}
    # Résultat final toujours trié par récence décroissante (a/1 : 1j, a/2 : 2j, b/1 : 5j).
    assert [r.url for r in selection] == ["https://a.example/1", "https://a.example/2", "https://b.example/1"]


def test_dedoublonne_par_url_canonique():
    resultats = [
        _resultat("https://a.example/page?utm_source=x", jours_avant=1),
        _resultat("https://a.example/page?utm_campaign=y", jours_avant=2),
    ]
    selection = selectionner_resultats(resultats, max_resultats=8)
    assert len(selection) == 1


def test_resultat_sans_horodatage_traite_comme_le_plus_ancien():
    resultats = [
        _resultat("https://a.example/sans-date", jours_avant=None, fournisseur="a"),
        _resultat("https://a.example/avec-date", jours_avant=1, fournisseur="a"),
    ]
    selection = selectionner_resultats(resultats, max_resultats=1)
    assert selection[0].url == "https://a.example/avec-date"


def test_liste_vide():
    assert selectionner_resultats([], max_resultats=8) == []
