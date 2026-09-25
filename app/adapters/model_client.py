"""Adaptateur modèle : reçoit tâche + schéma + budget, journalise usage et
coût (§6). Sépare la logique métier du fournisseur — changer de modèle ne
touche que ce fichier.

Tarifs indicatifs seulement (PRICES_USD_PAR_MILLION) : À VÉRIFIER sur la
page tarifaire réelle du fournisseur avant tout run réel, comme demandé par
le cahier des charges (§4, §6). ~1 USD ≈ 0,92 EUR — conversion approximative
elle aussi à vérifier.
"""
from __future__ import annotations

import logging

from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.pipeline.budget import BudgetTracker

logger = logging.getLogger(__name__)

# Indicatif — à vérifier avant activation d'un run réel (voir docstring).
PRICES_USD_PAR_MILLION_TOKENS = {
    "claude-haiku-4-5-20251001": {"input": 1.0, "output": 5.0},
    "claude-sonnet-5": {"input": 3.0, "output": 15.0},
}
USD_VERS_EUR = 0.92


class AccesModeleIndisponible(Exception):
    pass


def _estimer_cout_eur(modele: str, tokens_in_est: int, tokens_out_est: int) -> float:
    prix = PRICES_USD_PAR_MILLION_TOKENS.get(modele, {"input": 3.0, "output": 15.0})
    usd = (tokens_in_est / 1_000_000) * prix["input"] + (tokens_out_est / 1_000_000) * prix["output"]
    return usd * USD_VERS_EUR


class ModelClient:
    def __init__(self, settings: Settings, budget_tracker: BudgetTracker):
        self.settings = settings
        self.budget = budget_tracker
        self._client = None

    def _get_client(self):
        if self._client is None:
            import anthropic  # import différé : pas nécessaire en mode démo

            self._client = anthropic.Anthropic(api_key=self.settings.anthropic_api_key)
        return self._client

    def appeler_structure(
        self,
        *,
        modele: str,
        prompt_systeme: str,
        prompt_utilisateur: str,
        schema: type[BaseModel],
        version_prompt: str,
        max_tokens: int = 1500,
    ) -> BaseModel | None:
        """Renvoie une instance validée de `schema`, ou None si l'accès
        manque, si le budget est dépassé, ou si la sortie ne respecte pas le
        schéma (jamais d'exception avalée en silence : tout est journalisé)."""
        if not self.settings.has_model_access:
            raise AccesModeleIndisponible("ANTHROPIC_API_KEY absente : appeler le mode démo à la place.")

        tokens_in_est = (len(prompt_systeme) + len(prompt_utilisateur)) // 4
        cout_estime = _estimer_cout_eur(modele, tokens_in_est, max_tokens)
        self.budget.verifier_et_engager(cout_estime)  # lève BudgetDepasse si insuffisant

        client = self._get_client()
        outil = {
            "name": "repondre",
            "description": "Réponds strictement selon ce schéma JSON, sans champ supplémentaire.",
            "input_schema": schema.model_json_schema(),
        }
        try:
            resp = client.messages.create(
                model=modele,
                max_tokens=max_tokens,
                system=prompt_systeme,
                messages=[{"role": "user", "content": prompt_utilisateur}],
                tools=[outil],
                tool_choice={"type": "tool", "name": "repondre"},
            )
        except Exception as exc:  # réseau, 429, etc. — journalisé, pas de crash du run entier
            logger.error("Appel modèle échoué (%s): %s", modele, exc)
            self.budget.enregistrer_reel(
                fournisseur="anthropic", modele_ou_actor=modele, appels=1,
                tokens_in=None, tokens_out=None, cout_reel=0.0, cout_estime_engage=cout_estime,
            )
            return None

        tokens_in_reel = getattr(resp.usage, "input_tokens", tokens_in_est)
        tokens_out_reel = getattr(resp.usage, "output_tokens", 0)
        cout_reel = _estimer_cout_eur(modele, tokens_in_reel, tokens_out_reel)
        self.budget.enregistrer_reel(
            fournisseur="anthropic", modele_ou_actor=modele, appels=1,
            tokens_in=tokens_in_reel, tokens_out=tokens_out_reel,
            cout_reel=cout_reel, cout_estime_engage=cout_estime,
        )

        bloc_outil = next((b for b in resp.content if getattr(b, "type", None) == "tool_use"), None)
        if bloc_outil is None:
            logger.warning("Aucun tool_use dans la réponse du modèle %s", modele)
            return None
        try:
            return schema.model_validate(bloc_outil.input)
        except ValidationError as exc:
            logger.warning("Sortie du modèle %s invalide vs schéma %s: %s", modele, schema.__name__, exc)
            return None
