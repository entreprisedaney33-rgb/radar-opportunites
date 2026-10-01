"""Score v2 d'une fiche secteur x tâche (V2.5, RADAR-V2.md §5) -- CALCULÉ PAR DU CODE, jamais par un modèle.

La règle est écrite dans `SCORING-V2.md` et ses nombres dans `config/fiches.yaml` ; ce module n'invente aucun seuil.
Fonctions pures : aucune lecture de base, aucun réseau, aucun modèle.

Deux scores, comme en v1 :
- `score_brut` : estimations centrales, dates « à confirmer » comptées à demi-tarif ;
- `score_prudent` : estimations PESSIMISTES (borne basse de l'intervalle de confiance de la part), dates fermes seulement.
Un critère dont la preuve manque vaut 0 dans les deux et figure dans `preuves_manquantes` (jamais deviné).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from app import config as cfg
from app.faisabilite import ResultatAccessibilite
from app.models_schemas import DelaiPremierRevenu
from app.referentiels import Declencheur, TOUS_SECTEURS


@dataclass
class Critere:
    nom: str
    maximum: float
    points_brut: float
    points_prudent: float
    statut: str  # prouve | partiel | non_evalue | absent
    preuve: str  # phrase courte, chiffres à l'appui

    def en_dict(self) -> dict[str, Any]:
        return {"nom": self.nom, "max": self.maximum, "points_brut": self.points_brut, "points_prudent": self.points_prudent,
                "statut": self.statut, "preuve": self.preuve}


@dataclass
class ResultatScore:
    criteres: list[Critere]
    score_brut: float
    score_prudent: float
    preuves_manquantes: list[str] = field(default_factory=list)
    version: str = ""
    avertissements: list[str] = field(default_factory=list)  # ce qui rend le score moins sûr (ex. rattachement déclencheur-tâche provisoire)

    def en_dict(self) -> dict[str, Any]:
        return {"version": self.version, "criteres": [c.en_dict() for c in self.criteres], "score_brut": self.score_brut,
                "score_prudent": self.score_prudent, "preuves_manquantes": self.preuves_manquantes, "avertissements": self.avertissements}


def _r(x: float) -> float:
    return round(x, 1)


def _palier(valeur: float, paliers: list[list[float]]) -> float:
    """Points du premier palier [seuil, points] dont le seuil est atteint (paliers triés du plus haut au plus bas)."""
    for seuil, points in paliers:
        if valeur >= seuil:
            return float(points)
    return 0.0


def points_volume(n: float, c: dict) -> float:
    """Échelle logarithmique : 0 sous `offres_min`, `points_volume_max` à partir de `offres_plein`."""
    if n < c["offres_min"]:
        return 0.0
    rapport = math.log(n / c["offres_min"]) / math.log(c["offres_plein"] / c["offres_min"])
    return _r(c["points_volume_max"] * min(1.0, max(0.0, rapport)))


def estimations_basses(agregat: dict[str, Any]) -> tuple[float, float]:
    """(offres estimées au pire, offres de la zone estimées au pire) : on remplace la part par la borne basse de son
    intervalle de confiance. À 100 % de couverture, l'intervalle est la part elle-même et rien ne change."""
    part, bas = agregat["part_offres_tache"], agregat["part_ic95_bas"]
    facteur = (bas / part) if part > 0 else 0.0
    return agregat["nb_offres_tache_estime"] * facteur, agregat["nb_offres_tache_zone_estime"] * facteur


def critere_demande(agregat: dict[str, Any], c: dict) -> Critere:
    central = points_volume(agregat["nb_offres_tache_estime"], c)
    n_bas, _ = estimations_basses(agregat)
    prudent = points_volume(n_bas, c)
    bonus = 0.0
    morceaux = [f"{agregat['nb_offres_tache_estime']} offres estimées sur {agregat['fenetre_jours']} jours "
                f"(au pire {math.floor(n_bas)}) -> {central} pts bruts / {prudent} pts prudents"]
    salaire = agregat.get("salaire_median_annuel_eur")
    if salaire is not None and agregat["nb_salaires"] >= c["salaire_n_min"] and salaire >= c["salaire_seuil_eur"]:
        bonus += c["points_salaire"]
        morceaux.append(f"salaire médian {round(salaire)} € >= {c['salaire_seuil_eur']} € (+{c['points_salaire']})")
    if agregat["tendance_statut"] == "calculee" and (agregat["tendance_3_mois_pct"] or 0) > 0:
        bonus += c["points_tendance"]
        morceaux.append(f"tendance 3 mois {agregat['tendance_3_mois_pct']} % (+{c['points_tendance']})")
    elif agregat["tendance_statut"] != "calculee":
        morceaux.append(f"tendance non calculée ({agregat['tendance_statut']}, accumulation {agregat['accumulation_jours']} j)")
    haut = min(c["max"], central + bonus)
    bas = min(c["max"], prudent + bonus)
    return Critere("demande", c["max"], _r(haut), _r(bas), "prouve", "; ".join(morceaux))


def critere_proximite(agregat: dict[str, Any], etablissements_rayon: int, c: dict) -> Critere:
    pts_etab = _palier(etablissements_rayon, c["paliers_etablissements"])
    _, local_bas = estimations_basses(agregat)
    local_brut = _palier(agregat["nb_offres_tache_zone_estime"], c["paliers_offres_locales"])
    local_prudent = _palier(local_bas, c["paliers_offres_locales"])
    preuve = (f"{etablissements_rayon} établissements listés dans le rayon -> {pts_etab} pts ; offres de la tâche dans la zone "
              f"estimées {agregat['nb_offres_tache_zone_estime']} (au pire {math.floor(local_bas)}) -> {local_brut} / {local_prudent} pts")
    return Critere("proximite", c["max"], _r(min(c["max"], pts_etab + local_brut)), _r(min(c["max"], pts_etab + local_prudent)),
                   "prouve", preuve)


