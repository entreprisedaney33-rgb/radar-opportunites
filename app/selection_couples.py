"""Sélection des couples secteur x tâche candidats à une fiche (V2.5, RADAR-V2.md).

Seuils dans `config/fiches.yaml::selection`. Ils sont posés sur les PARTS EXTRAPOLÉES et leur intervalle de confiance, pas
sur les comptes bruts de l'échantillon : avec 60 offres étiquetées par code NAF, c'est la borne basse de l'intervalle de
Wilson de la part (`part_ic95_bas`) qui dit ce qu'on peut affirmer au minimum, et le nombre d'offres est celui du stock
entier (part x offres collectées du secteur), pas celui de l'échantillon.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from app import config as cfg


@dataclass
class Candidat:
    agregat: dict[str, Any]
    etablissements_rayon: int
    offres_estimees_bas: int

    @property
    def cle(self) -> tuple[str, str]:
        return self.agregat["code_naf"], self.agregat["tache_id"]


@dataclass
class Rejet:
    code_naf: str
    tache_id: str
    raisons: list[str] = field(default_factory=list)


def evaluer_couple(agregat: dict[str, Any], etablissements_rayon: int, reglages: dict | None = None) -> tuple[Candidat | None, Rejet | None]:
    """Candidat si TOUS les seuils tiennent ; sinon un rejet avec TOUTES les raisons (pas seulement la première)."""
    sel = (reglages or cfg.fiches())["selection"]
    recensement = agregat["couverture_etiquetage"] >= 0.999
    part, bas = agregat["part_offres_tache"], agregat["part_ic95_bas"]
    n_bas = math.floor(agregat["nb_offres_tache_estime"] * (bas / part)) if part > 0 else 0
    raisons: list[str] = []
    if agregat.get("motif_exclusion"):
        raisons.append(f"couple exclu : {agregat['motif_exclusion']}")
    if not recensement and agregat["nb_offres_secteur"] < sel["offres_etiquetees_min"]:
        raisons.append(f"échantillon trop petit : {agregat['nb_offres_secteur']} offres étiquetées < {sel['offres_etiquetees_min']}")
    if bas < sel["part_ic95_bas_min"]:
        raisons.append(f"part trop incertaine : borne basse {bas:.1%} < {sel['part_ic95_bas_min']:.1%}")
    if agregat["nb_offres_tache_estime"] < sel["offres_estimees_min"]:
        raisons.append(f"{agregat['nb_offres_tache_estime']} offres estimées < {sel['offres_estimees_min']}")
    if n_bas < sel["offres_estimees_bas_min"]:
        raisons.append(f"{n_bas} offres estimées au pire < {sel['offres_estimees_bas_min']}")
    if etablissements_rayon < sel["etablissements_rayon_min"]:
        raisons.append(f"{etablissements_rayon} établissements dans le rayon < {sel['etablissements_rayon_min']}")
    if raisons:
        return None, Rejet(agregat["code_naf"], agregat["tache_id"], raisons)
    return Candidat(agregat, etablissements_rayon, n_bas), None


def selectionner(agregats: list[dict[str, Any]], etablissements_par_code: dict[str, int], reglages: dict | None = None) -> tuple[list[Candidat], list[Rejet]]:
    candidats: list[Candidat] = []
    rejets: list[Rejet] = []
    for a in agregats:
        candidat, rejet = evaluer_couple(a, etablissements_par_code.get(a["code_naf"], 0), reglages)
        (candidats if candidat else rejets).append(candidat or rejet)
    candidats.sort(key=lambda c: (-c.agregat["nb_offres_tache_estime"], c.cle))
    return candidats, rejets
