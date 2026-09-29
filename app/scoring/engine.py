"""Moteur de score déterministe (§3 du cahier des charges + SCORING.md).

Le modèle ne fixe jamais la note : il ne fait qu'extraire des affirmations
typées et sourcées (`Affirmation`). C'est CE code, versionné, qui calcule le
score à partir de ces affirmations, selon les ancres décrites dans
SCORING.md. Toute modification des ancres ci-dessous doit être répercutée
dans SCORING.md dans le même commit — les deux doivent rester identiques.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from app.models_schemas import CritereAnalyst, TypeAffirmation

NIVEAU_FORT = {TypeAffirmation.OBSERVE, TypeAffirmation.CALCULE}

# Sous-étape 3.17 : seconds niveaux de domaine où le « domaine de base » compte
# trois étiquettes (bbc.co.uk, site.com.au) et non deux.
_SOUS_NIVEAUX_COMPOSES = {"co", "com", "org", "net", "gov", "ac", "edu"}


@dataclass(frozen=True)
class InfoSource:
    """Ce dont le moteur a besoin pour juger de l'indépendance de deux
    sources (sous-étape 3.17) : le domaine (voir `domaine_de_base`) et le
    fait d'être le signal d'origine du dossier (celui que le Scout a lu,
    preuve « Scout: … »)."""

    domaine: str
    origine: bool = False


def domaine_de_base(nom_hote: str) -> str:
    """`www.lemonde.fr` et `abonne.lemonde.fr` -> `lemonde.fr` ; `bbc.co.uk`
    reste `bbc.co.uk`. Volontairement simple (pas de liste de suffixes
    publics) : suffisant pour dire que deux pages du même éditeur ne sont
    pas deux sources indépendantes."""
    hote = nom_hote.strip().lower().split(":")[0].rstrip(".")
    etiquettes = [e for e in hote.split(".") if e]
    if len(etiquettes) <= 2:
        return ".".join(etiquettes)
    if len(etiquettes[-1]) == 2 and etiquettes[-2] in _SOUS_NIVEAUX_COMPOSES:
        return ".".join(etiquettes[-3:])
    return ".".join(etiquettes[-2:])


def sources_distinctes(id_a: str, id_b: str, sources: Mapping[str, InfoSource]) -> bool:
    """Deux sources sont distinctes si ce sont deux sources différentes ET
    (leurs domaines diffèrent OU l'une est le signal d'origine et pas
    l'autre). Une source absente de `sources` ne peut pas être jugée : elle
    n'est jamais distincte (prudence, pas de bénéfice du doute)."""
    if id_a == id_b:
        return False
    info_a, info_b = sources.get(id_a), sources.get(id_b)
    if info_a is None or info_b is None:
        return False
    return domaine_de_base(info_a.domaine) != domaine_de_base(info_b.domaine) or info_a.origine != info_b.origine


def _deux_faits_sur_deux_sources_distinctes(forts, sources: Mapping[str, InfoSource]) -> bool:
    """Au moins deux affirmations (observées/calculées, sourcées) dont l'une
    cite une source et l'autre une source DISTINCTE de la première."""
    for i, a in enumerate(forts):
        for b in forts[i + 1:]:
            if any(sources_distinctes(sa, sb, sources) for sa in a.source_ids for sb in b.source_ids):
                return True
    return False


def evaluer_critere(
    critere: CritereAnalyst | None, sources: Mapping[str, InfoSource],
) -> tuple[float | None, str]:
    """Renvoie (fraction du maximum, niveau de preuve). `fraction=None`
    signifie "inconnu" — voir SCORING.md pour les 3 ancres :
    0 (absent), 0.5 (indices partiels : un seul fait fort, deux faits forts
    sur la même source, ou seulement des hypothèses), 1.0 (preuves solides :
    au moins 2 affirmations observées ou calculées, sourcées, citant deux
    sources distinctes -- sous-étape 3.17). Une hypothèse (ou une inférence,
    qui n'est qu'une hypothèse) ne compte jamais comme fait fort.

    `sources` : `source_id -> InfoSource` pour les sources du dossier.
    Obligatoire, pour qu'un appelant qui l'oublierait échoue bruyamment
    plutôt que de retomber en silence sur l'ancienne règle."""
    if critere is None:
        return None, "inconnu"
    affirmations_sourcees = [a for a in critere.affirmations if a.source_ids]
    forts = [a for a in affirmations_sourcees if a.type in NIVEAU_FORT]
    n_forts = len(forts)
    n_hypotheses = sum(1 for a in affirmations_sourcees if a.type == TypeAffirmation.HYPOTHESE)
    if n_forts >= 2 and _deux_faits_sur_deux_sources_distinctes(forts, sources):
        return 1.0, "fort"
    if n_forts >= 1 or n_hypotheses >= 1:
        return 0.5, "moyen"
    return None, "inconnu"


@dataclass
class ResultatScore:
    valeurs: dict = field(default_factory=dict)
    score_brut: float = 0.0
    score_prudent: float = 0.0
    couverture_preuves: float = 0.0
    flags: list[str] = field(default_factory=list)


def calculer_score(
    criteres_analyst: list[CritereAnalyst], config_poids: dict, sources: Mapping[str, InfoSource],
) -> ResultatScore:
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
        fraction, niveau = evaluer_critere(par_nom.get(nom), sources)

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
