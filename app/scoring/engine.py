"""Moteur de score déterministe (§3 du cahier des charges + SCORING.md).

Le modèle ne fixe jamais la note : il ne fait qu'extraire des affirmations
typées et sourcées (`Affirmation`). C'est CE code, versionné, qui calcule le
score à partir de ces affirmations, selon les ancres décrites dans
SCORING.md. Toute modification des ancres ci-dessous doit être répercutée
dans SCORING.md dans le même commit — les deux doivent rester identiques.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.models_schemas import CritereAnalyst, TypeAffirmation

NIVEAU_FORT = {TypeAffirmation.OBSERVE, TypeAffirmation.CALCULE}


def evaluer_critere(critere: CritereAnalyst | None) -> tuple[float | None, str]:
    """Renvoie (fraction du maximum, niveau de preuve). `fraction=None`
    signifie "inconnu" — voir SCORING.md pour les 3 ancres :
    0 (absent), 0.5 (indices partiels : un seul fait fort, ou seulement des
    hypothèses), 1.0 (preuves solides : au moins 2 affirmations observées ou
    calculées, sourcées)."""
    if critere is None:
        return None, "inconnu"
    affirmations_sourcees = [a for a in critere.affirmations if a.source_ids]
    n_forts = sum(1 for a in affirmations_sourcees if a.type in NIVEAU_FORT)
    n_hypotheses = sum(1 for a in affirmations_sourcees if a.type == TypeAffirmation.HYPOTHESE)
    if n_forts >= 2:
        return 1.0, "fort"
    if n_forts == 1 or n_hypotheses >= 1:
        return 0.5, "moyen"
    return None, "inconnu"


@dataclass
class ResultatScore:
    valeurs: dict = field(default_factory=dict)
    score_brut: float = 0.0
    score_prudent: float = 0.0
    couverture_preuves: float = 0.0
    flags: list[str] = field(default_factory=list)


def calculer_score(criteres_analyst: list[CritereAnalyst], config_poids: dict) -> ResultatScore:
    par_nom = {c.nom: c for c in criteres_analyst}
    fraction_inconnue = config_poids.get("fraction_inconnue_score_prudent", 0.0)

    total_max = 0.0
    points_brut = 0.0
    max_connus = 0.0
    points_prudent = 0.0
    couverture_max = 0.0
    valeurs: dict = {}
    flags: list[str] = []

    for nom, cfg in config_poids["criteres"].items():
        max_pts = float(cfg["max"])
        total_max += max_pts
        fraction, niveau = evaluer_critere(par_nom.get(nom))

        if fraction is None:
            valeurs[nom] = {"fraction": None, "points": None, "niveau_preuve": niveau, "max": max_pts}
            points_prudent += max_pts * fraction_inconnue
            flags.append(f"{nom}: inconnu")
            continue

        pts = fraction * max_pts
        valeurs[nom] = {"fraction": fraction, "points": round(pts, 2), "niveau_preuve": niveau, "max": max_pts}
        points_brut += pts
        max_connus += max_pts
        points_prudent += pts
        couverture_max += max_pts

    score_brut = (points_brut / max_connus * 100.0) if max_connus > 0 else 0.0
    score_prudent = points_prudent  # les poids somment à 100 : déjà sur 100
    couverture = (couverture_max / total_max) if total_max > 0 else 0.0

    return ResultatScore(
        valeurs=valeurs,
        score_brut=round(score_brut, 1),
        score_prudent=round(score_prudent, 1),
        couverture_preuves=round(couverture, 2),
        flags=flags,
    )
