"""Rôle Analyst : examiner les meilleures pistes (§2.B).

V1 sans outil de recherche web propre au job nocturne : l'Analyst ne peut
s'appuyer QUE sur les preuves déjà collectées et rattachées à l'opportunité
(par le Scout, ou par un humain). Toute affirmation qui citerait une source
hors de cet ensemble est neutralisée après coup — jamais laissée telle
quelle — pour ne jamais transformer une invention du modèle en fait
présenté comme sourcé.
"""
from __future__ import annotations

import logging

from app.adapters.model_client import AccesModeleIndisponible, ModelClient
from app.models_schemas import AnalystSortie, CritereAnalyst, NiveauPreuve, TypeAffirmation
from app.pipeline.budget import BudgetDepasse
from app.roles.prompts_communs import RAPPEL_SECURITE

logger = logging.getLogger(__name__)

VERSION_PROMPT = "analyst-v1"

NOMS_CRITERES = [
    "probleme_frequence_cout",
    "acheteur_disposition_payer",
    "gain_realisable_ia",
    "acces_clients",
    "concurrence_differenciation",
    "economie_cout_lancement",
    "faisabilite_risque",
]

PROMPT_SYSTEME = (
    "Tu es le rôle Analyst d'un radar d'opportunités économiques IA. "
    f"{RAPPEL_SECURITE}\n\n"
    "Tu ne dois utiliser QUE les preuves listées ci-dessous (par leur id de source). "
    "N'en invente aucune autre : si une information te semble manquante, mets-la dans "
    "`inconnues` du critère concerné plutôt que de citer une source qui n'est pas listée. "
    "Distingue observé/calculé/hypothèse pour chaque affirmation. Une marge n'est estimable "
    "que si prix de vente ET coûts sont tous deux appuyés par une preuve fournie — sinon "
    "`marge_indicative` doit rester `null`."
)


def _prompt_utilisateur(opportunite: dict, preuves: list[dict]) -> str:
    lignes = [f"Opportunité : {opportunite['titre']} — acheteur : {opportunite['acheteur']}",
              f"Problème : {opportunite['probleme']}", "", "Preuves disponibles (id_source: extrait) :"]
    for p in preuves:
        lignes.append(f"- {p['source_id']}: {p['extrait'][:400]}")
    lignes.append("")
    lignes.append(f"Critères attendus : {', '.join(NOMS_CRITERES)}")
    return "\n".join(lignes)


def _analyst_heuristique(opportunity_id: str) -> AnalystSortie:
    return AnalystSortie(
        opportunity_id=opportunity_id,
        criteres=[CritereAnalyst(nom=n, affirmations=[], inconnues=["mode sans modèle : aucune évaluation automatique"]) for n in NOMS_CRITERES],
        prix_observes=[],
        marge_indicative=None,
        contradictions=[],
        prochain_test_moins_couteux="entretien manuel à programmer (mode sans modèle)",
        niveau_preuve_global=NiveauPreuve.FAIBLE,
    )


def _neutraliser_sources_hors_perimetre(sortie: AnalystSortie, sources_autorisees: set[str]) -> AnalystSortie:
    for critere in sortie.criteres:
        for affirmation in critere.affirmations:
            hors = [s for s in affirmation.source_ids if s not in sources_autorisees]
            if hors:
                logger.warning("Analyst: source(s) hors périmètre %s neutralisée(s).", hors)
                affirmation.source_ids = [s for s in affirmation.source_ids if s in sources_autorisees]
                affirmation.type = TypeAffirmation.NON_VERIFIE
    for prix in sortie.prix_observes:
        prix.source_ids = [s for s in prix.source_ids if s in sources_autorisees]
    if sortie.marge_indicative is not None:
        sortie.marge_indicative.source_ids = [s for s in sortie.marge_indicative.source_ids if s in sources_autorisees]
        if not sortie.marge_indicative.source_ids:
            sortie.marge_indicative = None
    return sortie


def executer_analyst(
    *, opportunity_id: str, opportunite: dict, preuves: list[dict], model_client: ModelClient | None, modele: str,
) -> tuple[AnalystSortie, bool]:
    sources_autorisees = {p["source_id"] for p in preuves}

    if model_client is not None:
        try:
            sortie = model_client.appeler_structure(
                modele=modele,
                prompt_systeme=PROMPT_SYSTEME,
                prompt_utilisateur=_prompt_utilisateur(opportunite, preuves),
                schema=AnalystSortie,
                version_prompt=VERSION_PROMPT,
                max_tokens=2500,
            )
        except BudgetDepasse:
            raise
        except AccesModeleIndisponible:
            sortie = None

        if sortie is not None:
            sortie.opportunity_id = opportunity_id
            return _neutraliser_sources_hors_perimetre(sortie, sources_autorisees), True

    return _analyst_heuristique(opportunity_id), False
