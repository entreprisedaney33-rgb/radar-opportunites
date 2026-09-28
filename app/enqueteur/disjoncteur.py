"""Disjoncteur Reddit de l'Enquêteur (sous-étape 3.11 d'AMELIORATIONS.md).

Après `SEUIL_ECHECS_CONSECUTIFS` réponses 429 consécutives — une par appel à
`FournisseurReddit.rechercher` qui a lui-même déjà épuisé ses tentatives
internes (`app.adapters.http.get_with_retry`, `TropDeRequetes`) — le
fournisseur Reddit de l'Enquêteur est mis en pause pour
`DUREE_PAUSE_MINUTES`, au lieu d'être retenté à chaque requête suivante.

Différence volontaire avec le disjoncteur du Scout
(`app.pipeline.orchestrator._collecter`, « reddit_mis_en_pause ») : celui-là
est un état LOCAL à un seul appel de fonction, réinitialisé à chaque passage
du Background Worker (« le reste de CE passage »). Celui-ci doit survivre
PLUSIEURS passages consécutifs (la fenêtre est en MINUTES, pas en passages)
et rester observable par `app.metriques`, un processus séparé qui ne lit
jamais la mémoire du worker — l'état vit donc en base
(`etats_disjoncteur_enqueteur`, `app/storage/schema.py`,
`app.storage.repo.lire_disjoncteur_enqueteur`/`ecrire_disjoncteur_enqueteur`),
jamais en mémoire du processus.

Les fonctions ci-dessous sont PURES (aucun accès base ni réseau) : c'est
`app.enqueteur.fournisseurs_gratuits.FournisseurReddit` qui les relie à
`app.storage.repo` pour un usage réel."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

# Seul cas réel aujourd'hui -- une clé par fournisseur si un jour un autre
# fournisseur payant (`app.enqueteur.fournisseur_payant`) a besoin du même
# mécanisme.
NOM_REDDIT = "reddit"

SEUIL_ECHECS_CONSECUTIFS = 3
DUREE_PAUSE_MINUTES = 60


@dataclass(frozen=True)
class EtatDisjoncteur:
    echecs_consecutifs: int
    pause_jusqu_a: datetime | None


ETAT_INITIAL = EtatDisjoncteur(echecs_consecutifs=0, pause_jusqu_a=None)


def est_en_pause(etat: EtatDisjoncteur, maintenant: datetime) -> bool:
    return etat.pause_jusqu_a is not None and maintenant < etat.pause_jusqu_a


def apres_echec_429(
    etat: EtatDisjoncteur,
    *,
    maintenant: datetime,
    seuil: int = SEUIL_ECHECS_CONSECUTIFS,
    duree_pause_minutes: int = DUREE_PAUSE_MINUTES,
) -> EtatDisjoncteur:
    """Un 429 de plus. Sous le seuil : seul le compteur avance (la pause en
    cours, s'il y en a une, reste inchangée -- ne devrait normalement pas se
    produire, l'appelant vérifie `est_en_pause` avant de tenter quoi que ce
    soit). Au seuil : nouvelle pause de `duree_pause_minutes` à partir de
    MAINTENANT, compteur remis à zéro (repart de zéro pour la prochaine
    série, après l'expiration de cette pause)."""
    echecs = etat.echecs_consecutifs + 1
    if echecs >= seuil:
        return EtatDisjoncteur(
            echecs_consecutifs=0, pause_jusqu_a=maintenant + timedelta(minutes=duree_pause_minutes),
        )
    return EtatDisjoncteur(echecs_consecutifs=echecs, pause_jusqu_a=etat.pause_jusqu_a)


def apres_succes(etat: EtatDisjoncteur) -> EtatDisjoncteur:
    """Une réponse qui n'est PAS un 429 remet tout à zéro -- 3 échecs
    CONSÉCUTIFS, jamais 3 au total sur toute la durée de vie du worker."""
    return ETAT_INITIAL
