"""Agrégation de la demande par secteur x tâche (V2.4, RADAR-V2.md).

    python -m app.cli agreger

Pour chaque code NAF retenu et chaque tâche mentionnée par au moins une offre : combien d'offres actives créées dans
les 90 derniers jours (le STOCK -- décision de Mathéo du 2026-10-01) la mentionnent, en France et dans les
départements de la zone, quelle part des offres du secteur cela représente, quel salaire médian.

Règles qui évitent de se raconter des histoires :
- le dénominateur est le nombre d'offres du secteur dont l'étiquetage est COMPLET (statut `ok`) ; la
  `couverture_etiquetage` dit quelle part des offres du secteur cela représente ;
- la TENDANCE (offres créées sur les 90 derniers jours contre les 90 précédents) n'est calculée que si la collecte a
  accumulé au moins `accumulation_min_tendance_jours` jours : l'API ne sert que les offres actives, les anciennes
  disparaissent, et comparer deux fenêtres sur une accumulation trop courte donnerait un chiffre faux ;
- chaque ligne porte `accumulation_jours`, la durée réelle d'accumulation de la collecte du code ;
- quand l'étiquetage ne porte que sur un ÉCHANTILLON du secteur (couverture < 100 %, plafond par code de
  `config/etiquetage.yaml`), la part est une estimation : on écrit son intervalle de confiance de Wilson à 95 % et le
  nombre d'offres EXTRAPOLÉ (part x toutes les offres collectées du secteur) à côté du nombre observé ; à 100 % de
  couverture, l'estimé égale l'observé et l'intervalle se réduit à la part ;
- une ligne n'est ajoutée que si un de ses chiffres a changé depuis la dernière (instantanés append-only).
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from app import config as cfg
from app import referentiels
from app.storage import repo
from app.storage.schema import collectes_offres, offres_emploi, offres_etiquetage, offres_taches

PROVENANCE_CITATION = "citation_verifiee"
CHAMPS_COMPARES = (
    "nb_offres_tache_estime", "nb_offres_tache_zone_estime", "part_ic95_bas", "part_ic95_haut", "nb_offres_secteur", "nb_offres_secteur_total", "nb_offres_tache", "nb_offres_tache_zone", "nb_citation_verifiee",
    "nb_lexique_seul", "salaire_median_annuel_eur", "nb_salaires", "tendance_3_mois_pct", "tendance_statut", "motif_exclusion",
)


@dataclass
class ResumeAgregation:
    secteurs_avec_offres: int = 0
    couples_calcules: int = 0
    lignes_ajoutees: int = 0
    lignes_inchangees: int = 0
    secteurs_exclus_ignores: int = 0


def _aware(valeur: datetime) -> datetime:
    return valeur if valeur.tzinfo else valeur.replace(tzinfo=timezone.utc)


def _milieu_salaire(bas: float | None, haut: float | None) -> float | None:
    if bas is None:
        return None
    return (bas + (haut if haut is not None else bas)) / 2


def intervalle_wilson(succes: int, total: int, *, z: float = 1.96) -> tuple[float, float]:
    """Intervalle de confiance de Wilson (95 % par défaut) d'une proportion ; (0, 0) si `total` est nul."""
    if total <= 0:
        return 0.0, 0.0
    p = succes / total
    denominateur = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominateur
    marge = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominateur
    return max(0.0, centre - marge), min(1.0, centre + marge)


