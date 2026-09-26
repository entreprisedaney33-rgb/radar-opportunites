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
toujours 0 et n'ont pas à entrer dans `estimer_cout_eur`.

Sous-étape 3.10 (AMELIORATIONS.md) : mesure réelle du 26/09/2026 (voir Journal
de cette sous-étape) — sur la fenêtre fiable disponible (depuis le
déploiement de la sous-étape 0.7), 78,3 % des sorties Critic et 18,8 % des
sorties Analyst échouaient la validation pydantic (repli heuristique "mode
sans modèle", jamais distingué jusqu'ici d'un vrai manque d'accès modèle).
Cause observée dans les logs Render : `objections` renvoyé comme objet
unique ou comme chaîne JSON au lieu d'une liste, sorties `{}` vides. Deux
correctifs, dans l'ordre :
1. Le dictionnaire `outil` passé à l'API porte désormais `"strict": True`
   (champ du SDK Anthropic ≥ 1.8 : « guarantees schema validation on tool
   names and inputs ») -- garantit la forme AU NIVEAU DE L'API, plutôt que de
   compter uniquement sur une correction après coup.
2. `_normaliser_sortie_outil` (le filet existant) sait maintenant EN PLUS
   remettre un objet unique dans une liste à un élément (nouveau -- l'ancien
   filet ne traitait que l'enveloppe à clé unique et la chaîne JSON). Si la
   sortie échoue malgré tout la validation : UNE SEULE relance, avec le
   message d'erreur de validation joint au prompt utilisateur -- jamais de
   repli silencieux au-delà (voir `appeler_structure`).
"""
from __future__ import annotations

import json
import logging
from typing import get_origin

from pydantic import BaseModel, ValidationError

from app import config as cfg
from app.config import Settings
from app.pipeline.budget import BudgetTracker

logger = logging.getLogger(__name__)

# Secours pour un modèle absent de config/tarifs.yaml (nouveau modèle pas
# encore ajouté à la config) : hypothèse volontairement pessimiste plutôt que
# de sous-estimer un coût réel inconnu.
PRIX_PAR_DEFAUT = {"input": 5.0, "output": 25.0}

# Issues possibles d'un appel modèle structuré (sous-étape 3.10), journalisées
# dans `usage_events.issue` (app.storage.repo.inserer_usage_event) : une
# ligne par TENTATIVE (l'éventuelle relance en produit une deuxième).
ISSUE_VALIDE = "valide"  # validé du premier coup, sans normalisation
ISSUE_NORMALISEE = "normalisee"  # validé, mais seulement après _normaliser_sortie_outil
ISSUE_RELANCEE = "relancee"  # tentative invalide, mais une relance suit (voir appeler_structure)
ISSUE_PERDUE = "perdue"  # tentative invalide, DERNIÈRE tentative (relance déjà faite, ou pas de relance possible)

# Une seule relance (point 2 de la sous-étape 3.10) : ce nombre, jamais plus.
MAX_TENTATIVES = 2


class AccesModeleIndisponible(Exception):
    pass


def _annotation_est_liste(annotation: object) -> bool:
    return get_origin(annotation) is list


def _normaliser_sortie_outil(brut: object, schema: type[BaseModel]) -> object:
    """Corrige des déformations observées en usage réel avec l'API tool-use,
    avant validation stricte par pydantic — jamais de contenu inventé ici,
    seulement du reformatage de ce que le modèle a réellement renvoyé :

    1. Le modèle enveloppe parfois sa réponse dans une clé unique
       (`{"repondre": {...}}`, `{"parameter": {...}}`) au lieu de renvoyer
       les champs directement. On déballe si aucun champ attendu n'est
       présent au premier niveau mais qu'une unique valeur imbriquée en
       contient.
    2. Un champ censé être une liste est parfois renvoyé comme une chaîne
       JSON (`'[{"texte": ...}]'` au lieu de `[{...}]`). On tente de la
       décoder ; en cas d'échec, on laisse tel quel (la validation pydantic
       échouera alors normalement, comme avant).
    3. Sous-étape 3.10 : un champ censé être une liste est parfois renvoyé
       comme un objet UNIQUE (`{"texte": ..., "source_ids": []}` au lieu de
       `[{...}]`) -- que ce soit directement (déjà un `dict`) ou après
       décodage d'une chaîne JSON par le point 2 ci-dessus -- on le remet
       dans une liste à un élément."""
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
        if champ is None:
            continue
        if champ.annotation is not str and isinstance(valeur, str):
            try:
                valeur = json.loads(valeur)
            except (json.JSONDecodeError, TypeError):
                valeur = resultat[cle]
        if _annotation_est_liste(champ.annotation) and isinstance(valeur, dict):
            valeur = [valeur]
        resultat[cle] = valeur
    return resultat


def estimer_cout_eur(modele: str, tokens_in_est: int, tokens_out_est: int) -> float:
    """Fonction pure, réutilisée par app.metriques (sous-étape 3.6, préalable)
    pour recalculer un coût passé aux tarifs COURANTS de la config — les
    tarifs peuvent changer (voir config/tarifs.yaml) sans que les lignes déjà
    journalisées dans usage_events soient recalculées rétroactivement."""
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
        schéma malgré une relance (jamais d'exception avalée en silence :
        tout est journalisé).

        `role` (scout|analyst|critic) et `opportunity_id` tracent l'appel
        dans `usage_events` (sous-étape 0.7) — `opportunity_id` reste `None`
        pour le Scout, appelé avant que l'opportunité n'existe.

        Sous-étape 3.10 : jusqu'à `MAX_TENTATIVES` (2) appels réels -- la
        première tentative invalide déclenche UNE SEULE relance, avec le
        message d'erreur de validation joint au prompt utilisateur (jamais de
        repli silencieux au-delà). Chaque tentative est une ligne
        `usage_events` à part (son propre coût réel), étiquetée `issue`
        (voir `ISSUE_*` ci-dessus) -- la première tentative d'une paire qui
        déclenche une relance est `ISSUE_RELANCEE`, jamais `ISSUE_PERDUE`
        (qui ne marque que la toute DERNIÈRE tentative invalide)."""
        if not self.settings.has_model_access:
            raise AccesModeleIndisponible("ANTHROPIC_API_KEY absente : appeler le mode démo à la place.")

        erreur_precedente: str | None = None
        for tentative in range(1, MAX_TENTATIVES + 1):
            derniere_tentative = tentative == MAX_TENTATIVES
            prompt_effectif = prompt_utilisateur
            if erreur_precedente is not None:
                prompt_effectif = (
                    f"{prompt_utilisateur}\n\n"
                    "Ta réponse précédente ne respectait pas le schéma attendu -- erreur de "
                    f"validation : {erreur_precedente}\n"
                    "Corrige et renvoie une réponse strictement conforme au schéma."
                )

            resultat, erreur_validation = self._un_appel(
                modele=modele, prompt_systeme=prompt_systeme, prompt_utilisateur=prompt_effectif,
                schema=schema, role=role, opportunity_id=opportunity_id, max_tokens=max_tokens,
                derniere_tentative=derniere_tentative,
            )
            if resultat is not None or erreur_validation is None:
                # Succès, ou échec non lié à la validation (erreur réseau/API
                # -- déjà journalisé et jamais relancé, voir `_un_appel`).
                return resultat
            if derniere_tentative:
                return None
            logger.info(
                "Sortie du modèle %s invalide (role=%s) -- relance %d/%d avec l'erreur jointe.",
                modele, role, tentative + 1, MAX_TENTATIVES,
            )
            erreur_precedente = erreur_validation
        return None  # jamais atteint (la boucle renvoie toujours avant) -- garde de type

    def _un_appel(
        self, *, modele: str, prompt_systeme: str, prompt_utilisateur: str, schema: type[BaseModel],
        role: str, opportunity_id: str | None, max_tokens: int, derniere_tentative: bool,
    ) -> tuple[BaseModel | None, str | None]:
        """UNE tentative réelle (un appel API, un budget engagé, une ligne
        `usage_events`). Renvoie `(resultat, erreur_validation)` :
        `erreur_validation` n'est jamais `None` seulement quand la sortie est
        invalide ET qu'une relance a un sens (voir `appeler_structure`) --
        `None` pour un succès ou pour un échec qui ne se relance jamais
        (erreur réseau/API, budget)."""
        tokens_in_est = (len(prompt_systeme) + len(prompt_utilisateur)) // 4
        cout_estime = estimer_cout_eur(modele, tokens_in_est, max_tokens)
        self.budget.verifier_et_engager(cout_estime, role=role)  # lève BudgetDepasse si insuffisant

        client = self._get_client()
        outil = {
            "name": "repondre",
            "description": "Réponds strictement selon ce schéma JSON, sans champ supplémentaire.",
            "input_schema": schema.model_json_schema(),
            # Sous-étape 3.10 : garantit la forme de la sortie au niveau de
            # l'API elle-même (SDK Anthropic ≥ 1.8, "structured outputs") --
            # voir la docstring de module.
            "strict": True,
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
        except Exception as exc:  # réseau, 429, etc. — journalisé, jamais relancé, pas de crash du run entier
            logger.error("Appel modèle échoué (%s): %s", modele, exc)
            self.budget.enregistrer_reel(
                fournisseur="anthropic", modele_ou_actor=modele, appels=1,
                tokens_in=None, tokens_out=None, cout_reel=0.0, cout_estime_engage=cout_estime,
                role=role, opportunity_id=opportunity_id, issue=ISSUE_PERDUE,
            )
            return None, None

        tokens_in_reel = getattr(resp.usage, "input_tokens", tokens_in_est)
        tokens_out_reel = getattr(resp.usage, "output_tokens", 0)
        cout_reel = estimer_cout_eur(modele, tokens_in_reel, tokens_out_reel)
        # Sous-étape 3.10, point 3 : `stop_reason == "max_tokens"` signale une
        # sortie coupée avant la fin -- souvent, mais pas toujours, cause
        # d'une sortie invalide juste en dessous. Journalisé indépendamment de
        # `issue` (une sortie tronquée reste tronquée même si elle valide par
        # chance) : voir `app.metriques` pour le taux de troncature par rôle.
        sortie_tronquee = getattr(resp, "stop_reason", None) == "max_tokens"
        if sortie_tronquee:
            logger.warning(
                "Sortie tronquée (max_tokens=%d) pour le rôle %s (modèle %s) -- envisager de relever max_tokens.",
                max_tokens, role, modele,
            )

        bloc_outil = next((b for b in resp.content if getattr(b, "type", None) == "tool_use"), None)
        if bloc_outil is None:
            issue = ISSUE_PERDUE if derniere_tentative else ISSUE_RELANCEE
            self.budget.enregistrer_reel(
                fournisseur="anthropic", modele_ou_actor=modele, appels=1,
                tokens_in=tokens_in_reel, tokens_out=tokens_out_reel, cout_reel=cout_reel,
                cout_estime_engage=cout_estime, role=role, opportunity_id=opportunity_id,
                issue=issue, sortie_tronquee=sortie_tronquee,
            )
            logger.warning("Aucun tool_use dans la réponse du modèle %s", modele)
            return None, "aucun bloc tool_use dans la réponse du modèle"

        brut_normalise = _normaliser_sortie_outil(bloc_outil.input, schema)
        try:
            resultat = schema.model_validate(brut_normalise)
        except ValidationError as exc:
            issue = ISSUE_PERDUE if derniere_tentative else ISSUE_RELANCEE
            self.budget.enregistrer_reel(
                fournisseur="anthropic", modele_ou_actor=modele, appels=1,
                tokens_in=tokens_in_reel, tokens_out=tokens_out_reel, cout_reel=cout_reel,
                cout_estime_engage=cout_estime, role=role, opportunity_id=opportunity_id,
                issue=issue, sortie_tronquee=sortie_tronquee,
            )
            logger.warning("Sortie du modèle %s invalide vs schéma %s: %s", modele, schema.__name__, exc)
            return None, str(exc)[:2000]

        issue = ISSUE_NORMALISEE if brut_normalise != bloc_outil.input else ISSUE_VALIDE
        self.budget.enregistrer_reel(
            fournisseur="anthropic", modele_ou_actor=modele, appels=1,
            tokens_in=tokens_in_reel, tokens_out=tokens_out_reel, cout_reel=cout_reel,
            cout_estime_engage=cout_estime, role=role, opportunity_id=opportunity_id,
            issue=issue, sortie_tronquee=sortie_tronquee,
        )
        return resultat, None
