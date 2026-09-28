"""Sous-étape 3.11 : `app.enqueteur.disjoncteur` — machine à états PURE du
disjoncteur Reddit de l'Enquêteur (aucune base, aucun réseau ici — voir
`tests/test_enqueteur_fournisseurs_gratuits.py` pour le branchement réel sur
`FournisseurReddit`)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.enqueteur import disjoncteur as d

MAINTENANT = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


def test_etat_initial_jamais_en_pause():
    assert d.est_en_pause(d.ETAT_INITIAL, MAINTENANT) is False


def test_sous_le_seuil_le_compteur_avance_sans_pause():
    etat = d.ETAT_INITIAL
    for attendu in range(1, d.SEUIL_ECHECS_CONSECUTIFS):
        etat = d.apres_echec_429(etat, maintenant=MAINTENANT)
        assert etat.echecs_consecutifs == attendu
        assert etat.pause_jusqu_a is None
        assert d.est_en_pause(etat, MAINTENANT) is False


def test_au_seuil_declenche_une_pause_et_remet_le_compteur_a_zero():
    etat = d.ETAT_INITIAL
    for _ in range(d.SEUIL_ECHECS_CONSECUTIFS):
        etat = d.apres_echec_429(etat, maintenant=MAINTENANT)
    assert etat.echecs_consecutifs == 0
    assert etat.pause_jusqu_a == MAINTENANT + timedelta(minutes=d.DUREE_PAUSE_MINUTES)
    assert d.est_en_pause(etat, MAINTENANT) is True


def test_pause_expire_apres_la_duree_configuree():
    etat = d.ETAT_INITIAL
    for _ in range(d.SEUIL_ECHECS_CONSECUTIFS):
        etat = d.apres_echec_429(etat, maintenant=MAINTENANT)
    juste_avant = MAINTENANT + timedelta(minutes=d.DUREE_PAUSE_MINUTES) - timedelta(seconds=1)
    juste_apres = MAINTENANT + timedelta(minutes=d.DUREE_PAUSE_MINUTES) + timedelta(seconds=1)
    assert d.est_en_pause(etat, juste_avant) is True
    assert d.est_en_pause(etat, juste_apres) is False


def test_succes_remet_tout_a_zero():
    etat = d.EtatDisjoncteur(echecs_consecutifs=2, pause_jusqu_a=None)
    assert d.apres_succes(etat) == d.ETAT_INITIAL


def test_succes_efface_meme_une_pause_en_cours():
    etat = d.EtatDisjoncteur(echecs_consecutifs=0, pause_jusqu_a=MAINTENANT + timedelta(minutes=10))
    assert d.apres_succes(etat) == d.ETAT_INITIAL


def test_seuil_et_duree_personnalises():
    etat = d.ETAT_INITIAL
    etat = d.apres_echec_429(etat, maintenant=MAINTENANT, seuil=1, duree_pause_minutes=5)
    assert etat.pause_jusqu_a == MAINTENANT + timedelta(minutes=5)