def calculer_agregats(engine: Engine, *, maintenant: datetime | None = None) -> ResumeAgregation:
    reglages = cfg.etiquetage()
    secteurs = referentiels.secteurs_tpe()
    taches = referentiels.taches()
    zone_deps = set(referentiels.zone().departements_zone())
    quand = maintenant or datetime.now(timezone.utc)
    fenetre = int(reglages["fenetre_stock_jours"])
    debut_stock = quand - timedelta(days=fenetre)
    debut_precedent = quand - timedelta(days=2 * fenetre)
    resume = ResumeAgregation()

    with engine.connect() as cx:
        offres = cx.execute(
            select(offres_emploi.c.id_offre, offres_emploi.c.code_naf, offres_emploi.c.departement, offres_emploi.c.date_creation,
                   offres_emploi.c.salaire_annuel_min_eur, offres_emploi.c.salaire_annuel_max_eur,
                   offres_etiquetage.c.statut)
            .select_from(offres_emploi.outerjoin(offres_etiquetage, offres_emploi.c.id_offre == offres_etiquetage.c.id_offre))
            .where(offres_emploi.c.naf_version == secteurs.naf_version, offres_emploi.c.code_naf.is_not(None),
                   offres_emploi.c.date_creation >= debut_precedent)
        ).mappings().all()
        mentions = defaultdict(lambda: defaultdict(set))  # id_offre -> tache_id -> provenances
        for id_offre, tache_id, provenance in cx.execute(select(offres_taches.c.id_offre, offres_taches.c.tache_id, offres_taches.c.provenance)):
            mentions[id_offre][tache_id].add(provenance)
        premieres = {code: _aware(t) for code, t in cx.execute(
            select(collectes_offres.c.code_naf, func.min(collectes_offres.c.horodatage)).where(
                collectes_offres.c.naf_version == secteurs.naf_version).group_by(collectes_offres.c.code_naf))}

    par_secteur = defaultdict(list)
    for o in offres:
        par_secteur[o["code_naf"]].append(o)
    codes_retenus = {s.code for s in secteurs.non_exclus()}
    derniers = repo.dernier_agregat_par_couple(engine, secteurs.naf_version)
    a_ecrire: list[dict] = []
    minimum_tendance = int(reglages["accumulation_min_tendance_jours"])

    for code, liste in sorted(par_secteur.items()):
        if code not in codes_retenus:
            resume.secteurs_exclus_ignores += 1
            continue
        resume.secteurs_avec_offres += 1
        stock = [o for o in liste if _aware(o["date_creation"]) >= debut_stock]
        precedent = [o for o in liste if _aware(o["date_creation"]) < debut_stock]
        if not stock:
            continue
        complets = [o for o in stock if o["statut"] == "ok"]
        if not complets:
            continue
        accumulation = max(0, (quand - premieres[code]).days) if code in premieres else 0
        total_zone = sum(1 for o in stock if o["departement"] in zone_deps)
        couverture = len(complets) / len(stock)
        # Offres mentionnant chaque tâche (France), parmi les offres complètement étiquetées du stock.
        par_tache = defaultdict(list)
        for o in complets:
            for tache_id in mentions.get(o["id_offre"], {}):
                par_tache[tache_id].append(o)
        for tache_id, concernees in sorted(par_tache.items()):
            salaires = [m for o in concernees if (m := _milieu_salaire(o["salaire_annuel_min_eur"], o["salaire_annuel_max_eur"])) is not None]
            avec_citation = sum(1 for o in concernees if PROVENANCE_CITATION in mentions[o["id_offre"]][tache_id])
            if accumulation >= minimum_tendance:
                avant = sum(1 for o in precedent if o["statut"] == "ok" and tache_id in mentions.get(o["id_offre"], {}))
                if avant:
                    tendance, statut_tendance = round(100 * (len(concernees) - avant) / avant, 1), "calculee"
                else:
                    tendance, statut_tendance = None, "sans_reference"
            else:
                tendance, statut_tendance = None, "accumulation_insuffisante"
            part = len(concernees) / len(complets)
            if couverture >= 0.999:  # recensement : aucune erreur d'échantillonnage
                ic_bas = ic_haut = part
                estime, estime_zone = len(concernees), sum(1 for o in concernees if o["departement"] in zone_deps)
            else:
                ic_bas, ic_haut = intervalle_wilson(len(concernees), len(complets))
                estime, estime_zone = round(part * len(stock)), round(part * total_zone)
            ligne = {
                "code_naf": code, "naf_version": secteurs.naf_version, "tache_id": tache_id, "fenetre_jours": fenetre,
                "nb_offres_secteur": len(complets), "nb_offres_secteur_total": len(stock),
                "couverture_etiquetage": round(len(complets) / len(stock), 4),
                "nb_offres_tache": len(concernees),
                "nb_offres_tache_zone": sum(1 for o in concernees if o["departement"] in zone_deps),
                "nb_citation_verifiee": avec_citation, "nb_lexique_seul": len(concernees) - avec_citation,
                "part_offres_tache": round(part, 4), "nb_offres_tache_estime": estime,
                "nb_offres_tache_zone_estime": estime_zone, "part_ic95_bas": round(ic_bas, 4), "part_ic95_haut": round(ic_haut, 4),
                "salaire_median_annuel_eur": round(statistics.median(salaires), 2) if salaires else None,
                "nb_salaires": len(salaires), "accumulation_jours": accumulation,
                "tendance_3_mois_pct": tendance, "tendance_statut": statut_tendance,
                "motif_exclusion": referentiels.motif_exclusion_couple(code, tache_id, secteurs, taches),
                "calcule_le": quand,
            }
            resume.couples_calcules += 1
            ancien = derniers.get((code, tache_id))
            if ancien is not None and all(ancien[c] == ligne[c] for c in CHAMPS_COMPARES):
                resume.lignes_inchangees += 1
                continue
            a_ecrire.append(ligne)
    repo.enregistrer_agregats(engine, a_ecrire)
    resume.lignes_ajoutees = len(a_ecrire)
    return resume
