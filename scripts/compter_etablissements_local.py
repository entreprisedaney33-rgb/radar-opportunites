#!/usr/bin/env python3
"""V2.8b : mesure les établissements DEPUIS CE POSTE (l'API Recherche d'entreprises est injoignable depuis Render le 2026-10-02) et
écrit `data/etablissements_import.json`, que le worker importe (`app/import_etablissements.py`).

    python scripts/compter_etablissements_local.py --priorite 1 --manquantes     # paires de priorité 1 jamais mesurées en base
    python scripts/compter_etablissements_local.py --toutes                      # toutes les paires du référentiel (rafraîchit)
    python scripts/compter_etablissements_local.py --paires 69.20Z/33,69.20Z/FR  # quelques paires (test réel)

- Données publiques SIRENE, API gratuite et sans clé : aucun secret, ni dans les requêtes ni dans le fichier (qui est commité et déployé).
- Débit : le limiteur par hôte de `app.adapters.http` (4 requêtes/s, limite officielle 7/s), backoff sur 429.
- Par paire (code NAF, département) : comptage d'entreprises actives (`total_results`, plafonné à 10 000 par l'API) et un échantillon
  d'au plus `--plafond` établissements (100 par défaut) : raison sociale, SIRET, adresse, commune, coordonnées (pour la distance au
  centre de la zone), tranche d'effectif. Département « FR » : comptage seul.
- Le fichier est FUSIONNÉ : les paires déjà présentes sont gardées, celles mesurées par ce lancement remplacées. Une paire déjà mesurée
  dans le fichier depuis moins de `--reprendre-jours` jours est sautée (reprise après une coupure). Écriture atomique tous les 20 paires.
- `--manquantes` lit la base (compte `radar_lecture`, LECTURE SEULE, comme `scripts/suivre_cartographie.py`) ; rien n'est jamais écrit
  en base par ce script : c'est le worker qui importe le fichier.
- Disjoncteur : 3 paires de suite en échec -> arrêt, le fichier garde ce qui a été mesuré.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE))

from app import referentiels  # noqa: E402
from app.adapters import recherche_entreprises as api  # noqa: E402
from app.adapters.http import ErreurCollecte  # noqa: E402
from app.import_etablissements import COLONNES_PROSPECT, DEPARTEMENT_FRANCE, FICHIER_DEFAUT, FORMAT, FichierInvalide, lire_fichier  # noqa: E402

ECHECS_CONSECUTIFS_MAX = 3
ECRITURE_TOUTES_LES = 20


def _iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).isoformat(timespec="seconds")


def toutes_les_paires(priorite: int | None) -> list[tuple[str, str]]:
    secteurs, zone = referentiels.secteurs_tpe(), referentiels.zone()
    codes = secteurs.codes_par_priorite(priorite) if priorite else tuple(s.code for s in secteurs.non_exclus())
    departements = zone.departements_zone() + (DEPARTEMENT_FRANCE,)
    return [(c, d) for c in codes for d in departements]


def paires_mesurees_en_base() -> set[tuple[str, str]]:
    from sqlalchemy import create_engine

    from app.storage import repo
    from scripts.suivre_cartographie import url_lecture
    engine = create_engine(url_lecture())
    try:
        return set(repo.derniers_comptages_etablissements(engine, referentiels.secteurs_tpe().naf_version))
    finally:
        engine.dispose()


def lire_existant(chemin: Path) -> dict[tuple[str, str], dict]:
    """Paires du fichier existant, telles qu'écrites (la validation complète refuse un fichier abîmé : on repart alors de zéro)."""
    if not chemin.is_file():
        return {}
    try:
        lire_fichier(chemin)
    except FichierInvalide as exc:
        print(f"Fichier existant refusé ({exc}) : il sera remplacé.")
        return {}
    brut = json.loads(chemin.read_text(encoding="utf-8"))
    return {(p["code_naf"], p["departement"]): p for p in brut["paires"]}


def ecrire(chemin: Path, paires: dict[tuple[str, str], dict], plafond: int) -> None:
    contenu = {
        "format": FORMAT,
        "description": ("Établissements mesurés hors de Render par scripts/compter_etablissements_local.py (V2.8b, RADAR-V2.md) : "
                        "données publiques de l'API Recherche d'entreprises (SIRENE), aucun secret. Importé par app/import_etablissements.py."),
        "source": api.URL_RECHERCHE,
        "naf_version": referentiels.secteurs_tpe().naf_version,
        "genere_le": _iso(datetime.now(timezone.utc)),
        "plafond_echantillon_par_paire": plafond,
        "colonnes_prospect": list(COLONNES_PROSPECT),
        "paires": [paires[k] for k in sorted(paires)],
    }
    chemin.parent.mkdir(parents=True, exist_ok=True)
    temporaire = chemin.with_suffix(".json.tmp")
    # Une paire par ligne : un diff git lisible d'un rafraîchissement à l'autre, sans indentation coûteuse.
    lignes = ",\n".join(json.dumps(p, ensure_ascii=False, separators=(",", ":")) for p in contenu["paires"])
    entete = json.dumps({k: v for k, v in contenu.items() if k != "paires"}, ensure_ascii=False, indent=1)
    temporaire.write_text(entete[:-2] + ',\n "paires": [\n' + lignes + "\n]}\n", encoding="utf-8")
    lire_fichier(temporaire)  # jamais un fichier que le worker refuserait
    os.replace(temporaire, chemin)


