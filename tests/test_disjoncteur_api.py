"""Sous-étape 3.13 : `app.pipeline.disjoncteur_api` — machine à états PURE du
disjoncteur de l'appel au modèle (aucune base, aucun réseau ici — voir
`tests/test_model_client.py` pour le branchement réel dans `ModelClient`)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.pipeline import disjoncteur_api as d

MAINTENANT = datetime(2026, 9, 26, 13, 41, tzinfo=timezone.utc)


def test_etat_initial_jamais_bloquant():
    assert d.doit_bloquer(d.ETAT_INITIAL, MAINTENANT) is False
    assert d.ETAT_INITIAL.en_erreur is False


def test_sous_le_seuil_le_compteur_avance_sans_ouvrir():
    etat = d.ETAT_INITIAL
    for attendu in range(1, d.SEUIL_ECHECS_CONSECUTIFS):
        etat = d.apres_echec(etat, message=f"echec {attendu}", maintenant=MAINTENANT)
        assert etat.echecs_consecutifs == attendu
        assert etat.en_erreur is False
        assert d.doit_bloquer(etat, MAINTENANT) is False


def test_au_seuil_ouvre_l_incident_et_remet_le_compteur_a_zero():
    etat = d.ETAT_INITIAL
    for _ in range(d.SEUIL_ECHECS_CONSECUTIFS):
        etat = d.apres_echec(etat, message="400 additionalProperties", maintenant=MAINTENANT)
    assert etat.echecs_consecutifs == 0
    assert etat.en_erreur is True
    assert etat.depuis == MAINTENANT
    assert etat.pause_jusqu_a == MAINTENANT + timedelta(minutes=d.DUREE_PAUSE_MINUTES)
    assert etat.dernier_message == "400 additionalProperties"
    assert d.doit_bloquer(etat, MAINTENANT) is True


def test_pause_expire_apres_la_duree_configuree():
    etat = d.ETAT_INITIAL
    for _ in range(d.SEUIL_ECHECS_CONSECUTIFS):
        etat = d.apres_echec(etat, message="x", maintenant=MAINTENANT)
    juste_avant = MAINTENANT + timedelta(minutes=d.DUREE_PAUSE_MINUTES) - timedelta(seconds=1)
    juste_apres = MAINTENANT + timedelta(minutes=d.DUREE_PAUSE_MINUTES) + timedelta(seconds=1)
    assert d.doit_bloquer(etat, juste_avant) is True
    # Après la pause, un nouvel essai est autorisé -- mais l'incident reste
    # affiché comme en cours (Jarvis) tant qu'aucun succès n'a eu lieu.
    assert d.doit_bloquer(etat, juste_apres) is False
    assert etat.en_erreur is True


def test_un_seul_echec_de_plus_une_fois_ouvert_reprolonge_la_pause():
    """Un incident déjà ouvert n'a pas besoin de 5 échecs de plus pour rester
    ouvert -- un seul (le "nouvel essai" qui rate encore) suffit à réarmer une
    nouvelle pause, `depuis` restant celui de l'ouverture initiale."""
    etat = d.ETAT_INITIAL
    for _ in range(d.SEUIL_ECHECS_CONSECUTIFS):
        etat = d.apres_echec(etat, message="x", maintenant=MAINTENANT)
    apres_pause = MAINTENANT + timedelta(minutes=d.DUREE_PAUSE_MINUTES) + timedelta(seconds=1)
    etat2 = d.apres_echec(etat, message="encore 400", maintenant=apres_pause)
    assert etat2.en_erreur is True
    assert etat2.depuis == MAINTENANT  # inchangé : début de l'incident, pas du dernier échec
    assert etat2.pause_jusqu_a == apres_pause + timedelta(minutes=d.DUREE_PAUSE_MINUTES)
    assert etat2.dernier_message == "encore 400"
    assert d.doit_bloquer(etat2, apres_pause) is True


def test_succes_ferme_l_incident_meme_en_cours_de_pause():
    etat = d.EtatDisjoncteurAPI(
        echecs_consecutifs=0, en_erreur=True, depuis=MAINTENANT,
        pause_jusqu_a=MAINTENANT + timedelta(minutes=10), dernier_message="400",
    )
    assert d.apres_succes(etat) == d.ETAT_INITIAL


def test_succes_remet_a_zero_sous_le_seuil_aussi():
    etat = d.EtatDisjoncteurAPI(
        echecs_consecutifs=2, en_erreur=False, depuis=None, pause_jusqu_a=None, dernier_message="x",
    )
    assert d.apres_succes(etat) == d.ETAT_INITIAL


def test_seuil_et_duree_personnalises():
    etat = d.apres_echec(d.ETAT_INITIAL, message="x", maintenant=MAINTENANT, seuil=1, duree_pause_minutes=5)
    assert etat.en_erreur is True
    assert etat.pause_jusqu_a == MAINTENANT + timedelta(minutes=5)
