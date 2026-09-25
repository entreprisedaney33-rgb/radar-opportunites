"""Suivi de budget d'un run : refuse un nouvel appel si le reste estimé ne
suffit pas (§4). Le suivi interne ne peut pas garantir une facture exacte en
temps réel — c'est une estimation, documentée comme telle partout où elle
s'affiche."""
from __future__ import annotations

import logging

from sqlalchemy.engine import Engine

from app.storage import repo

logger = logging.getLogger(__name__)


class BudgetDepasse(Exception):
    pass


class BudgetTracker:
    def __init__(self, engine: Engine, run_id: str, plafond_eur: float):
        self.engine = engine
        self.run_id = run_id
        self.plafond_eur = plafond_eur
        self._depense_engagee = 0.0  # inclut les estimations pas encore confirmées

    def solde_restant(self) -> float:
        return self.plafond_eur - self._depense_engagee

    def verifier_et_engager(self, cout_estime: float) -> None:
        if self._depense_engagee + cout_estime > self.plafond_eur:
            raise BudgetDepasse(
                f"Plafond de {self.plafond_eur:.2f} € dépassé : "
                f"{self._depense_engagee:.2f} € déjà engagés + {cout_estime:.2f} € estimés pour cet appel."
            )
        self._depense_engagee += cout_estime

    def enregistrer_reel(self, *, fournisseur: str, modele_ou_actor: str, appels: int, tokens_in: int | None,
                          tokens_out: int | None, cout_reel: float, cout_estime_engage: float) -> None:
        # Ajuste l'engagement de l'estimation vers le coût réel constaté.
        self._depense_engagee += cout_reel - cout_estime_engage
        repo.inserer_usage_event(
            self.engine,
            run_id=self.run_id,
            fournisseur=fournisseur,
            modele_ou_actor=modele_ou_actor,
            appels=appels,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cout=cout_reel,
        )

    def cout_total_reel(self) -> float:
        return repo.cout_total_run(self.engine, self.run_id)
