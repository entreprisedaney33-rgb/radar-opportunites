"""Schémas Pydantic : contrats d'entrée/sortie stricts pour Scout/Analyst/Critic.

Ces schémas sont la frontière de confiance du système (§5 du cahier des
charges) : tout ce qui vient d'un modèle ou d'une page collectée doit passer
par ici avant d'être stocké ou affiché. Une affirmation sans `source_ids`
valides doit être marquée `non_verifie`, jamais promue en fait.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

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
    # Sous-étape 3.4 : trouvée par le Scout, l'Enquêteur a tourné (avec ou
    # sans nouvelle source trouvée) -- prête pour l'Analyst. Voir
    # app/pipeline/orchestrator.py::_phase_enquete.
    ENQUETE_TERMINEE = "enquete_terminee"
    EN_ANALYSE = "en_analyse"
    INCERTAIN = "incertain"
    REJETE = "rejete"
    A_REVOIR = "a_revoir"
    SELECTIONNE = "selectionne"
    # Sous-étape 3.13 : dossier créé uniquement par repli sans modèle pendant
    # la panne du 26/09/2026 (voir app.reprise) -- retiré des métriques et de
    # la liste Jarvis tant qu'il n'est pas repassé par un vrai Scout.
    A_REPRENDRE = "a_reprendre"
    # Sous-étape 4.1 : dernier score < 50 (règle actuelle) -- conservé en
    # base, retiré des listes et des requêtes courantes, jamais supprimé.
    ARCHIVE_FAIBLE = "archive_faible"


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
    """Sortie attendue du rôle Scout pour un groupe de signaux.

    `secteur`/`secteur_citation` (sous-étape 2.2) : une VRAIE proposition du
    Scout, appuyée d'une citation mot pour mot du signal — pas un écho du
    secteur déjà donné en entrée. Optionnels : `None` est préférable à une
    citation approximative (voir le prompt système). C'est
    `app.pipeline.normalisation.inferer_secteur` (2.1), branchée dans
    `app/pipeline/orchestrator.py`, qui vérifie la citation et décide si le
    secteur proposé est réellement retenu (`citation_verifiee`) ou non.

    `mots_cles_en`/`mots_cles_fr` (sous-étape 3.11) : 3 à 6 mots courts
    (lettres/chiffres/espaces uniquement — jamais un opérateur de recherche,
    jamais une guillemet), qui remplacent la phrase entière (`pain`) dans les
    requêtes de l'Enquêteur — un moteur de recherche ne répond pas à une
    phrase de 200 caractères. Optionnels, comme `secteur`/`secteur_citation` :
    `None` vaut mieux qu'une valeur bricolée pour tenir le format. Validés
    par du code, jamais fait confiance tels quels :
    `app.pipeline.mots_cles.valider_mots_cles`, appelée dans
    `app/pipeline/orchestrator.py` avant toute persistance."""

    opportunity_candidate: str
    buyer: str
    pain: str
    ai_mechanism: str
    why_now: str
    signal_ids: list[str]
    missing_facts: list[str] = Field(default_factory=list)
    secteur: str | None = None
    secteur_citation: str | None = None
    cluster_id: str | None = None  # None = nouveau groupe proposé
    mots_cles_en: str | None = None
    mots_cles_fr: str | None = None


class InvestissementInitial(str, Enum):
    MOINS_DE_5K = "moins_de_5k"
    DE_5K_A_20K = "5k_a_20k"
    DE_20K_A_100K = "20k_a_100k"
    PLUS_DE_100K = "plus_de_100k"


class DelaiPremierRevenu(str, Enum):
    MOINS_DE_3_MOIS = "moins_de_3_mois"
    DE_3_A_12_MOIS = "3_a_12_mois"
    PLUS_DE_12_MOIS = "plus_de_12_mois"


class Marche(str, Enum):
    ACCESSIBLE_DEPUIS_FRANCE = "accessible_depuis_france"
    EUROPE = "europe"
    ETATS_UNIS_SEULEMENT = "etats_unis_seulement"
    AUTRE = "autre"


class Competence(str, Enum):
    DEV_IA = "dev_ia"
    VENTE = "vente"
    REGLEMENTAIRE_LOURD = "reglementaire_lourd"
    MATERIEL_INDUSTRIEL = "materiel_industriel"
    RESEAU_SPECIFIQUE = "reseau_specifique"


class TailleProbleme(str, Enum):
    NICHE_LOCALE = "niche_locale"
    SEGMENT_PME = "segment_pme"
    MARCHE_NATIONAL_LARGE = "marche_national_large"
    SYSTEMIQUE = "systemique"


class ChampInvestissement(BaseModel):
    valeur: InvestissementInitial
    justification: str  # une phrase


class ChampDelai(BaseModel):
    valeur: DelaiPremierRevenu
    justification: str


class ChampMarche(BaseModel):
    valeur: Marche
    justification: str


class ChampCompetences(BaseModel):
    valeurs: list[Competence]
    justification: str


class ChampTaille(BaseModel):
    valeur: TailleProbleme
    justification: str


class FaisabiliteSortie(BaseModel):
    """Sous-étape 4.1 : faisabilité pour Mathéo (seul, depuis la France),
    bloc structuré SUPPLÉMENTAIRE de l'Analyst. Toujours une HYPOTHÈSE
    (`type` figé) : ne touche JAMAIS au score de preuve
    (`app.scoring.engine`, qui ignore ce bloc). `accessible_solo` et le motif
    d'exclusion n'en sont pas des champs : le CODE les déduit
    (`app.faisabilite.evaluer_accessibilite`)."""

    type: Literal["hypothese"] = "hypothese"
    investissement_initial: ChampInvestissement
    delai_premier_revenu: ChampDelai
    marche: ChampMarche
    competences: ChampCompetences
    taille_du_probleme: ChampTaille


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
    # Sous-étape 4.1 : bloc de faisabilité (hypothèse, jamais dans le score).
    faisabilite: FaisabiliteSortie | None = None


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
