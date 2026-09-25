import pytest

from app.enqueteur.fournisseurs import (
    DefinitionFournisseur,
    FournisseurRechercheSimule,
    RegistreFournisseurs,
    ResultatRecherche,
)


def test_fournisseur_simule_fabrique_des_resultats_a_partir_de_la_requete():
    fournisseur = FournisseurRechercheSimule()
    resultats = fournisseur.rechercher("des heures par semaine", limite=3)
    assert len(resultats) == 3
    assert all("des heures par semaine" in r.titre for r in resultats)
    assert all(r.fournisseur == "simule" for r in resultats)


def test_fournisseur_simule_respecte_la_limite_meme_avec_des_resultats_fournis():
    resultats_fixes = [
        ResultatRecherche(url="https://exemple.invalid/1", titre="a", extrait="a", horodatage_source=None, fournisseur="simule"),
        ResultatRecherche(url="https://exemple.invalid/2", titre="b", extrait="b", horodatage_source=None, fournisseur="simule"),
    ]
    fournisseur = FournisseurRechercheSimule(resultats=resultats_fixes)
    assert fournisseur.rechercher("peu importe", limite=1) == resultats_fixes[:1]


def test_registre_enregistrer_deux_fois_le_meme_nom_leve_une_erreur():
    registre = RegistreFournisseurs()
    registre.enregistrer(DefinitionFournisseur(nom="simule", fabrique=FournisseurRechercheSimule))
    with pytest.raises(ValueError):
        registre.enregistrer(DefinitionFournisseur(nom="simule", fabrique=FournisseurRechercheSimule))


def test_fournisseur_actif_par_defaut_est_actif_sans_variable_environnement(monkeypatch):
    monkeypatch.delenv("RADAR_ENQUETEUR_ACTIF_SIMULE", raising=False)
    registre = RegistreFournisseurs()
    registre.enregistrer(DefinitionFournisseur(nom="simule", fabrique=FournisseurRechercheSimule, actif_par_defaut=True))
    assert registre.est_actif("simule") is True
    assert len(registre.fournisseurs_actifs()) == 1


def test_fournisseur_actif_par_defaut_desactivable_par_variable_environnement(monkeypatch):
    monkeypatch.setenv("RADAR_ENQUETEUR_ACTIF_SIMULE", "0")
    registre = RegistreFournisseurs()
    registre.enregistrer(DefinitionFournisseur(nom="simule", fabrique=FournisseurRechercheSimule, actif_par_defaut=True))
    assert registre.est_actif("simule") is False
    assert registre.fournisseurs_actifs() == []


def test_fournisseur_desactive_par_defaut_reste_inactif_sans_activation_explicite(monkeypatch):
    """Même mécanisme que le futur fournisseur web payant (sous-étape 3.5) :
    `actif_par_defaut=False` doit rester désactivé tant que rien ne l'active
    explicitement."""
    monkeypatch.delenv("RADAR_ENQUETEUR_ACTIF_PAYANT", raising=False)
    registre = RegistreFournisseurs()
    registre.enregistrer(DefinitionFournisseur(nom="payant", fabrique=FournisseurRechercheSimule, actif_par_defaut=False))
    assert registre.est_actif("payant") is False
    assert registre.fournisseurs_actifs() == []


def test_fournisseur_desactive_par_defaut_activable_par_variable_environnement(monkeypatch):
    monkeypatch.setenv("RADAR_ENQUETEUR_ACTIF_PAYANT", "true")
    registre = RegistreFournisseurs()
    registre.enregistrer(DefinitionFournisseur(nom="payant", fabrique=FournisseurRechercheSimule, actif_par_defaut=False))
    assert registre.est_actif("payant") is True
    assert len(registre.fournisseurs_actifs()) == 1
