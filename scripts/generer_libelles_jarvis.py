"""Libellés des secteurs (codes NAF) et des tâches pour le workflow Jarvis `jarvis-radar-recap` (V2.7).

La base du radar ne stocke que des codes (`code_naf`, `tache_id`) ; les libellés vivent dans les référentiels
YAML. Le workflow n8n n'a pas accès à ces fichiers : il en reçoit une COPIE, générée ici, qui est collée en tête
de ses nœuds de code. Un test (`tests/test_jarvis_libelles.py`) échoue si la copie n'est plus à jour : une
modification d'un référentiel oblige donc à relancer ce script, puis à reposer le workflow.

    python scripts/generer_libelles_jarvis.py            # réécrit produits/jarvis/radar-v2/libelles.json
    python scripts/generer_libelles_jarvis.py --verifier # n'écrit rien, échoue si la copie est périmée
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]
SORTIE = RACINE.parent / "jarvis" / "radar-v2" / "libelles.json"
sys.path.insert(0, str(RACINE))

from app import referentiels  # noqa: E402


def construire() -> dict:
    secteurs = referentiels.secteurs_tpe()
    return {
        "naf_version": secteurs.naf_version,
        "secteurs": {code: s.libelle for code, s in sorted(secteurs.par_code().items())},
        "taches": {tid: t.libelle for tid, t in sorted(referentiels.taches().par_id().items())},
        # Obligations datées : le détail d'une fiche affiche le lien officiel des constats qui s'y réfèrent (`declencheur:<id>`).
        "declencheurs": {d.id: {"libelle": d.libelle, "date": d.date.isoformat(), "source_url": d.source_url}
                         for d in sorted(referentiels.declencheurs().declencheurs, key=lambda x: x.id)},
        "rayon_km": referentiels.zone().rayon_km,
        "centre": referentiels.zone().centre.nom,
    }


def texte(donnees: dict) -> str:
    return json.dumps(donnees, ensure_ascii=False, indent=1, sort_keys=True) + "\n"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--verifier", action="store_true")
    args = p.parse_args()
    attendu = texte(construire())
    if args.verifier:
        if not SORTIE.exists() or SORTIE.read_text(encoding="utf-8") != attendu:
            print(f"PÉRIMÉ : {SORTIE} ne correspond plus aux référentiels (relancer sans --verifier).", file=sys.stderr)
            return 1
        print("à jour")
        return 0
    SORTIE.write_text(attendu, encoding="utf-8")
    print(f"écrit : {SORTIE} ({len(attendu)} octets)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
