"""Suivi de budget (§4, corrigé en sous-étape 0.7 d'AMELIORATIONS.md) : refuse
un nouvel appel si le plafond du JOUR CALENDAIRE UTC (tous runs confondus)
serait dépassé. La dépense engagée est relue en base à chaque appel, jamais
gardée en mémoire depuis l'initialisation — un `run_id` différent (ex. après
un redémarrage du worker) ne fait donc jamais repartir le compteur à zéro
(voir `rapports/DIAGNOSTIC_BUDGET_2026-09-25.md`). Une petite réserve en
mémoire couvre seulement l'intervalle entre `verifier_et_engager` (avant
l'appel) et `enregistrer_reel` (après, quand la ligne existe enfin en base) :
sans elle, deux appels engagés coup sur coup avant que le premier ne soit
journalisé se verraient l'un l'autre comme "gratuits".

Second garde-fou, indépendant de toute estimation de prix : un plafond sur le
nombre d'appels au modèle approfondi (Analyst/Critic) du jour
(`config/quotas.yaml::max_appels_approfondis_par_jour`). Le premier des deux
plafonds atteint arrête les appels."""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from sqlalchemy.engine import Engine

from app.storage import repo

logger = logging.getLogger(__name__)

# Rôles dont le modèle est l'« approfondi » (Analyst/Critic) — le Scout
# utilise le modèle de tri, bien moins cher, et n'est jamais compté ici.
ROLES_APPROFONDIS = {"analyst", "critic"}


class BudgetDepasse(Exception):
    pass


def _jour_utc() -> date:
    return datetime.now(timezone.utc).date()


class BudgetTracker:
    def __init__(self, engine: Engine, run_id: str, plafond_eur: float, plafond_appels_approfondis: int):
        self.engine = engine
        self.run_id = run_id
        self.plafond_eur = plafond_eur
        self.plafond_appels_approfondis = plafond_appels_approfondis
        # Estimations "en vol" : verifier_et_engager() a réservé ce coût,
        # enregistrer_reel() ne l'a pas encore journalisé en base.
        self._reserve_estimee_eur = 0.0
        self._reserve_appels_approfondis = 0

    def depense_jour_engagee(self) -> float:
        return repo.cout_total_jour_utc(self.engine, _jour_utc()) + self._reserve_estimee_eur

    def appels_approfondis_jour_engages(self) -> int:
        return repo.nombre_appels_approfondis_jour_utc(self.engine, _jour_utc()) + self._reserve_appels_approfondis

    def solde_restant(self) -> float:
        return self.plafond_eur - self.depense_jour_engagee()

    def verifier_et_engager(self, cout_estime: float, *, role: str) -> None:
        depense_jour = self.depense_jour_engagee()
        if depense_jour + cout_estime > self.plafond_eur:
            raise BudgetDepasse(
                f"Plafond de {self.plafond_eur:.2f} €/jour dépassé : "
                f"{depense_jour:.2f} € déjà engagés aujourd'hui (UTC, tous runs confondus) "
                f"+ {cout_estime:.2f} € estimés pour cet appel."
            )
        if role in ROLES_APPROFONDIS:
            appels_jour = self.appels_approfondis_jour_engages()
            if appels_jour >= self.plafond_appels_approfondis:
                raise BudgetDepasse(
                    f"Plafond de {self.plafond_appels_approfondis} appels au modèle approfondi/jour "
                    f"atteint ({appels_jour} déjà engagés aujourd'hui, UTC)."
                )
            self._reserve_appels_approfondis += 1
        self._reserve_estimee_eur += cout_estime

    def enregistrer_reel(self, *, fournisseur: str, modele_ou_actor: str, appels: int, tokens_in: int | None,
                          tokens_out: int | None, cout_reel: float, cout_estime_engage: float, role: str,
                          opportunity_id: str | None = None) -> None:
        self._reserve_estimee_eur = max(0.0, self._reserve_estimee_eur - cout_estime_engage)
        if role in ROLES_APPROFONDIS:
            self._reserve_appels_approfondis = max(0, self._reserve_appels_approfondis - 1)
        repo.inserer_usage_event(
            self.engine,
            run_id=self.run_id,
            fournisseur=fournisseur,
            modele_ou_actor=modele_ou_actor,
            appels=appels,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cout=cout_reel,
            role=role,
            opportunity_id=opportunity_id,
        )

    def cout_total_reel(self) -> float:
        """Coût cumulé de CE run (reporting/`couts_json`) — distinct du
        plafond, qui porte sur la journée entière (voir `depense_jour_engagee`)."""
        return repo.cout_total_run(self.engine, self.run_id)
