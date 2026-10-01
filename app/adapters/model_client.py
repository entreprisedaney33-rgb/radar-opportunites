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

Sous-étape 3.13 (AMELIORATIONS.md) : le correctif ci-dessus (`"strict": True`)
a en réalité cassé TOUS les appels dès son déploiement (26/09/2026, ~13:41
UTC) -- cause identifiée dans les logs Render (`rapports/POINT_ETAPE_2026-09-27.md`) :
« Error code: 400 ... tools.0.custom: For 'object' type, 'additionalProperties'
must be explicitly set to false ». Le mode strict de l'API exige
`additionalProperties: false` (et la totalité des propriétés en `required`)
sur CHAQUE objet du schéma, récursivement -- absent du schéma brut que
`BaseModel.model_json_schema()` produit. Corrigé par
`app.adapters.schema_strict.rendre_schema_strict`, appliquée à `input_schema`
ci-dessous, en gardant le mode strict (jamais désactivé). En complément :
un disjoncteur (`app.pipeline.disjoncteur_api`) coupe court après 5 échecs
consécutifs -- voir `DisjoncteurAPIOuvert` et `appeler_structure` -- pour
qu'une panne future de ce genre arrête le pipeline plutôt que de le laisser
tourner 25 h sur des replis heuristiques sans que personne ne le remarque.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import get_origin

from pydantic import BaseModel, ValidationError

from app import config as cfg
from app.adapters.schema_strict import rendre_schema_strict
from app.config import Settings
from app.pipeline import disjoncteur_api
from app.pipeline.budget import BudgetTracker
from app.storage import repo

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


