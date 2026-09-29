"""Recalcul des scores, archivage des dossiers faibles et reprise de la
faisabilité (sous-étape 4.1 d'AMELIORATIONS.md, décisions de Mathéo du
29/09/2026).

    python -m app.recalcul --simulation      # lecture seule (RADAR_DATABASE_URL), n'écrit rien
    python -m app.recalcul --appliquer       # DATABASE_URL, droits d'écriture

1. **Recalcul** : pour chaque dossier ayant une analyse de l'Analyst, le score
   est recalculé avec la règle ACTUELLE (`app.scoring.engine`, poids de
   `config/poids_scoring.yaml`) sur SA DERNIÈRE analyse et SES sources ; une
   NOUVELLE ligne `scores` (`origine = recalcul_4_1`) est ajoutée -- l'ancienne
   n'est jamais modifiée. Idempotent : un dossier dont la dernière ligne est
   déjà un recalcul à la version de poids courante est laissé tel quel.
2. **Archivage** : un dossier dont le DERNIER score est sous
   `seuil_score_liste` (50) passe au statut `archive_faible` -- conservé,
   retiré des listes et des requêtes courantes, jamais supprimé. Son statut
   d'avant est écrit dans `decisions` (annulation possible à la main).
3. **Faisabilité** : reprise limitée aux dossiers >= 50 (non archivés) qui n'ont
   pas encore de bloc (`app.roles.faisabilite`), plafonnée par le budget dur du
   jour comme tout appel modèle ; jamais d'Analyst ni de Critic relancé.

Sur le Background Worker, 1 et 2 tournent au démarrage si `RADAR_RECALCUL_4_1`
est posée à `1` ; 3 tourne à chaque passage tant que `RADAR_FAISABILITE_REPRISE`
est posée à `1` (voir `app.pipeline.orchestrator`). Aucune des deux n'est
active par défaut : c'est Mathéo qui les allume dans Render.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass, field

from sqlalchemy.engine import Engine

from app import config as cfg
from app.adapters.model_client import estimer_cout_eur
from app.faisabilite import evaluer_accessibilite
from app.models_schemas import AnalystSortie
from app.roles import faisabilite as role_faisabilite
from app.scoring.engine import InfoSource, ResultatScore, calculer_score
from app.storage import repo

logger = logging.getLogger(__name__)

VARIABLE_RECALCUL = "RADAR_RECALCUL_4_1"
VARIABLE_FAISABILITE = "RADAR_FAISABILITE_REPRISE"
ORIGINE_RECALCUL = "recalcul_4_1"
ORIGINE_FAISABILITE_REPRISE = "reprise_4_1"
STATUT_ARCHIVE = "archive_faible"
# Statuts que ni le recalcul ni l'archivage ne touchent : `a_reprendre` est
# hors métriques tant que le Scout n'a pas repassé (3.13).
STATUTS_IGNORES = {"a_reprendre"}


@dataclass
class LigneRecalcul:
    opportunity_id: str
    statut: str
    ancien_score: float | None
    nouveau: ResultatScore
    run_id: str
    decision_critic: str | None
    analyste_repli: bool  # la dernière analyse est le repli sans modèle (aucun critère évalué)


@dataclass
class PlanRecalcul:
    lignes: list[LigneRecalcul] = field(default_factory=list)
    deja_recalcules: int = 0
    sans_analyse: int = 0
    analyses_illisibles: list[str] = field(default_factory=list)

    def a_archiver(self, seuil: float) -> list[LigneRecalcul]:
        return [l for l in self.lignes if l.nouveau.score_prudent < seuil and l.statut != STATUT_ARCHIVE]


def _infos_sources(brutes: dict[str, dict]) -> dict[str, InfoSource]:
    return {
        source_id: InfoSource(domaine=info["domaine"], origine=info["origine"]) for source_id, info in brutes.items()
    }


def planifier_recalcul(engine: Engine, poids_config: dict | None = None) -> PlanRecalcul:
    """Lecture seule. Calcule, sans rien écrire, le nouveau score de chaque
    dossier qui a une analyse."""
    poids = poids_config or cfg.poids_scoring()
    version = poids.get("version", "?")
    derniers = repo.dernier_score_par_dossier(engine)
    analyses = repo.derniers_assessments_par_dossier(engine, "analyst")  # quelques requêtes en tout, pas une par dossier
    sources_par_dossier = repo.infos_sources_tous_dossiers(engine)
    plan = PlanRecalcul()
    for opp in repo.lister_opportunites_ouvertes(engine):
        if opp["statut"] in STATUTS_IGNORES:
            continue
        dernier = derniers.get(opp["id"])
        if dernier is not None and dernier.get("origine") == ORIGINE_RECALCUL and dernier["version_poids"] == version:
            plan.deja_recalcules += 1
            continue
        analyse = analyses.get(opp["id"])
        if analyse is None:
            plan.sans_analyse += 1
            continue
        try:
            sortie = AnalystSortie.model_validate(analyse["payload_json"])
        except Exception:  # payload d'une ancienne version du schéma : jamais bloquant
            plan.analyses_illisibles.append(opp["id"])
            continue
        plan.lignes.append(LigneRecalcul(
            opportunity_id=opp["id"], statut=opp["statut"],
            ancien_score=dernier["score_prudent"] if dernier else None,
            nouveau=calculer_score(sortie.criteres, poids, _infos_sources(sources_par_dossier.get(opp["id"], {}))),
            run_id=analyse["run_id"],
            decision_critic=dernier["decision_critic"] if dernier else None,
            # Le repli de l'Analyst est stocké sous le nom du vrai modèle : on le
            # reconnaît à son texte (`app.roles.analyst._analyst_heuristique`).
            analyste_repli="mode sans modèle" in (analyse["payload_json"].get("prochain_test_moins_couteux") or ""),
        ))
    return plan


def appliquer_recalcul(engine: Engine, plan: PlanRecalcul, poids_config: dict | None = None) -> dict:
    """Écrit les nouvelles lignes de score, puis archive (sous le seuil).
    Ne supprime ni ne modifie jamais une ligne existante."""
    poids = poids_config or cfg.poids_scoring()
    seuil = cfg.faisabilite()["seuil_score_liste"]
    for ligne in plan.lignes:
        repo.inserer_score(
            engine, opportunity_id=ligne.opportunity_id, run_id=ligne.run_id,
            version_poids=poids.get("version", "?"), valeurs=ligne.nouveau.valeurs,
            score_brut=ligne.nouveau.score_brut, score_prudent=ligne.nouveau.score_prudent,
            couverture_preuves=ligne.nouveau.couverture_preuves, flags=ligne.nouveau.flags,
            decision_critic=ligne.decision_critic, origine=ORIGINE_RECALCUL,
        )
    archives = plan.a_archiver(seuil)
    for ligne in archives:
        archiver_faible(engine, ligne.opportunity_id, statut_avant=ligne.statut, score=ligne.nouveau.score_prudent)
    return {"recalcules": len(plan.lignes), "archives": len(archives)}


def archiver_faible(engine: Engine, opportunity_id: str, *, statut_avant: str, score: float) -> None:
    """Statut `archive_faible` + trace du statut d'avant (jamais de perte)."""
    repo.maj_statut_opportunite(engine, opportunity_id, STATUT_ARCHIVE)
    repo.inserer_decision(
        engine, opportunity_id=opportunity_id, auteur="systeme", action=STATUT_ARCHIVE,
        justification=f"score {score} < {cfg.faisabilite()['seuil_score_liste']} ; statut avant : {statut_avant}",
    )


