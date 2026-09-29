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

Sous-étape 3.17, point 5 : le même marquage tourne AUSSI au démarrage du
Background Worker si la variable d'environnement `RADAR_REPRISE_DEPUIS` est
définie (même format qu'`--depuis`, ex. `2026-09-26T13:41Z`) --
`reprise_au_demarrage` ci-dessous. Pas besoin de connexion en écriture depuis
un poste : le worker a déjà la sienne. Idempotent (redémarrer le worker avec la
variable toujours posée ne marque rien de plus), résumé dans les logs et dans
`runs.erreurs_json`, jamais une exception qui remonte (un marquage raté ne doit
pas empêcher le worker de tourner).

Ce n'est QUE le marquage : le retraitement réel (rappel du Scout avec le
signal d'origine) est fait par le Background Worker lui-même, en priorité,
à chaque passage -- voir `app.pipeline.orchestrator._phase_reprise`. Cette
commande écrit dans la base APPLICATIVE (`DATABASE_URL`, droits d'écriture)
-- contrairement à `app.metriques`, jamais `RADAR_DATABASE_URL` (lecture
seule)."""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone

from sqlalchemy.engine import Engine

from app.storage import repo
from app.storage.db import get_engine, migrer

logger = logging.getLogger(__name__)

VARIABLE_ENV = "RADAR_REPRISE_DEPUIS"


def _parser_depuis(texte: str) -> datetime:
    dt = datetime.fromisoformat(texte)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def marquer_a_reprendre(engine: Engine, depuis: datetime, *, simulation: bool = False) -> list[dict]:
    """Le marquage lui-même, partagé par la commande et par le démarrage du
    worker. Renvoie les dossiers concernés (marqués, ou qui le seraient en
    simulation)."""
    candidats = repo.opportunites_creees_par_repli_scout(engine, depuis)
    if not simulation:
        for o in candidats:
            repo.maj_statut_opportunite(engine, o["id"], "a_reprendre")
    return candidats


def reprise_au_demarrage(engine: Engine) -> str | None:
    """Sous-étape 3.17, point 5. `None` si `RADAR_REPRISE_DEPUIS` est absente
    ou vide (rien à faire, rien à journaliser). Sinon marque les dossiers
    comme `app.reprise --depuis` et renvoie une ligne de résumé (déjà écrite
    dans les logs) destinée à `runs.erreurs_json`. Ne lève jamais."""
    brut = (os.environ.get(VARIABLE_ENV) or "").strip()
    if not brut:
        return None
    try:
        depuis = _parser_depuis(brut)
    except ValueError:
        message = f"Reprise au démarrage IGNORÉE : {VARIABLE_ENV}={brut!r} n'est pas un horodatage ISO 8601 (ex. 2026-09-26T13:41Z)."
        logger.error(message)
        return message
    try:
        marques = marquer_a_reprendre(engine, depuis)
        en_attente = sum(1 for o in repo.lister_opportunites_ouvertes(engine) if o["statut"] == "a_reprendre")
    except Exception as exc:  # un marquage raté n'arrête jamais le worker
        message = f"Reprise au démarrage ÉCHOUÉE ({VARIABLE_ENV}={brut}) : {type(exc).__name__}: {exc}"
        logger.exception(message)
        return message
    message = (
        f"Reprise au démarrage ({VARIABLE_ENV}={depuis.isoformat()}) : {len(marques)} dossier(s) nouvellement "
        f"marqué(s) `a_reprendre`, {en_attente} en attente de retraitement au total."
    )
    logger.info(message)
    return message


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

    candidats = marquer_a_reprendre(engine, args.depuis, simulation=True)

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

    marquer_a_reprendre(engine, args.depuis)

    print(f"{len(candidats)} dossier(s) marqué(s) `a_reprendre`.")
    print("Retraitement réel fait par le Background Worker au prochain passage (priorité, voir _phase_reprise).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
