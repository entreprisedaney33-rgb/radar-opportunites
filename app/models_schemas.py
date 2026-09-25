"""Schémas Pydantic : contrats d'entrée/sortie stricts pour Scout/Analyst/Critic.

Ces schémas sont la frontière de confiance du système (§5 du cahier des
charges) : tout ce qui vient d'un modèle ou d'une page collectée doit passer
par ici avant d'être stocké ou affiché. Une affirmation sans `source_ids`
valides doit être marquée `non_verifie`, jamais promue en fait.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class TypeAffirmation(str, Enum):
    OBSERVE = "observe"
    CALCULE = "calcule"
    HYPOTHESE = "hypothese"
    NON_VERIFIE = "non_verifie"


class NiveauPreuve(str, Enum):
    FAIBLE = "faible"
    MOYEN = "moyen"
    FORT = "fort"


class DecisionCritic(str, Enum):
    REJETER = "rejeter"
    A_VERIFIER = "a_verifier"
    ELIGIBLE_REVUE_HUMAINE = "eligible_revue_humaine"


class StatutOpportunite(str, Enum):
    NOUVEAU = "nouveau"
    EN_ANALYSE = "en_analyse"
    INCERTAIN = "incertain"
    REJETE = "rejete"
    A_REVOIR = "a_revoir"
    SELECTIONNE = "selectionne"


class ValeurFinanciere(BaseModel):
    """Une valeur en euros (ou autre devise) n'existe jamais seule : elle
    porte toujours son unité, sa période et sa méthode de calcul, ou elle
    est `None`."""

    montant: float
    devise: str = "EUR"
    periode: str | None = None  # ex. "par mois", "par dossier"
    methode: str  # ex. "prix affiché sur la page tarifaire du concurrent"
    source_ids: list[str] = Field(default_factory=list)


class Affirmation(BaseModel):
    """Une affirmation faite par un rôle, toujours reliée à des preuves
    réellement collectées (`source_ids`) et typée. Le moteur de score
    (scoring/engine.py) traite déjà toute affirmation OBSERVE/CALCULE sans
    `source_ids` comme "inconnue" : voir `evaluer_critere`."""

    texte: str
    type: TypeAffirmation
    source_ids: list[str] = Field(default_factory=list)


class ScoutSortie(BaseModel):
    """Sortie attendue du rôle Scout pour un groupe de signaux."""

    opportunity_candidate: str
    buyer: str
    pain: str
    ai_mechanism: str
    why_now: str
    signal_ids: list[str]
    missing_facts: list[str] = Field(default_factory=list)
    secteur: str
    cluster_id: str | None = None  # None = nouveau groupe proposé


class CritereAnalyst(BaseModel):
    nom: str
    affirmations: list[Affirmation] = Field(default_factory=list)
    inconnues: list[str] = Field(default_factory=list)


class AnalystSortie(BaseModel):
    opportunity_id: str
    criteres: list[CritereAnalyst]
    prix_observes: list[ValeurFinanciere] = Field(default_factory=list)
    marge_indicative: ValeurFinanciere | None = None
    contradictions: list[str] = Field(default_factory=list)
    prochain_test_moins_couteux: str
    niveau_preuve_global: NiveauPreuve


class Objection(BaseModel):
    texte: str
    source_ids: list[str] = Field(default_factory=list)


class CriticSortie(BaseModel):
    opportunity_id: str
    objections: list[Objection]
    faits_contestes: list[str] = Field(default_factory=list)
    recherches_supplementaires: list[str] = Field(default_factory=list)
    decision: DecisionCritic
    motif: str


class SignalNormalise(BaseModel):
    """Un signal après normalisation, prêt à être stocké et rattaché."""

    source_id: str
    texte_court: str
    categorie: str
    date_signal: datetime | None = None
    url_canonique: str
    empreinte_contenu: str
