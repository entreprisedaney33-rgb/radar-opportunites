"""Adaptateur modèle : reçoit tâche + schéma + budget, journalise usage et
coût (§6). Sépare la logique métier du fournisseur — changer de modèle ne
touche que ce fichier.

Tarifs et taux de change lus depuis `config/tarifs.yaml` (source et date de
vérification dans ce fichier — sous-étape 0.7 d'AMELIORATIONS.md). Le
fournisseur ne facture jamais l'estimation d'ici : c'est une approximation
cohérente, pas une facture réelle (voir `rapports/DIAGNOSTIC_BUDGET_2026-09-25.md`, §5).

Aucun `cache_control` n'est envoyé dans les requêtes de ce fichier : le cache
de prompt Anthropic ne s'active jamais ici (il est strictement opt-in côté
API), donc `cache_creation_input_tokens`/`cache_read_input_tokens` valent
toujours 0 et n'ont pas à entrer dans `_estimer_cout_eur`.
"""
from __future__ import annotations

import json
import logging

from pydantic import BaseModel, ValidationError

from app import config as cfg
from app.config import Settings
from app.pipeline.budget import BudgetTracker

logger = logging.getLogger(__name__)

# Secours pour un modèle absent de config/tarifs.yaml (nouveau modèle pas
# encore ajouté à la config) : hypothèse volontairement pessimiste plutôt que
# de sous-estimer un coût réel inconnu.
PRIX_PAR_DEFAUT = {"input": 5.0, "output": 25.0}


class AccesModeleIndisponible(Exception):
    pass


def _normaliser_sortie_outil(brut: object, schema: type[BaseModel]) -> object:
    """Corrige deux déformations observées en usage réel avec l'API tool-use,
    avant validation stricte par pydantic — jamais de contenu inventé ici,
    seulement du reformatage de ce que le modèle a réellement renvoyé :

    1. Le modèle enveloppe parfois sa réponse dans une clé unique
       (`{"repondre": {...}}`, `{"parameter": {...}}`) au lieu de renvoyer
       les champs directement. On déballe si aucun champ attendu n'est
       présent au premier niveau mais qu'une unique valeur imbriquée en
       contient.
    2. Un champ censé être une liste/un objet est parfois renvoyé comme une
       chaîne JSON (`'[{"texte": ...}]'` au lieu de `[{...}]`). On tente de
       la décoder ; en cas d'échec, on laisse tel quel (la validation
       pydantic échouera alors normalement, comme avant)."""
    if not isinstance(brut, dict):
        return brut

    champs_attendus = set(schema.model_fields.keys())
    if not (set(brut.keys()) & champs_attendus) and len(brut) == 1:
        (valeur_unique,) = brut.values()
        if isinstance(valeur_unique, dict):
            brut = valeur_unique

    resultat = dict(brut)
    for cle, valeur in resultat.items():
        champ = schema.model_fields.get(cle)
        if champ is not None and champ.annotation is not str and isinstance(valeur, str):
            try:
                resultat[cle] = json.loads(valeur)
            except (json.JSONDecodeError, TypeError):
                pass
    return resultat


def _estimer_cout_eur(modele: str, tokens_in_est: int, tokens_out_est: int) -> float:
    config_tarifs = cfg.tarifs()
    prix = config_tarifs["prix_usd_par_million_tokens"].get(modele, PRIX_PAR_DEFAUT)
    usd = (tokens_in_est / 1_000_000) * prix["input"] + (tokens_out_est / 1_000_000) * prix["output"]
    return usd * config_tarifs["usd_vers_eur"]


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
        role: str,
        opportunity_id: str | None = None,
        max_tokens: int = 1500,
    ) -> BaseModel | None:
        """Renvoie une instance validée de `schema`, ou None si l'accès
        manque, si le budget est dépassé, ou si la sortie ne respecte pas le
        schéma (jamais d'exception avalée en silence : tout est journalisé).

        `role` (scout|analyst|critic) et `opportunity_id` tracent l'appel
        dans `usage_events` (sous-étape 0.7) — `opportunity_id` reste `None`
        pour le Scout, appelé avant que l'opportunité n'existe."""
        if not self.settings.has_model_access:
            raise AccesModeleIndisponible("ANTHROPIC_API_KEY absente : appeler le mode démo à la place.")

        tokens_in_est = (len(prompt_systeme) + len(prompt_utilisateur)) // 4
        cout_estime = _estimer_cout_eur(modele, tokens_in_est, max_tokens)
        self.budget.verifier_et_engager(cout_estime, role=role)  # lève BudgetDepasse si insuffisant

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
                role=role, opportunity_id=opportunity_id,
            )
            return None

        tokens_in_reel = getattr(resp.usage, "input_tokens", tokens_in_est)
        tokens_out_reel = getattr(resp.usage, "output_tokens", 0)
        cout_reel = _estimer_cout_eur(modele, tokens_in_reel, tokens_out_reel)
        self.budget.enregistrer_reel(
            fournisseur="anthropic", modele_ou_actor=modele, appels=1,
            tokens_in=tokens_in_reel, tokens_out=tokens_out_reel,
            cout_reel=cout_reel, cout_estime_engage=cout_estime,
            role=role, opportunity_id=opportunity_id,
        )

        bloc_outil = next((b for b in resp.content if getattr(b, "type", None) == "tool_use"), None)
        if bloc_outil is None:
            logger.warning("Aucun tool_use dans la réponse du modèle %s", modele)
            return None
        try:
            return schema.model_validate(_normaliser_sortie_outil(bloc_outil.input, schema))
        except ValidationError as exc:
            logger.warning("Sortie du modèle %s invalide vs schéma %s: %s", modele, schema.__name__, exc)
            return None
