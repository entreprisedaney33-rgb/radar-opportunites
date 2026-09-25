"""Rôle Critic : réfuter, puis réévaluer (§2.C).

Le Critic ne modifie jamais directement un score : il rend une décision
(`rejeter`/`a_verifier`/`eligible_revue_humaine`) et des objections. C'est le
pipeline qui, à partir de cette décision, change le STATUT de l'opportunité
— jamais les points du score, qui restent uniquement dérivés des critères de
l'Analyst (voir scoring/engine.py). Ainsi le Critic ne peut pas retirer
« arbitrairement -15 points » : il n'a tout simplement pas accès au score.
"""
from __future__ import annotations

import logging

from app.adapters.model_client import AccesModeleIndisponible, ModelClient
from app.models_schemas import AnalystSortie, CriticSortie, DecisionCritic
from app.pipeline.budget import BudgetDepasse
from app.roles.prompts_communs import RAPPEL_SECURITE

logger = logging.getLogger(__name__)

VERSION_PROMPT = "critic-v1"

PROMPT_SYSTEME = (
    "Tu es le rôle Critic d'un radar d'opportunités économiques IA. "
    f"{RAPPEL_SECURITE}\n\n"
    "Cherche activement les failles du dossier Analyst ci-dessous : faux acheteur, "
    "absence de budget, concurrence plus forte, données périmées, coûts cachés, "
    "dépendance à une plateforme, illusion de marge. N'utilise QUE les preuves listées "
    "(par id de source) pour étayer tes objections — sinon laisse `source_ids` vide. "
    "Choisis une décision : `rejeter` (faille rédhibitoire), `a_verifier` (contradiction "
    "non résolue) ou `eligible_revue_humaine` (dossier soutenable, malgré ses inconnues)."
)


def _prompt_utilisateur(opportunite: dict, analyst_sortie: AnalystSortie, preuves: list[dict]) -> str:
    lignes = [f"Opportunité : {opportunite['titre']}", "", "Dossier Analyst :"]
    for c in analyst_sortie.criteres:
        lignes.append(f"- {c.nom}: {len(c.affirmations)} affirmation(s), inconnues={c.inconnues}")
    lignes.append(f"Contradictions déjà notées par l'Analyst : {analyst_sortie.contradictions}")
    lignes.append("")
    lignes.append("Preuves disponibles (id_source: extrait) :")
    for p in preuves:
        lignes.append(f"- {p['source_id']}: {p['extrait'][:400]}")
    return "\n".join(lignes)


def _critic_heuristique(opportunity_id: str) -> CriticSortie:
    return CriticSortie(
        opportunity_id=opportunity_id,
        objections=[],
        faits_contestes=[],
        recherches_supplementaires=["revue humaine (mode sans modèle : aucune critique automatique)"],
        decision=DecisionCritic.A_VERIFIER,
        motif="Mode sans modèle : impossible de challenger le dossier automatiquement.",
    )


def _neutraliser_sources_hors_perimetre(sortie: CriticSortie, sources_autorisees: set[str]) -> CriticSortie:
    for objection in sortie.objections:
        hors = [s for s in objection.source_ids if s not in sources_autorisees]
        if hors:
            logger.warning("Critic: source(s) hors périmètre %s neutralisée(s).", hors)
            objection.source_ids = [s for s in objection.source_ids if s in sources_autorisees]
    return sortie


def executer_critic(
    *, opportunity_id: str, opportunite: dict, analyst_sortie: AnalystSortie, preuves: list[dict],
    model_client: ModelClient | None, modele: str,
) -> tuple[CriticSortie, bool]:
    sources_autorisees = {p["source_id"] for p in preuves}

    if model_client is not None:
        try:
            sortie = model_client.appeler_structure(
                modele=modele,
                prompt_systeme=PROMPT_SYSTEME,
                prompt_utilisateur=_prompt_utilisateur(opportunite, analyst_sortie, preuves),
                schema=CriticSortie,
                version_prompt=VERSION_PROMPT,
                max_tokens=1800,
            )
        except BudgetDepasse:
            raise
        except AccesModeleIndisponible:
            sortie = None

        if sortie is not None:
            sortie.opportunity_id = opportunity_id
            return _neutraliser_sources_hors_perimetre(sortie, sources_autorisees), True

    return _critic_heuristique(opportunity_id), False


STATUT_PAR_DECISION = {
    DecisionCritic.REJETER: "rejete",
    DecisionCritic.A_VERIFIER: "incertain",
    DecisionCritic.ELIGIBLE_REVUE_HUMAINE: "a_revoir",
}