def recalcul_au_demarrage(engine: Engine) -> str | None:
    """Appelé au début d'`executer_continu`. `None` si `RADAR_RECALCUL_4_1`
    n'est pas `1`. Ne lève jamais (un recalcul raté n'arrête pas le worker)."""
    if (os.environ.get(VARIABLE_RECALCUL) or "").strip() != "1":
        return None
    try:
        plan = planifier_recalcul(engine)
        bilan = appliquer_recalcul(engine, plan)
    except Exception as exc:
        message = f"Recalcul 4.1 ÉCHOUÉ ({VARIABLE_RECALCUL}=1) : {type(exc).__name__}: {exc}"
        logger.exception(message)
        return message
    message = (
        f"Recalcul 4.1 : {bilan['recalcules']} dossier(s) recalculé(s), {bilan['archives']} passé(s) "
        f"`{STATUT_ARCHIVE}`, {plan.deja_recalcules} déjà recalculé(s), {plan.sans_analyse} sans analyse, "
        f"{len(plan.analyses_illisibles)} analyse(s) illisible(s)."
    )
    logger.info(message)
    return message


# ------------------------------------------------------------ faisabilité ---

def reprise_faisabilite_active() -> bool:
    return (os.environ.get(VARIABLE_FAISABILITE) or "").strip() == "1"


