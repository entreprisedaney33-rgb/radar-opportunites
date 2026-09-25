"""Rôle Scout : découvrir et regrouper (§2.A).

Deux chemins, jamais mélangés dans le résultat : un chemin modèle (quand une
clé API et du budget sont disponibles) et un repli déterministe, honnête —
qui ne fabrique aucun fait, il se contente de marquer tout comme à
confirmer. Le repli s'utilise quand l'accès manque, quand le budget est
épuisé, ou quand la sortie du modèle ne passe pas la validation stricte.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.adapters.model_client import AccesModeleIndisponible, ModelClient
from app.models_schemas import ScoutSortie
from app.pipeline.budget import BudgetDepasse
from app.roles.prompts_communs import RAPPEL_SECURITE

logger = logging.getLogger(__name__)

VERSION_PROMPT = "scout-v1"

PROMPT_SYSTEME = (
    "Tu es le rôle Scout d'un radar d'opportunités économiques IA. "
    f"{RAPPEL_SECURITE}\n\n"
    "À partir d'UN signal fourni, propose une hypothèse courte : qui pourrait être "
    "l'acheteur, quelle douleur concrète, quel mécanisme IA changerait la donne, "
    "pourquoi maintenant. Si une information manque, mets-la dans `missing_facts` "
    "plutôt que de l'inventer. `signal_ids` doit contenir UNIQUEMENT l'identifiant "
    "du signal fourni ci-dessous."
)


def _prompt_utilisateur(signal_id: str, texte: str, secteur: str) -> str:
    return (
        f"Secteur proposé : {secteur}\n"
        f"Signal (id={signal_id}) :\n{texte}\n\n"
        "Réponds avec l'outil `repondre`."
    )


def _scout_heuristique(signal_id: str, texte: str, secteur: str) -> ScoutSortie:
    titre = texte.strip().splitlines()[0][:120]
    return ScoutSortie(
        opportunity_candidate=titre,
        buyer="à confirmer (mode sans modèle : aucune extraction automatique de l'acheteur)",
        pain=texte.strip()[:300],
        ai_mechanism="à définir en analyse",
        why_now=f"signal détecté le {datetime.now(timezone.utc).date().isoformat()}",
        signal_ids=[signal_id],
        missing_facts=["acheteur", "mécanisme IA précis", "économie chiffrée"],
        secteur=secteur,
        cluster_id=None,
    )


def executer_scout(
    *, signal_id: str, texte: str, secteur: str, model_client: ModelClient | None, modele: str,
) -> tuple[ScoutSortie, bool]:
    """Renvoie (sortie, via_modele)."""
    if model_client is not None:
        try:
            sortie = model_client.appeler_structure(
                modele=modele,
                prompt_systeme=PROMPT_SYSTEME,
                prompt_utilisateur=_prompt_utilisateur(signal_id, texte, secteur),
                schema=ScoutSortie,
                version_prompt=VERSION_PROMPT,
                role="scout",
            )
        except BudgetDepasse:
            raise
        except AccesModeleIndisponible:
            sortie = None

        if sortie is not None:
            # Le modèle ne doit citer QUE le signal fourni — sinon repli.
            if set(sortie.signal_ids) <= {signal_id} and sortie.signal_ids:
                return sortie, True
            logger.warning("Scout: signal_ids hors périmètre (%s), repli heuristique.", sortie.signal_ids)

    return _scout_heuristique(signal_id, texte, secteur), False
