"""Disjoncteur de l'appel au modèle (sous-étape 3.13 d'AMELIORATIONS.md).

Après `SEUIL_ECHECS_CONSECUTIFS` échecs consécutifs d'un appel au modèle
(quelle qu'en soit la cause -- exception réseau/API, sortie sans `tool_use`,
échec de validation malgré la relance : voir
`app/adapters/model_client.py::ModelClient.appeler_structure`), le worker
entre dans l'état « API en erreur » : aucune tentative supplémentaire n'est
faite pendant `DUREE_PAUSE_MINUTES`, et surtout -- contrairement à un échec
isolé -- aucun repli heuristique n'est utilisé pour créer ou faire avancer un
dossier pendant cet état (§ point 2 de la sous-étape : « aucun dossier n'est
créé ni analysé par repli pendant cet état »). C'est
`app.pipeline.orchestrator` qui applique cette règle : il attrape
`app.adapters.model_client.DisjoncteurAPIOuvert` et s'arrête pour ce passage
plutôt que de laisser `role_scout`/`role_analyst`/`role_critic` retomber sur
leur repli habituel.

Même esprit que `app.enqueteur.disjoncteur` (disjoncteur Reddit de
l'Enquêteur, sous-étape 3.11) : état PERSISTÉ EN BASE
(`etats_disjoncteur_api`, `app/storage/schema.py`,
`app.storage.repo.lire_disjoncteur_api`/`ecrire_disjoncteur_api`), jamais en
mémoire du processus -- doit survivre plusieurs passages (la fenêtre est en
MINUTES) et rester lisible par `app.metriques`, un processus séparé.

Différence de comportement, volontaire, avec le disjoncteur Reddit : une fois
l'état « en erreur » atteint, un SEUL nouvel échec suffit à réarmer une
nouvelle pause de `DUREE_PAUSE_MINUTES` (pas besoin de
`SEUIL_ECHECS_CONSECUTIFS` de plus) -- l'incident est déjà avéré, inutile de
le laisser retenter en boucle serrée pour le reconfirmer. `depuis` (heure de
DÉBUT de l'incident, affichée par Jarvis) ne change jamais tant qu'aucun
succès n'a eu lieu, même à travers plusieurs cycles de pause/nouvel essai.

Fonctions pures (aucun accès base ni réseau)."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

SEUIL_ECHECS_CONSECUTIFS = 5
DUREE_PAUSE_MINUTES = 15


@dataclass(frozen=True)
class EtatDisjoncteurAPI:
    echecs_consecutifs: int
    en_erreur: bool
    depuis: datetime | None  # début de l'incident en cours ; None si en_erreur=False
    pause_jusqu_a: datetime | None
    dernier_message: str | None


ETAT_INITIAL = EtatDisjoncteurAPI(
    echecs_consecutifs=0, en_erreur=False, depuis=None, pause_jusqu_a=None, dernier_message=None,
)


def doit_bloquer(etat: EtatDisjoncteurAPI, maintenant: datetime) -> bool:
    """True tant qu'on est dans la fenêtre de pause d'un incident ouvert --
    l'appelant ne doit alors RIEN tenter (voir docstring de module). Une fois
    la pause expirée, un nouvel essai est autorisé (l'incident reste affiché
    comme en cours -- `etat.en_erreur` -- tant qu'aucun succès ne l'a fermé,
    mais ce nouvel essai n'est plus bloqué)."""
    return etat.en_erreur and etat.pause_jusqu_a is not None and maintenant < etat.pause_jusqu_a


def apres_echec(
    etat: EtatDisjoncteurAPI,
    *,
    message: str,
    maintenant: datetime,
    seuil: int = SEUIL_ECHECS_CONSECUTIFS,
    duree_pause_minutes: int = DUREE_PAUSE_MINUTES,
) -> EtatDisjoncteurAPI:
    if etat.en_erreur:
        # Incident déjà ouvert : un échec de plus (le "nouvel essai" qui
        # rate) réarme la pause sans exiger de seuil échecs supplémentaires
        # -- `depuis` reste celui de l'ouverture initiale, le compteur
        # repart de zéro (même convention qu'à l'ouverture initiale
        # ci-dessous : il compte les échecs du cycle courant, pas un total).
        return replace(
            etat, echecs_consecutifs=0,
            pause_jusqu_a=maintenant + timedelta(minutes=duree_pause_minutes),
            dernier_message=message,
        )
    echecs = etat.echecs_consecutifs + 1
    if echecs >= seuil:
        return EtatDisjoncteurAPI(
            echecs_consecutifs=0, en_erreur=True, depuis=maintenant,
            pause_jusqu_a=maintenant + timedelta(minutes=duree_pause_minutes),
            dernier_message=message,
        )
    return replace(etat, echecs_consecutifs=echecs, dernier_message=message)


def apres_succes(etat: EtatDisjoncteurAPI) -> EtatDisjoncteurAPI:
    """Un appel réussi ferme l'incident pour de bon, quel que soit l'état
    précédent -- `SEUIL_ECHECS_CONSECUTIFS` échecs CONSÉCUTIFS, jamais un
    total cumulé sur la durée de vie du worker."""
    return ETAT_INITIAL