def dossiers_pour_faisabilite(engine: Engine, seuil: float | None = None) -> list[dict]:
    """Dossiers notés >= seuil (dernier score), ni archivés ni à reprendre,
    sans bloc de faisabilité. Les plus hauts scores d'abord."""
    seuil = cfg.faisabilite()["seuil_score_liste"] if seuil is None else seuil
    derniers = repo.dernier_score_par_dossier(engine)
    deja = repo.dossiers_avec_faisabilite(engine)
    candidats = [
        o for o in repo.lister_opportunites_ouvertes(engine)
        if o["statut"] not in STATUTS_IGNORES | {STATUT_ARCHIVE}
        and o["id"] not in deja
        and o["id"] in derniers and derniers[o["id"]]["score_prudent"] >= seuil
    ]
    candidats.sort(key=lambda o: derniers[o["id"]]["score_prudent"], reverse=True)
    return candidats


def enregistrer_faisabilite(
    engine: Engine, opportunity_id: str, run_id: str | None, bloc, *, origine: str, modele: str | None,
) -> None:
    """Range un bloc de faisabilité et le drapeau que le CODE en déduit."""
    resultat = evaluer_accessibilite(bloc)
    repo.inserer_faisabilite(
        engine, opportunity_id=opportunity_id, run_id=run_id, origine=origine,
        payload=bloc.model_dump(mode="json"), accessible_solo=resultat.accessible_solo,
        motif_exclusion=resultat.motif_exclusion, modele=modele,
    )


def estimer_cout_reprise_faisabilite(engine: Engine, candidats: list[dict], modele: str) -> dict:
    """Estimation SANS appel modèle : ~3,5 caractères par token pour l'entrée
    (le vrai prompt de reprise, construit pour chaque dossier), 700 tokens de
    sortie (5 champs + justifications). Aux tarifs courants de
    `config/tarifs.yaml`."""
    from app.pipeline.orchestrator import _charger_preuves  # import tardif : évite le cycle

    entrees = 0
    for o in candidats:
        texte = role_faisabilite.PROMPT_SYSTEME + role_faisabilite._prompt_utilisateur(
            o, _charger_preuves(engine, o["id"])
        )
        entrees += int(len(texte) / 3.5) + 1
    sortie = 700 * len(candidats)
    return {
        "dossiers": len(candidats), "tokens_entree": entrees, "tokens_sortie": sortie,
        "cout_estime_eur": round(estimer_cout_eur(modele, entrees, sortie), 4),
    }


# ------------------------------------------------------------------ CLI ---

