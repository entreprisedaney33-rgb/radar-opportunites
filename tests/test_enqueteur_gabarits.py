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
    chemin.write_text("demande:\n  - '<mots_cles> reddit'\nconcurrence:\n  - '<mots_cles> tool'\n", encoding="utf-8")
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
    mots_cles="invoice reconciliation manual",
    mecanisme="extraction automatique des lignes de facture",
)


def test_generer_requetes_substitue_les_mots_cles_dans_demande_et_concurrence():
    requetes = generer_requetes(_HYPOTHESE)
    assert requetes["demande"] == [
        f"{_HYPOTHESE.mots_cles} reddit",
        f"{_HYPOTHESE.mots_cles} ask hn",
    ]
    assert requetes["concurrence"] == [
        f"{_HYPOTHESE.mots_cles} tool",
        f"{_HYPOTHESE.mots_cles} software",
        f"{_HYPOTHESE.mots_cles} logiciel",
    ]
    # Aucun placeholder ne doit survivre à la substitution.
    for requete in requetes["demande"] + requetes["concurrence"]:
        assert "<" not in requete and ">" not in requete


def test_generer_requetes_mots_cles_vides_aucune_requete_demande_ni_concurrence():
    """Sous-étape 3.11 : garde-fou « jamais de requête vide » — un gabarit
    avec le placeholder remplacé par rien (`" reddit"`) ne doit JAMAIS être
    envoyé à un vrai fournisseur."""
    hypothese_sans_mots_cles = HypotheseEnqueteur(acheteur="un cabinet comptable", mots_cles="", mecanisme="x")
    requetes = generer_requetes(hypothese_sans_mots_cles, concurrents=["ConcurrentA"])
    assert requetes["demande"] == []
    assert requetes["concurrence"] == []
    # La famille `prix` ne dépend que des concurrents, jamais des mots-clés.
    assert requetes["prix"] == ["ConcurrentA pricing", "ConcurrentA tarifs"]


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
        "demande": ["<mots_cles> forum"],
        "concurrence": ["<mots_cles> alternative"],
        "prix": ["<nom_concurrent> tarifs"],
    }
    requetes = generer_requetes(_HYPOTHESE, concurrents=["X"], gabarits_charges=gabarits_fixture)
    assert requetes == {
        "demande": [f"{_HYPOTHESE.mots_cles} forum"],
        "concurrence": [f"{_HYPOTHESE.mots_cles} alternative"],
        "prix": ["X tarifs"],
    }
