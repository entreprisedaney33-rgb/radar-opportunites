"""Point d'entrée : `python -m app.cli run-once [--dry-run] [--demo] ...`

`--dry-run` bascule sur une base SQLite jetable dédiée (`radar_dry_run.db`,
recréée à chaque fois) et interdit tout appel modèle payant — jamais
d'écriture dans la base de production, comme demandé au §7 (Phase 1).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys


def _construire_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="radar-opportunites")
    sous = parser.add_subparsers(dest="commande", required=True)

    p_run = sous.add_parser("run-once", help="Lance un lot fini : collecte -> Scout -> Analyst -> Critic -> score -> rapport")
    p_run.add_argument("--dry-run", action="store_true",
                        help="Aucun appel modèle payant, écrit dans une base SQLite jetable séparée")
    p_run.add_argument("--demo", action="store_true", help="Force les données de démonstration (DEMO) au lieu des sources réelles")
    p_run.add_argument("--max-signals", type=int, default=None, dest="max_signals")
    p_run.add_argument("--max-deep-dives", type=int, default=None, dest="max_deep_dives")
    p_run.add_argument("--rapport", default=None, help="Chemin du rapport HTML (défaut: rapport_<run_id>.html)")

    p_forever = sous.add_parser(
        "run-forever",
        help="Tourne en continu (Background Worker) : un run par journée UTC, passages enchaînés indéfiniment",
    )
    p_forever.add_argument("--demo", action="store_true", help="Force les données de démonstration (DEMO) au lieu des sources réelles")

    sous.add_parser("migrate", help="Crée les tables manquantes dans la base configurée (DATABASE_URL)")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = _construire_parser().parse_args(argv)

    if args.commande == "migrate":
        from app.storage.db import migrer

        migrer()
        print("Tables créées/à jour.")
        return 0

    if args.commande == "run-once":
        if args.dry_run:
            os.environ["DATABASE_URL"] = "sqlite:///./radar_dry_run.db"
            if os.path.exists("radar_dry_run.db"):
                os.remove("radar_dry_run.db")
            from app import config as cfg

            cfg.get_settings.cache_clear()

        from app.pipeline.orchestrator import ArretPause, OptionsRun, executer_run
        from app.reports.html_report import generer_rapport_html
        from app.storage.db import get_engine, migrer

        engine = get_engine()
        migrer(engine)

        options = OptionsRun(
            mode="dry-run" if args.dry_run else "reel",
            max_signaux=args.max_signals,
            max_analyses=args.max_deep_dives,
            forcer_demo=args.demo,
        )
        try:
            run_id, resume = executer_run(engine, options)
        except ArretPause as exc:
            print(f"ARRÊT : {exc}", file=sys.stderr)
            return 1

        chemin_rapport = args.rapport or f"rapport_{run_id[:8]}.html"
        with open(chemin_rapport, "w", encoding="utf-8") as f:
            f.write(generer_rapport_html(engine, run_id))

        print(f"Run {run_id} terminé. Rapport : {chemin_rapport}")
        print(f"Résumé : {resume}")
        return 0

    if args.commande == "run-forever":
        from app.pipeline.orchestrator import executer_continu
        from app.storage.db import get_engine, migrer

        engine = get_engine()
        migrer(engine)
        executer_continu(engine, forcer_demo=args.demo)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