def _distribution(valeurs: list[float]) -> str:
    if not valeurs:
        return "aucune valeur"
    tri = sorted(valeurs)
    n = len(tri)
    return f"médiane {tri[n // 2]} / p90 {tri[min(n - 1, int(0.9 * (n - 1) + 0.5))]} / max {tri[-1]}"


def _rapport(engine: Engine, plan: PlanRecalcul, seuil: float) -> str:
    lignes_ok = [l for l in plan.lignes if l.ancien_score is not None]
    hausse = sum(1 for l in lignes_ok if l.nouveau.score_prudent > l.ancien_score)
    baisse = sum(1 for l in lignes_ok if l.nouveau.score_prudent < l.ancien_score)
    archives = plan.a_archiver(seuil)
    gardes = [l for l in plan.lignes if l.nouveau.score_prudent >= seuil]
    repli = sum(1 for l in archives if l.analyste_repli)
    candidats = dossiers_pour_faisabilite_simulee(engine, plan, seuil)
    return "\n".join([
        f"Dossiers avec analyse : {len(plan.lignes)} à recalculer ; {plan.deja_recalcules} déjà recalculés ; "
        f"{plan.sans_analyse} sans analyse ; {len(plan.analyses_illisibles)} analyse(s) illisible(s).",
        f"Anciens scores : {_distribution([l.ancien_score for l in lignes_ok])}",
        f"Nouveaux scores : {_distribution([l.nouveau.score_prudent for l in plan.lignes])}",
        f"Baisse : {baisse} ; hausse : {hausse} ; inchangé : {len(lignes_ok) - baisse - hausse}",
        f"Sous {seuil} -> `{STATUT_ARCHIVE}` : {len(archives)} (dont {repli} dont l'analyse est le repli sans modèle)",
        f">= {seuil} (restent listés) : {len(gardes)}",
        f"Reprise de faisabilité : {len(candidats)} dossier(s) >= {seuil}",
    ])


def dossiers_pour_faisabilite_simulee(engine: Engine, plan: PlanRecalcul, seuil: float) -> list[dict]:
    """Ce que `dossiers_pour_faisabilite` renverrait APRÈS le recalcul du
    plan (les nouveaux scores n'étant pas encore en base)."""
    nouveaux = {l.opportunity_id: l.nouveau.score_prudent for l in plan.lignes}
    derniers = repo.dernier_score_par_dossier(engine)
    deja = repo.dossiers_avec_faisabilite(engine)
    resultat = []
    for o in repo.lister_opportunites_ouvertes(engine):
        if o["statut"] in STATUTS_IGNORES or o["id"] in deja:
            continue
        score = nouveaux.get(o["id"], derniers.get(o["id"], {}).get("score_prudent"))
        if score is not None and score >= seuil:
            resultat.append(o)
    return resultat


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.recalcul")
    groupe = parser.add_mutually_exclusive_group(required=True)
    groupe.add_argument("--simulation", action="store_true", help="Lecture seule, n'écrit rien.")
    groupe.add_argument("--appliquer", action="store_true", help="Écrit (DATABASE_URL) : recalcul + archivage.")
    args = parser.parse_args(argv)

    seuil = cfg.faisabilite()["seuil_score_liste"]
    if args.simulation:
        from app.metriques import _engine_lecture_seule
        engine = _engine_lecture_seule()
    else:
        from app.storage.db import get_engine, migrer
        engine = get_engine()
        migrer(engine)

    plan = planifier_recalcul(engine)
    print(_rapport(engine, plan, seuil))
    if args.simulation:
        candidats = dossiers_pour_faisabilite_simulee(engine, plan, seuil)
        est = estimer_cout_reprise_faisabilite(engine, candidats, cfg.get_settings().model_approfondi)
        print(f"Estimation de la reprise de faisabilité : {est}")
        print("Mode simulation : aucune écriture.")
        return 0
    print(appliquer_recalcul(engine, plan))
    return 0


if __name__ == "__main__":
    sys.exit(main())
