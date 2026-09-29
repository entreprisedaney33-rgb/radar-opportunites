"""Bloc de faisabilité (sous-étape 4.1 d'AMELIORATIONS.md).

Deux usages, un seul texte de consigne :
- l'Analyst (`app.roles.analyst`) le demande DANS SA SORTIE pour toute
  nouvelle analyse (`CONSIGNE_FAISABILITE` ajoutée à son prompt) ;
- la reprise sur les dossiers déjà notés >= 50 (`executer_faisabilite`) le
  demande dans un appel dédié, court -- SANS relancer l'Analyst ni le Critic
  (un dossier existant garde son analyse et son score).

Le résultat est toujours une hypothèse, jamais une entrée du score.
"""
from __future__ import annotations

import logging

from pydantic import BaseModel

from app.adapters.model_client import AccesModeleIndisponible, ModelClient
from app.models_schemas import FaisabiliteSortie
from app.pipeline.budget import BudgetDepasse
from app.roles.prompts_communs import RAPPEL_SECURITE

logger = logging.getLogger(__name__)

VERSION_PROMPT = "faisabilite-v1"

CONSIGNE_FAISABILITE = (
    "Bloc `faisabilite` (HYPOTHÈSES, jamais des faits) : évalue si cette opportunité est "
    "réalisable par UNE personne seule, en France, sans équipe ni capital. Pour chacun des cinq "
    "champs, choisis UNE valeur parmi celles autorisées ET écris une justification d'UNE phrase "
    "(les valeurs, dans l'ordre : investissement_initial = moins_de_5k | 5k_a_20k | 20k_a_100k | "
    "plus_de_100k ; delai_premier_revenu = moins_de_3_mois | 3_a_12_mois | plus_de_12_mois ; "
    "marche = accessible_depuis_france | europe | etats_unis_seulement | autre ; competences = "
    "liste parmi dev_ia | vente | reglementaire_lourd | materiel_industriel | reseau_specifique ; "
    "taille_du_probleme = niche_locale | segment_pme | marche_national_large | systemique). "
    "Ces champs n'entrent JAMAIS dans le score : ne les gonfle ni ne les minore pour "
    "arranger le dossier."
)

PROMPT_SYSTEME = (
    "Tu es le rôle Faisabilité d'un radar d'opportunités économiques IA. "
    f"{RAPPEL_SECURITE}\n\n{CONSIGNE_FAISABILITE}"
)


class _SortieFaisabilite(BaseModel):
    faisabilite: FaisabiliteSortie


def _prompt_utilisateur(opportunite: dict, preuves: list[dict]) -> str:
    lignes = [
        f"Opportunité : {opportunite['titre']} — acheteur : {opportunite['acheteur']}",
        f"Problème : {opportunite['probleme']}",
        f"Mécanisme IA proposé : {opportunite['mecanisme_ia']}",
        "", "Preuves disponibles (id_source: extrait) :",
    ]
    for p in preuves:
        lignes.append(f"- {p['source_id']}: {p['extrait'][:400]}")
    return "\n".join(lignes)


def executer_faisabilite(
    *, opportunity_id: str, opportunite: dict, preuves: list[dict], model_client: ModelClient | None, modele: str,
) -> FaisabiliteSortie | None:
    """`None` si le modèle n'est pas disponible ou si la sortie reste invalide
    après la relance : jamais de valeur inventée par un repli (une faisabilité
    inconnue reste inconnue). `BudgetDepasse` et `DisjoncteurAPIOuvert`
    remontent à l'appelant."""
    if model_client is None:
        return None
    try:
        sortie = model_client.appeler_structure(
            modele=modele, prompt_systeme=PROMPT_SYSTEME,
            prompt_utilisateur=_prompt_utilisateur(opportunite, preuves),
            schema=_SortieFaisabilite, version_prompt=VERSION_PROMPT,
            role="faisabilite", opportunity_id=opportunity_id, max_tokens=1500,
        )
    except BudgetDepasse:
        raise
    except AccesModeleIndisponible:
        return None
    return sortie.faisabilite if sortie is not None else None
