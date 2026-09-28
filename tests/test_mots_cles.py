"""Sous-étape 3.11 : `app.pipeline.mots_cles` — validation stricte de la
proposition du Scout (`valider_mots_cles`) et repli dérivé par du code pour
une opportunité sans mots-clés utilisables (`deriver_mots_cles_repli`). Deux
fonctions PURES, aucun réseau ni base ici."""
from __future__ import annotations

from app.pipeline.mots_cles import deriver_mots_cles_repli, valider_mots_cles

# ------------------------------------------------------ valider_mots_cles --

def test_valide_trois_a_six_mots_lettres_uniquement():
    assert valider_mots_cles("invoice reconciliation manual process") == "invoice reconciliation manual process"


def test_valide_avec_chiffres():
    assert valider_mots_cles("csv export 2fa setup issue") == "csv export 2fa setup issue"


def test_valide_normalise_espaces_multiples_et_bords():
    assert valider_mots_cles("  invoice   reconciliation  manual  ") == "invoice reconciliation manual"


def test_none_si_absent():
    assert valider_mots_cles(None) is None


def test_none_si_vide_ou_espaces_seuls():
    assert valider_mots_cles("") is None
    assert valider_mots_cles("   ") is None


def test_none_si_moins_de_trois_mots():
    assert valider_mots_cles("invoice reconciliation") is None
    assert valider_mots_cles("invoice") is None


def test_none_si_plus_de_six_mots():
    assert valider_mots_cles("un deux trois quatre cinq six sept") is None


def test_none_si_un_mot_contient_une_ponctuation():
    # Apostrophe, tiret, guillemet, opérateur de recherche — tout ce qui
    # n'est pas lettre/chiffre/espace fait échouer TOUTE la proposition.
    assert valider_mots_cles("l'export des factures groupees") is None
    assert valider_mots_cles("export-facture manuel processus") is None
    assert valider_mots_cles('"export facture" manuel bloque') is None
    assert valider_mots_cles("site:reddit.com export facture bloque") is None


def test_valide_avec_accents_francais():
    assert valider_mots_cles("rapprochement bancaire manuel répété") == "rapprochement bancaire manuel répété"


# -------------------------------------------------- deriver_mots_cles_repli --

def test_derive_retire_les_mots_vides_fr_et_en():
    assert deriver_mots_cles_repli(
        "We manually reconcile the invoices every week and it is very tedious"
    ) == "manually reconcile invoices every week"


def test_derive_max_5_mots_par_defaut():
    resultat = deriver_mots_cles_repli("alpha bravo charlie delta echo foxtrot golf hotel")
    assert len(resultat.split(" ")) == 5
    assert resultat == "alpha bravo charlie delta echo"


def test_derive_respecte_max_mots_personnalise():
    resultat = deriver_mots_cles_repli("alpha bravo charlie delta echo foxtrot", max_mots=2)
    assert resultat == "alpha bravo"


def test_derive_dedoublonne_en_gardant_l_ordre_d_apparition():
    resultat = deriver_mots_cles_repli("invoice invoice reconciliation invoice manual process", max_mots=5)
    assert resultat == "invoice reconciliation manual process"


def test_derive_none_si_texte_vide():
    assert deriver_mots_cles_repli("") is None


def test_derive_none_si_seulement_des_mots_vides_ou_trop_courts():
    assert deriver_mots_cles_repli("the a of it is on at by") is None


def test_derive_ignore_la_ponctuation():
    # "on"/"des"/"chaque" sont des mots vides -- seuls les mots significatifs
    # restent, dans leur ordre d'apparition.
    assert deriver_mots_cles_repli("On perd des heures, chaque semaine, à réconcilier !") == "perd heures semaine réconcilier"
