"""Reprise des dossiers créés uniquement par repli sans modèle (sous-étape
3.13 d'AMELIORATIONS.md) — panne du 26/09/2026 (voir
`rapports/POINT_ETAPE_2026-09-27.md`) : ~100 % des appels Scout/Analyst/
Critic ont échoué pendant ~25 h, chaque dossier retombant en silence sur son
repli heuristique (score 0, aucune preuve, aucune affirmation).

    python -m app.reprise --depuis 2026-09-26T13:41Z
    python -m app.reprise --depuis 2026-09-26T13:41Z --simulation

Marque `a_reprendre` (au lieu de son statut courant) toute opportunité créée
à partir de `--depuis` dont TOUTES les évaluations Scout connues sont un
repli sans modèle (`assessments.modele == "heuristique"`, jamais un vrai nom
de modèle) -- voir `app.storage.repo.opportunites_creees_par_repli_scout`.
Idempotente : une opportunité déjà reprise avec succès (une nouvelle
évaluation Scout avec un vrai modèle existe) disparaît d'elle-même de cette
liste, relancer la commande ne la touche plus jamais. `--simulation`
affiche ce qui serait fait, sans écrire quoi que ce soit.

Ce n'est QUE le marquage : le retraitement réel (rappel du Scout avec le
signal d'origine) est fait par le Background Worker lui-même, en priorité,
à chaque passage -- voir `app.pipeline.orchestrator._phase_reprise`. Cette
commande écrit dans la base APPLICATIVE (`DATABASE_URL`, droits d'écriture)
-- contrairement à `app.metriques`, jamais `RADAR_DATABASE_URL` (lecture
seule)."""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from app.storage import repo
from app.storage.db import get_engine, migrer


def _parser_depuis(texte: str) -> datetime:
    dt = datetime.fromisoformat(texte)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _construire_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.reprise")
    parser.add_argument(
        "--depuis", required=True, type=_parser_depuis,
        help="Horodatage ISO 8601 UTC (ex. 2026-09-26T13:41Z) : borne basse de date_creation.",
    )
    parser.add_argument(
        "--simulation", action="store_true",
        help="N'écrit rien -- affiche seulement ce qui serait marqué `a_reprendre`.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _construire_parser().parse_args(argv)

    engine = get_engine()
    migrer(engine)

    candidats = repo.opportunites_creees_par_repli_scout(engine, args.depuis)

    if not candidats:
        print(f"Aucun dossier créé depuis {args.depuis.isoformat()} uniquement par repli sans modèle.")
        return 0

    verbe = "seraient marqués" if args.simulation else "marqués"
    print(f"{len(candidats)} dossier(s) {verbe} `a_reprendre` (créés depuis {args.depuis.isoformat()}) :")
    for o in candidats:
        print(f"  - {o['id']}  [{o['statut']} -> a_reprendre]  {o['date_creation'].isoformat()}  {o['titre'][:80]}")

    if args.simulation:
        print("Mode simulation : aucune écriture.")
        return 0

    for o in candidats:
        repo.maj_statut_opportunite(engine, o["id"], "a_reprendre")

    print(f"{len(candidats)} dossier(s) marqué(s) `a_reprendre`.")
    print("Retraitement réel fait par le Background Worker au prochain passage (priorité, voir _phase_reprise).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