def mesurer(code: str, dep: str, plafond: int) -> dict:
    quand = datetime.now(timezone.utc)
    if dep == DEPARTEMENT_FRANCE:
        page = api.compter_entreprises_actives(code, None)
        return {"code_naf": code, "departement": dep, "mesure_le": _iso(quand), "nb_entreprises_actives": page.total_resultats,
                "comptage_plafonne": page.plafonne, "nb_etablissements_listes": None, "echantillon_complet": None,
                "plafond_echantillon": None, "requetes": 1, "source_url": page.url, "prospects": []}
    e = api.collecter_echantillon(code, dep, plafond=plafond)
    lignes = [[p.siret, p.raison_sociale, p.adresse, p.code_postal, p.code_commune, p.commune, p.latitude, p.longitude,
               p.tranche_effectif_salarie, p.categorie_entreprise, p.est_siege] for p in e.prospects]
    return {"code_naf": code, "departement": dep, "mesure_le": _iso(quand), "nb_entreprises_actives": e.total_entreprises,
            "comptage_plafonne": e.total_plafonne, "nb_etablissements_listes": len(lignes), "echantillon_complet": e.complet,
            "plafond_echantillon": plafond, "requetes": e.pages_lues, "source_url": e.url_premiere_page, "prospects": lignes}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    choix = p.add_mutually_exclusive_group(required=True)
    choix.add_argument("--toutes", action="store_true", help="toutes les paires du référentiel (ou de --priorite)")
    choix.add_argument("--manquantes", action="store_true", help="paires sans aucune mesure en base (lecture seule, radar_lecture)")
    choix.add_argument("--paires", help="liste CODE/DEP séparée par des virgules, ex. 69.20Z/33,69.20Z/FR")
    p.add_argument("--priorite", type=int, choices=(1, 2, 3), help="restreint aux secteurs de cette priorité")
    p.add_argument("--plafond", type=int, default=100, help="établissements d'échantillon par paire (défaut 100)")
    p.add_argument("--reprendre-jours", type=float, default=1.0, help="saute une paire du fichier mesurée depuis moins de N jours (0 = jamais)")
    p.add_argument("--sortie", default=str(RACINE / FICHIER_DEFAUT))
    args = p.parse_args(argv)

    if args.paires:
        valides = set(toutes_les_paires(None))
        cibles = []
        for morceau in args.paires.split(","):
            code, _, dep = morceau.strip().partition("/")
            if (code, dep) not in valides:
                p.error(f"paire inconnue, exclue ou hors zone : {morceau}")
            cibles.append((code, dep))
    else:
        cibles = toutes_les_paires(args.priorite)
        if args.manquantes:
            en_base = paires_mesurees_en_base()
            cibles = [c for c in cibles if c not in en_base]

    chemin = Path(args.sortie)
    paires = lire_existant(chemin)
    seuil = datetime.now(timezone.utc) - timedelta(days=args.reprendre_jours)
    if args.reprendre_jours > 0:
        deja = [c for c in cibles if c in paires and datetime.fromisoformat(paires[c]["mesure_le"]) >= seuil]
        cibles = [c for c in cibles if c not in set(deja)]
        if deja:
            print(f"{len(deja)} paires déjà mesurées dans le fichier depuis moins de {args.reprendre_jours:g} jour(s) : sautées.")
    print(f"{len(cibles)} paires à mesurer (plafond {args.plafond} établissements par paire) -> {chemin}")

    debut = time.monotonic()
    requetes = mesurees = echecs = consecutifs = 0
    for i, (code, dep) in enumerate(cibles, start=1):
        try:
            ligne = mesurer(code, dep, args.plafond)
        except ErreurCollecte as exc:
            echecs += 1
            consecutifs += 1
            print(f"  ÉCHEC {code}/{dep} : [{getattr(exc, 'type_erreur', None)}] {exc}")
            if consecutifs >= ECHECS_CONSECUTIFS_MAX:
                print(f"{ECHECS_CONSECUTIFS_MAX} échecs de suite : arrêt (le fichier garde ce qui a été mesuré).")
                break
            continue
        consecutifs = 0
        paires[(code, dep)] = ligne
        mesurees += 1
        requetes += ligne["requetes"]
        if i % ECRITURE_TOUTES_LES == 0:
            ecrire(chemin, paires, args.plafond)
            print(f"  {i}/{len(cibles)} paires, {requetes} requêtes, {time.monotonic() - debut:.0f} s")
    if mesurees:
        ecrire(chemin, paires, args.plafond)
    taille = chemin.stat().st_size if chemin.is_file() else 0
    print(f"Fini : {mesurees} paires mesurées, {echecs} échecs, {requetes} requêtes, {time.monotonic() - debut:.0f} s ; "
          f"fichier : {len(paires)} paires, {taille / 1e6:.1f} Mo")
    return 0 if echecs == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
