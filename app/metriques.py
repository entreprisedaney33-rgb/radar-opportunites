"""Métriques de pilotage — lecture seule sur la base, AUCUN appel modèle.

    python -m app.metriques --jour AAAA-MM-JJ

Affiche le résultat et l'écrit dans `rapports/metriques/<jour>.json`. Le
"jour" est une journée UTC (mêmes bornes que le run journalier du worker,
voir app/pipeline/orchestrator.py). Une opportunité compte pour le jour où
elle a été REPÉRÉE (`opportunities.date_creation`), même si elle n'a été
notée qu'un jour plus tard — "analysée" est calculé séparément (présence
d'un score), voir plus bas.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.engine import Engine

from app.storage.schema import opportunities, opportunity_evidence, scores, usage_events

RACINE = Path(__file__).resolve().parent.parent
DOSSIER_RAPPORTS = RACINE / "rapports" / "metriques"


def _bornes_jour_utc(jour: date) -> tuple[datetime, datetime]:
    debut = datetime(jour.year, jour.month, jour.day, tzinfo=timezone.utc)
    return debut, debut + timedelta(days=1)


def _percentile(valeurs: list[float], p: float) -> float | None:
    """Percentile par rang le plus proche (arrondi au-dessus à 0,5 pile),
    sans interpolation ni dépendance supplémentaire — reproductible, mais
    tombe toujours sur une vraie valeur observée, jamais une moyenne entre
    deux. `p` entre 0 et 1."""
    if not valeurs:
        return None
    trie = sorted(valeurs)
    rang = min(len(trie) - 1, int(p * (len(trie) - 1) + 0.5))
    return trie[rang]


def calculer_metriques(engine: Engine, jour: date) -> dict:
    """Fonction pure côté lecture : ne modifie jamais la base, ne fait
    jamais d'appel réseau. Testée sur fixtures dans tests/test_metriques.py."""
    debut, fin = _bornes_jour_utc(jour)

    with engine.connect() as cx:
        opps = cx.execute(
            select(opportunities).where(
                opportunities.c.date_creation >= debut, opportunities.c.date_creation < fin
            )
        ).mappings().all()
        opp_ids = [o["id"] for o in opps]

        # `scores` est append-only (voir SCORING.md, "Historique, jamais
        # écrasé") : on garde le DERNIER score de chaque opportunité en
        # itérant dans l'ordre croissant, jamais un score intermédiaire.
        scores_par_opp: dict[str, dict] = {}
        if opp_ids:
            lignes_scores = cx.execute(
                select(scores).where(scores.c.opportunity_id.in_(opp_ids)).order_by(scores.c.date_creation)
            ).mappings().all()
            for ligne in lignes_scores:
                scores_par_opp[ligne["opportunity_id"]] = dict(ligne)

        preuves_par_opp: dict[str, set] = {}
        if opp_ids:
            lignes_preuves = cx.execute(
                select(opportunity_evidence.c.opportunity_id, opportunity_evidence.c.source_id)
                .where(opportunity_evidence.c.opportunity_id.in_(opp_ids))
            ).all()
            for opp_id, source_id in lignes_preuves:
                preuves_par_opp.setdefault(opp_id, set()).add(source_id)

        lignes_couts = cx.execute(
            select(usage_events.c.cout_declare_ou_estime).where(
                usage_events.c.date_creation >= debut, usage_events.c.date_creation < fin
            )
        ).all()

    nb_reperees = len(opps)
    nb_analysees = len(scores_par_opp)
    cout_jour = round(sum(c for (c,) in lignes_couts), 4)

    par_statut: dict[str, int] = {}
    for o in opps:
        par_statut[o["statut"]] = par_statut.get(o["statut"], 0) + 1

    par_decision: dict[str, int] = {}
    for s in scores_par_opp.values():
        cle = s["decision_critic"] or "aucune"
        par_decision[cle] = par_decision.get(cle, 0) + 1

    scores_prudents = [s["score_prudent"] for s in scores_par_opp.values()]
    nb_hors_intersectoriel = sum(1 for o in opps if o["secteur"] != "intersectoriel")
    nb_sources = [len(preuves_par_opp.get(o["id"], set())) for o in opps]

    return {
        "jour": jour.isoformat(),
        "opportunites_reperees": nb_reperees,
        "opportunites_analysees": nb_analysees,
        "par_statut": par_statut,
        "par_decision_critic": par_decision,
        "score_prudent": {
            "min": min(scores_prudents) if scores_prudents else None,
            "mediane": _percentile(scores_prudents, 0.5),
            "p90": _percentile(scores_prudents, 0.9),
            "max": max(scores_prudents) if scores_prudents else None,
            "nb_superieur_60": sum(1 for v in scores_prudents if v > 60),
            "nb_superieur_80": sum(1 for v in scores_prudents if v > 80),
        },
        "part_hors_intersectoriel": round(nb_hors_intersectoriel / nb_reperees, 4) if nb_reperees else None,
        "sources_par_dossier": {
            "min": min(nb_sources) if nb_sources else None,
            "mediane": _percentile([float(n) for n in nb_sources], 0.5),
            "max": max(nb_sources) if nb_sources else None,
            "part_une_seule_source": round(sum(1 for n in nb_sources if n == 1) / nb_reperees, 4) if nb_reperees else None,
        },
        # Le type d'objection n'existe pas encore dans le modèle de données
        # (Objection = texte + source_ids seulement, voir app/models_schemas.py)
        # -- champ laissé vide plutôt qu'inventé, en attendant l'étape 5 du
        # brief pour Fable 5 (BRIEF-FABLE5-AMELIORER-RADAR.md).
        "objections_critic_par_type": None,
        "cout_jour_eur": cout_jour,
        "cout_moyen_par_dossier_analyse_eur": round(cout_jour / nb_analysees, 4) if nb_analysees else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.metriques")
    parser.add_argument("--jour", required=True, help="Jour UTC au format AAAA-MM-JJ")
    args = parser.parse_args(argv)

    try:
        jour = datetime.strptime(args.jour, "%Y-%m-%d").date()
    except ValueError:
        print(f"Format de date invalide : {args.jour!r} (attendu AAAA-MM-JJ)", file=sys.stderr)
        return 1

    from app.storage.db import get_engine

    resultat = calculer_metriques(get_engine(), jour)

    DOSSIER_RAPPORTS.mkdir(parents=True, exist_ok=True)
    chemin = DOSSIER_RAPPORTS / f"{jour.isoformat()}.json"
    with chemin.open("w", encoding="utf-8") as f:
        json.dump(resultat, f, ensure_ascii=False, indent=2)

    print(json.dumps(resultat, ensure_ascii=False, indent=2))
    print(f"\nÉcrit dans {chemin}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
