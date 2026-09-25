import pytest

from app.enqueteur.gabarits import (
    FAMILLES_VALIDES,
    GabaritsInvalides,
    HypotheseEnqueteur,
    charger_gabarits,
    gabarits,
    generer_requetes,
)


def test_charger_le_vrai_fichier_a_les_trois_familles_non_vides():
    g = gabarits()
    assert set(g) == FAMILLES_VALIDES
    for famille in FAMILLES_VALIDES:
        assert g[famille], f"famille {famille!r} vide"


def test_famille_manquante_refusee(tmp_path):
    chemin = tmp_path / "gabarits.yaml"
    chemin.write_text("demande:\n  - '<douleur> reddit'\nconcurrence:\n  - '<douleur> tool'\n", encoding="utf-8")
    with pytest.raises(GabaritsInvalides):
        charger_gabarits(chemin)


def test_famille_inconnue_refusee(tmp_path):
    chemin = tmp_path / "gabarits.yaml"
    chemin.write_text(
        "demande:\n  - 'a'\nconcurrence:\n  - 'b'\nprix:\n  - 'c'\nautre_famille:\n  - 'd'\n",
        encoding="utf-8",
    )
    with pytest.raises(GabaritsInvalides):
        charger_gabarits(chemin)


def test_famille_liste_vide_refusee(tmp_path):
    chemin = tmp_path / "gabarits.yaml"
    chemin.write_text("demande: []\nconcurrence:\n  - 'b'\nprix:\n  - 'c'\n", encoding="utf-8")
    with pytest.raises(GabaritsInvalides):
        charger_gabarits(chemin)


def test_fichier_qui_n_est_pas_un_objet_refuse(tmp_path):
    chemin = tmp_path / "gabarits.yaml"
    chemin.write_text("- demande\n- concurrence\n", encoding="utf-8")
    with pytest.raises(GabaritsInvalides):
        charger_gabarits(chemin)


_HYPOTHESE = HypotheseEnqueteur(
    acheteur="un cabinet comptable",
    douleur="le rapprochement manuel des factures",
    mecanisme="extraction automatique des lignes de facture",
)


def test_generer_requetes_substitue_la_douleur_dans_demande_et_concurrence():
    requetes = generer_requetes(_HYPOTHESE)
    assert requetes["demande"] == [
        f"{_HYPOTHESE.douleur} reddit",
        f"{_HYPOTHESE.douleur} ask hn",
    ]
    assert requetes["concurrence"] == [
        f"{_HYPOTHESE.douleur} tool",
        f"{_HYPOTHESE.douleur} software",
        f"{_HYPOTHESE.douleur} logiciel",
    ]
    # Aucun placeholder ne doit survivre à la substitution.
    for requete in requetes["demande"] + requetes["concurrence"]:
        assert "<" not in requete and ">" not in requete


def test_generer_requetes_sans_concurrents_famille_prix_vide():
    requetes = generer_requetes(_HYPOTHESE)
    assert requetes["prix"] == []


def test_generer_requetes_avec_concurrents_une_requete_par_concurrent_et_par_gabarit():
    requetes = generer_requetes(_HYPOTHESE, concurrents=["ConcurrentA", "ConcurrentB"])
    # gabarits.yaml (depuis la sous-étape 3.4b) : 2 gabarits `prix` par concurrent.
    assert requetes["prix"] == [
        "ConcurrentA pricing", "ConcurrentA tarifs", "ConcurrentB pricing", "ConcurrentB tarifs",
    ]


def test_generer_requetes_est_pure_memes_entrees_memes_sorties():
    a = generer_requetes(_HYPOTHESE, concurrents=["ConcurrentA"])
    b = generer_requetes(_HYPOTHESE, concurrents=["ConcurrentA"])
    assert a == b


def test_generer_requetes_utilise_des_gabarits_explicitement_fournis():
    gabarits_fixture = {
        "demande": ["<douleur> forum"],
        "concurrence": ["<douleur> alternative"],
        "prix": ["<nom_concurrent> tarifs"],
    }
    requetes = generer_requetes(_HYPOTHESE, concurrents=["X"], gabarits_charges=gabarits_fixture)
    assert requetes == {
        "demande": [f"{_HYPOTHESE.douleur} forum"],
        "concurrence": [f"{_HYPOTHESE.douleur} alternative"],
        "prix": ["X tarifs"],
    }
