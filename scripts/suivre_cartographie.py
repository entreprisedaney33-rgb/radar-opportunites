#!/usr/bin/env python3
"""V2.8 : suit l'avancement de la cartographie (lecture seule) et vérifie que le compte `radar_lecture` lit les tables de la v2.

    python scripts/suivre_cartographie.py            # état des runs du cycle + avancement chiffré
    python scripts/suivre_cartographie.py --acces    # + contrôle de lecture de chaque table v2 et absence de tout droit d'écriture
    python scripts/suivre_cartographie.py --journal 15   # + les N dernières lignes du journal du dernier run

Se connecte avec RADAR_DATABASE_URL (variable d'environnement, ou fichier ~/.config/radar-opportunites/env) : le compte en LECTURE SEULE.
N'affiche jamais l'URL ni aucun mot de passe, n'écrit rien dans la base.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE))

FICHIER_ENV = Path.home() / ".config" / "radar-opportunites" / "env"
TABLES_V2 = ("etablissements_secteur", "prospection", "offres_emploi", "collectes_offres", "offres_etiquetage", "offres_taches",
             "demande_secteur_tache", "fiches_secteur_tache", "concurrence_secteur_tache", "recherches_web")
MODES = ("cartographie_initiale_v2", "cycle_v2")


def url_lecture() -> str:
    url = os.environ.get("RADAR_DATABASE_URL")
    if not url and FICHIER_ENV.is_file():
        for ligne in FICHIER_ENV.read_text(encoding="utf-8").splitlines():
            if ligne.startswith("RADAR_DATABASE_URL="):
                url = ligne.split("=", 1)[1].strip()
    if not url:
        raise SystemExit("RADAR_DATABASE_URL absente (ni dans l'environnement, ni dans ~/.config/radar-opportunites/env).")
    return url


def derniers_runs(engine: Engine) -> list[dict]:
    resultat = []
    with engine.connect() as cx:
        for mode in MODES:
            ligne = cx.execute(text("SELECT id, mode, statut, debut, fin, resume_json, couts_json, erreurs_json FROM runs WHERE mode = :m ORDER BY debut DESC LIMIT 1"), {"m": mode}).mappings().first()
            if ligne:
                d = dict(ligne)
                for cle in ("resume_json", "couts_json", "erreurs_json"):
                    if isinstance(d[cle], str):
                        d[cle] = json.loads(d[cle])
                resultat.append(d)
    return resultat


def compter(engine: Engine) -> dict[str, int | str]:
    comptes: dict[str, int | str] = {}
    with engine.connect() as cx:
        for table in TABLES_V2:
            try:
                comptes[table] = cx.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()  # noms constants ci-dessus : jamais saisis
            except Exception as exc:  # noqa: BLE001
                cx.rollback()
                comptes[table] = f"ILLISIBLE ({type(exc).__name__})"
    return comptes


def controle_acces(engine: Engine, comptes: dict[str, int | str]) -> list[str]:
    """Problèmes d'accès : table illisible, ou droit d'écriture présent (Postgres seulement)."""
    problemes = [f"{t} : illisible par ce compte" for t, n in comptes.items() if isinstance(n, str)]
    if engine.dialect.name == "postgresql":
        with engine.connect() as cx:
            for table in TABLES_V2:
                droits = cx.execute(text("SELECT has_table_privilege(current_user, :t, 'INSERT'), has_table_privilege(current_user, :t, 'UPDATE'), "
                                         "has_table_privilege(current_user, :t, 'DELETE'), has_table_privilege(current_user, :t, 'TRUNCATE')"), {"t": table}).one()
                if any(droits):
                    problemes.append(f"{table} : ce compte a un droit d'ÉCRITURE (il doit être en lecture seule)")
    return problemes


def main(argv: list[str] | None = None, *, engine: Engine | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--acces", action="store_true", help="vérifie la lecture des tables v2 et l'absence de droit d'écriture")
    p.add_argument("--journal", type=int, default=6, help="nombre de lignes de journal à montrer (défaut 6)")
    args = p.parse_args(argv)
    engine = engine or create_engine(url_lecture(), future=True)
    for run in derniers_runs(engine):
        resume = run["resume_json"] or {}
        print(f"[{run['mode']}] statut {run['statut']} ; début {run['debut']} ; fin {run['fin']} ; passes {resume.get('passes')}")
        for ligne in (resume.get("journal") or [])[-args.journal:]:
            print(f"    {ligne}")
        if run["erreurs_json"]:
            print(f"    erreurs : {run['erreurs_json']}")
        if resume.get("avancement"):
            print("    avancement : " + json.dumps({k: v for k, v in resume["avancement"].items() if k in ("depense_modele_cumulee_eur", "depense_modele_du_jour_eur", "reste_priorite")}, ensure_ascii=False))
    comptes = compter(engine)
    print("Lignes par table : " + ", ".join(f"{t}={n}" for t, n in comptes.items()))
    if args.acces:
        problemes = controle_acces(engine, comptes)
        for pb in problemes:
            print(f"  PROBLÈME D'ACCÈS : {pb}")
        print("Accès en lecture : OK (toutes les tables v2 lisibles, aucun droit d'écriture)." if not problemes else "Accès en lecture : À CORRIGER.")
        return 1 if problemes else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
