"""Faisabilité pour Mathéo (sous-étape 4.1 d'AMELIORATIONS.md).

L'Analyst (ou la reprise, `app.roles.faisabilite`) propose un bloc structuré
d'HYPOTHÈSES (`FaisabiliteSortie`) ; c'est CE code qui en déduit le drapeau
`accessible_solo` et, sinon, un motif d'exclusion lisible, d'après les
critères de `config/faisabilite.yaml`. Rien ici ne touche au score de
preuve : `app.scoring.engine` ignore ce bloc.
"""
from __future__ import annotations

from dataclasses import dataclass

from app import config as cfg
from app.models_schemas import FaisabiliteSortie

MOTIF_NON_EVALUEE = "faisabilité non évaluée"

_LIBELLES_INVESTISSEMENT = {
    "moins_de_5k": "moins de 5 k€", "5k_a_20k": "5 à 20 k€",
    "20k_a_100k": "20 à 100 k€", "plus_de_100k": "plus de 100 k€",
}
_LIBELLES_DELAI = {
    "moins_de_3_mois": "moins de 3 mois", "3_a_12_mois": "3 à 12 mois", "plus_de_12_mois": "plus de 12 mois",
}
_LIBELLES_MARCHE = {
    "accessible_depuis_france": "accessible depuis la France", "europe": "Europe",
    "etats_unis_seulement": "États-Unis seulement", "autre": "hors Europe",
}
_LIBELLES_COMPETENCE = {
    "reglementaire_lourd": "réglementaire lourd", "materiel_industriel": "matériel industriel",
    "dev_ia": "développement IA", "vente": "vente", "reseau_specifique": "réseau spécifique",
}
_LIBELLES_TAILLE = {
    "niche_locale": "niche locale", "segment_pme": "segment PME",
    "marche_national_large": "marché national large", "systemique": "problème systémique",
}


@dataclass(frozen=True)
class ResultatAccessibilite:
    accessible_solo: bool | None  # None = pas de bloc de faisabilité (inconnu)
    motif_exclusion: str | None  # None si accessible (ou inconnu : voir MOTIF_NON_EVALUEE)


def evaluer_accessibilite(
    bloc: FaisabiliteSortie | dict | None, config: dict | None = None,
) -> ResultatAccessibilite:
    """`accessible_solo` = True seulement si TOUS les critères de
    `config/faisabilite.yaml` tiennent ; sinon False avec les motifs (tous,
    pas seulement le premier) joints par « ; ». Sans bloc : (None, motif
    « non évaluée ») -- jamais True par défaut."""
    if bloc is None:
        return ResultatAccessibilite(None, MOTIF_NON_EVALUEE)
    if not isinstance(bloc, FaisabiliteSortie):
        bloc = FaisabiliteSortie.model_validate(bloc)
    cfg_f = config if config is not None else cfg.faisabilite()

    motifs: list[str] = []
    invest = bloc.investissement_initial.valeur.value
    if invest not in cfg_f["investissements_acceptes"]:
        motifs.append(f"investissement trop élevé ({_LIBELLES_INVESTISSEMENT[invest]})")
    delai = bloc.delai_premier_revenu.valeur.value
    if delai not in cfg_f["delais_acceptes"]:
        motifs.append(f"premier revenu trop lointain ({_LIBELLES_DELAI[delai]})")
    marche = bloc.marche.valeur.value
    if marche not in cfg_f["marches_acceptes"]:
        motifs.append(f"marché non accessible ({_LIBELLES_MARCHE[marche]})")
    for competence in bloc.competences.valeurs:
        if competence.value in cfg_f["competences_refusees"]:
            motifs.append(f"exige {_LIBELLES_COMPETENCE[competence.value]}")
    taille = bloc.taille_du_probleme.valeur.value
    if taille in cfg_f["tailles_refusees"]:
        motifs.append(f"{_LIBELLES_TAILLE[taille]}")

    if motifs:
        return ResultatAccessibilite(False, "; ".join(motifs))
    return ResultatAccessibilite(True, None)