def declencheurs_du_secteur(code_naf: str, declencheurs: list[Declencheur], aujourdhui: date, c: dict, tache_id: str | None = None) -> list[Declencheur]:
    """Obligations touchant ce secteur ET cette tâche (`tache_id` : une obligation sans `taches` touche toutes les tâches ; `None` =
    pas de filtre par tâche), datées de moins de `retro_mois` mois dans le passé à `horizon_mois` mois dans l'avenir."""
    debut = aujourdhui - timedelta(days=round(c["retro_mois"] * 30.4))
    fin = aujourdhui + timedelta(days=round(c["horizon_mois"] * 30.4))
    return sorted(
        (d for d in declencheurs if (TOUS_SECTEURS in d.secteurs or code_naf in d.secteurs) and debut <= d.date <= fin
         and (tache_id is None or not d.taches or tache_id in d.taches)),
        key=lambda d: d.date,
    )


def critere_declencheur(agregat: dict[str, Any], datees: list[Declencheur], c: dict) -> Critere:
    fermes = [d for d in datees if d.precision_date == "ferme"]
    a_confirmer = [d for d in datees if d.precision_date == "a_confirmer"]
    brut = c["points_date_ferme"] if fermes else (c["points_date_a_confirmer"] if a_confirmer else 0.0)
    prudent = c["points_date_ferme"] if fermes else 0.0
    preuve = ""
    statut = "absent"
    if datees:
        statut = "prouve" if fermes else "partiel"
        preuve = "; ".join(f"{d.libelle[:70]} ({d.date.isoformat()}, date {d.precision_date})" for d in datees[:3])
    tendance = agregat["tendance_3_mois_pct"] if agregat["tendance_statut"] == "calculee" else None
    if tendance is not None and tendance > c["tendance_seuil_pct"]:
        brut, prudent = max(brut, c["points_tendance"]), max(prudent, c["points_tendance"])
        statut = "prouve"
        preuve = (preuve + "; " if preuve else "") + f"tendance des offres +{tendance} % (> {c['tendance_seuil_pct']} %)"
    if not preuve:
        preuve = "aucune obligation datée dans la fenêtre et aucune tendance calculée"
    return Critere("declencheur", c["max"], _r(brut), _r(prudent), statut, preuve)


def critere_concurrence(concurrence: dict[str, Any] | None, c: dict) -> Critere:
    """`concurrence` : None tant que V2.6 n'a pas évalué. Sinon {outils_dedies: int, service_local: bool}."""
    if concurrence is None:
        return Critere("concurrence", c["max"], 0.0, 0.0, "non_evalue", "concurrence non évaluée (recherche web de V2.6)")
    if concurrence.get("service_local"):
        pts, texte = c["points_service_local_etabli"], "service local établi"
    elif concurrence.get("outils_dedies", 0) > 0:
        pts, texte = c["points_outils_sans_service_local"], f"{concurrence['outils_dedies']} outil(s) dédié(s), aucun service local"
    else:
        pts, texte = c["points_aucun_outil"], "aucun outil dédié identifié (marché non prouvé)"
    return Critere("concurrence", c["max"], float(pts), float(pts), "prouve", texte)


def critere_accessibilite(acces: ResultatAccessibilite | None, delai: DelaiPremierRevenu | str | None, c: dict) -> Critere:
    if acces is None or acces.accessible_solo is None or delai is None:
        return Critere("accessibilite", c["max"], 0.0, 0.0, "non_evalue", "faisabilité non évaluée par l'Analyste")
    if acces.accessible_solo is False:
        return Critere("accessibilite", c["max"], 0.0, 0.0, "prouve", f"porte fermée : {acces.motif_exclusion}")
    cle = delai.value if isinstance(delai, DelaiPremierRevenu) else str(delai)
    pts = float(c["points_par_delai"].get(cle, 0))
    return Critere("accessibilite", c["max"], pts, pts, "prouve", f"accessible ; premier revenu : {cle} (hypothèse de l'Analyste)")


def calculer_score(
    agregat: dict[str, Any], *, etablissements_rayon: int, declencheurs: list[Declencheur], aujourdhui: date,
    acces: ResultatAccessibilite | None, delai: DelaiPremierRevenu | str | None, concurrence: dict[str, Any] | None = None,
    reglages: dict | None = None, rattachement_provisoire: bool = False,
) -> ResultatScore:
    cfg_f = reglages or cfg.fiches()
    s = cfg_f["score"]
    datees = declencheurs_du_secteur(agregat["code_naf"], declencheurs, aujourdhui, s["declencheur"], agregat["tache_id"])
    criteres = [
        critere_demande(agregat, s["demande"]),
        critere_proximite(agregat, etablissements_rayon, s["proximite"]),
        critere_declencheur(agregat, datees, s["declencheur"]),
        critere_concurrence(concurrence, s["concurrence"]),
        critere_accessibilite(acces, delai, s["accessibilite"]),
    ]
    manquantes = [f"{c.nom} : {c.preuve}" for c in criteres if c.statut in ("non_evalue", "absent")]
    avertissements: list[str] = []
    if criteres[2].points_brut > 0 and rattachement_provisoire:
        avertissements.append("critère « déclencheur » : rattachement obligation-tâche PROVISOIRE (accepté le 2026-10-01, à relire à V2.9 sur fiches réelles)")
    return ResultatScore(
        criteres=criteres, score_brut=_r(sum(c.points_brut for c in criteres)), score_prudent=_r(sum(c.points_prudent for c in criteres)),
        preuves_manquantes=manquantes, version=str(cfg_f["version"]), avertissements=avertissements,
    )