class DisjoncteurAPIOuvert(Exception):
    """Sous-étape 3.13 : levée par `appeler_structure` quand le disjoncteur
    (`app.pipeline.disjoncteur_api`) est ouvert -- aucun appel n'est tenté.
    Distincte de `AccesModeleIndisponible` (dont un rôle se remet
    volontairement, via son repli heuristique) : celle-ci n'est PAS attrapée
    par `app/roles/scout.py`/`analyst.py`/`critic.py`, elle remonte jusqu'à
    `app.pipeline.orchestrator`, qui doit s'arrêter pour ce passage plutôt
    que de laisser un rôle retomber sur son repli (§ point 2 de la
    sous-étape : « aucun dossier n'est créé ni analysé par repli pendant cet
    état »)."""
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
        forcer_outil: bool = True,
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
        (qui ne marque que la toute DERNIÈRE tentative invalide).

        `forcer_outil` (V2.5) : `True` (défaut, comportement de TOUS les rôles de la v1) impose l'appel de l'outil `repondre`
        (`tool_choice` = outil imposé). `False` laisse le choix au modèle (`auto`) : constaté au test de fumée du 2026-10-01, un appel
        forcé pousse parfois le modèle d'analyse (Sonnet 5) à renvoyer un appel d'outil FACTICE (« placeholder », 1, 2) en quelques
        centaines de jetons, alors qu'en mode auto il rédige la réponse complète. Sans appel d'outil, la tentative compte comme
        invalide (relance, puis perte) exactement comme avant."""
        if not self.settings.has_model_access:
            raise AccesModeleIndisponible("ANTHROPIC_API_KEY absente : appeler le mode démo à la place.")

        maintenant = datetime.now(timezone.utc)
        etat_disjoncteur = self._etat_disjoncteur()
        if disjoncteur_api.doit_bloquer(etat_disjoncteur, maintenant):
            raise DisjoncteurAPIOuvert(
                etat_disjoncteur.dernier_message or "Disjoncteur API ouvert (échecs consécutifs du modèle)."
            )

        erreur_precedente: str | None = None
        dernier_message_echec: str | None = None
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

            resultat, erreur_validation, message_echec = self._un_appel(
                modele=modele, prompt_systeme=prompt_systeme, prompt_utilisateur=prompt_effectif,
                schema=schema, role=role, opportunity_id=opportunity_id, max_tokens=max_tokens,
                derniere_tentative=derniere_tentative, forcer_outil=forcer_outil,
            )
            if message_echec is not None:
                dernier_message_echec = message_echec
            if resultat is not None:
                self._maj_disjoncteur(succes=True, message=None)
                return resultat
            if erreur_validation is None:
                # Succès, ou échec non lié à la validation (erreur réseau/API
                # -- déjà journalisé et jamais relancé, voir `_un_appel`).
                self._maj_disjoncteur(succes=False, message=dernier_message_echec)
                return None
            if derniere_tentative:
                self._maj_disjoncteur(succes=False, message=dernier_message_echec)
                return None
            logger.info(
                "Sortie du modèle %s invalide (role=%s) -- relance %d/%d avec l'erreur jointe.",
                modele, role, tentative + 1, MAX_TENTATIVES,
            )
            erreur_precedente = erreur_validation
        return None  # jamais atteint (la boucle renvoie toujours avant) -- garde de type

    def _etat_disjoncteur(self) -> disjoncteur_api.EtatDisjoncteurAPI:
        brut = repo.lire_disjoncteur_api(self.budget.engine)
        return disjoncteur_api.EtatDisjoncteurAPI(**brut) if brut else disjoncteur_api.ETAT_INITIAL

    def _maj_disjoncteur(self, *, succes: bool, message: str | None) -> None:
        """Sous-étape 3.13 : une seule mise à jour par appel LOGIQUE à
        `appeler_structure` (jusqu'à `MAX_TENTATIVES` tentatives réelles) --
        « 5 échecs consécutifs » compte des appels de rôle, pas des tentatives
        HTTP individuelles."""
        etat = self._etat_disjoncteur()
        if succes:
            nouvel_etat = disjoncteur_api.apres_succes(etat)
        else:
            nouvel_etat = disjoncteur_api.apres_echec(
                etat, message=message or "échec sans message", maintenant=datetime.now(timezone.utc),
            )
            if nouvel_etat.en_erreur:
                logger.error(
                    "Disjoncteur API : état « API en erreur » (depuis %s, %d échecs consécutifs) -- %s",
                    nouvel_etat.depuis, nouvel_etat.echecs_consecutifs, nouvel_etat.dernier_message,
                )
        repo.ecrire_disjoncteur_api(
            self.budget.engine,
            echecs_consecutifs=nouvel_etat.echecs_consecutifs, en_erreur=nouvel_etat.en_erreur,
            depuis=nouvel_etat.depuis, pause_jusqu_a=nouvel_etat.pause_jusqu_a,
            dernier_message=nouvel_etat.dernier_message,
        )

    def _un_appel(
        self, *, modele: str, prompt_systeme: str, prompt_utilisateur: str, schema: type[BaseModel],
        role: str, opportunity_id: str | None, max_tokens: int, derniere_tentative: bool, forcer_outil: bool = True,
    ) -> tuple[BaseModel | None, str | None, str | None]:
        """UNE tentative réelle (un appel API, un budget engagé, une ligne
        `usage_events`). Renvoie `(resultat, erreur_validation, message_echec)` :
        `erreur_validation` n'est jamais `None` seulement quand la sortie est
        invalide ET qu'une relance a un sens (voir `appeler_structure`) --
        `None` pour un succès ou pour un échec qui ne se relance jamais
        (erreur réseau/API, budget). `message_echec` (sous-étape 3.13) est
        renseigné pour TOUTE tentative invalide, réseau/API compris --
        nourrit le disjoncteur, indépendamment de la décision de relancer."""
        tokens_in_est = (len(prompt_systeme) + len(prompt_utilisateur)) // 4
        cout_estime = estimer_cout_eur(modele, tokens_in_est, max_tokens)
        self.budget.verifier_et_engager(cout_estime, role=role)  # lève BudgetDepasse si insuffisant

        client = self._get_client()
        outil = {
            "name": "repondre",
            "description": "Réponds strictement selon ce schéma JSON, sans champ supplémentaire.",
            # Sous-étape 3.13 : le schéma brut de Pydantic ne respecte pas les
            # exigences du mode strict de l'API (additionalProperties: false
            # + required exhaustif, récursivement) -- voir la docstring de
            # module et `app.adapters.schema_strict`.
            "input_schema": rendre_schema_strict(schema.model_json_schema()),
            # Sous-étape 3.10 : garantit la forme de la sortie au niveau de
            # l'API elle-même (SDK Anthropic ≥ 1.8, "structured outputs") --
            # voir la docstring de module.
            "strict": True,
        }
        try:
            parametres_outil = {"tool_choice": {"type": "tool", "name": "repondre"}} if forcer_outil else {}
            resp = client.messages.create(
                model=modele,
                max_tokens=max_tokens,
                system=prompt_systeme,
                messages=[{"role": "user", "content": prompt_utilisateur}],
                tools=[outil],
                **parametres_outil,
            )
        except Exception as exc:  # réseau, 429, etc. — journalisé, jamais relancé, pas de crash du run entier
            message = f"{type(exc).__name__}: {exc}"
            logger.error("Appel modèle échoué (%s): %s", modele, exc)
            self.budget.enregistrer_reel(
                fournisseur="anthropic", modele_ou_actor=modele, appels=1,
                tokens_in=None, tokens_out=None,
                # Sous-étape 3.13, point 4 : aucune réponse n'a été reçue, le
                # coût RÉEL est par construction inconnu -- mais 0 € cassait
                # le plafond journalier (§4 du cahier des charges : un appel
                # en échec doit compter, jamais disparaître du calcul), voir
                # Journal de cette sous-étape. On compte le coût ESTIMÉ
                # avant appel, jamais 0. Les deux branches d'échec ci-dessous
                # gardent, elles, leur coût RÉEL (une réponse a bien été
                # reçue, avec de vrais tokens) -- plus précis qu'une
                # estimation, jamais remplacé.
                cout_reel=cout_estime, cout_estime_engage=cout_estime,
                role=role, opportunity_id=opportunity_id, issue=ISSUE_PERDUE,
            )
            return None, None, message

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
            message = "aucun bloc tool_use dans la réponse du modèle"
            return None, message, message

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
            message = str(exc)[:2000]
            return None, message, message

        issue = ISSUE_NORMALISEE if brut_normalise != bloc_outil.input else ISSUE_VALIDE
        self.budget.enregistrer_reel(
            fournisseur="anthropic", modele_ou_actor=modele, appels=1,
            tokens_in=tokens_in_reel, tokens_out=tokens_out_reel, cout_reel=cout_reel,
            cout_estime_engage=cout_estime, role=role, opportunity_id=opportunity_id,
            issue=issue, sortie_tronquee=sortie_tronquee,
        )
        return resultat, None, None
