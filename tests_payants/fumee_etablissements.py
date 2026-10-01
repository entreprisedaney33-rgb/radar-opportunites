"""Test de fumée RÉEL de la sous-étape V2.2 : API Recherche d'entreprises (gratuite, sans clé, 0 €).

    PYTHONPATH=. python tests_payants/fumee_etablissements.py

Hors suite par défaut (appelle le vrai réseau). Deux volets :

1. Cinq requêtes de validation de l'adaptateur : comptage département, comptage France, échantillon
   (page 1 puis page 2 : la pagination avance), et un code inexistant (99.99Z).
2. Existence réelle des codes NAF du référentiel : UNE requête par code, France entière. Un code qui ne
   renvoie AUCUNE entreprise active est « suspect » (faute de frappe, ou code absent de la nomenclature).
   On relève au passage le code NAF rév. 2.1 (`activite_principale_naf25`) que l'API porte déjà pour
   chaque entreprise : simple observation pour la table de correspondance de V2.10, pas une table.

Écrit `rapports/FUMEE_V2_2_<date>.json`. Débit : 4 requêtes/s (limite officielle : 7/s).
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from app import referentiels
from app.adapters import recherche_entreprises as api
from app.adapters.http import ErreurCollecte

RACINE = Path(__file__).resolve().parent.parent


def _volet_1() -> tuple[list[dict], bool]:
    resultats: list[dict] = []
    ok = True

    def noter(nom: str, reussi: bool, detail: str) -> None:
        nonlocal ok
        ok = ok and reussi
        resultats.append({"verification": nom, "ok": reussi, "detail": detail})
        print(f"[{'OK' if reussi else 'ÉCHEC'}] {nom} : {detail}")

    dept = api.compter_entreprises_actives("69.20Z", "33")
    noter("comptage 69.20Z / Gironde", dept.total_resultats > 0, f"{dept.total_resultats} entreprises actives")
    france = api.compter_entreprises_actives("69.20Z", None)
    noter("comptage 69.20Z / France", france.total_resultats >= dept.total_resultats,
          f"{france.total_resultats} (≥ Gironde : {france.total_resultats >= dept.total_resultats})")
    p1 = api.lire_page("69.20Z", "33", page=1)
    pros1 = api.extraire_prospects(p1, "69.20Z", "33")
    avec_coord = sum(1 for p in pros1 if p.latitude is not None and p.longitude is not None)
    noter("échantillon page 1", len(pros1) > 0 and avec_coord > 0,
          f"{len(p1.entreprises)} entreprises lues, {len(pros1)} établissements actifs retenus, {avec_coord} avec coordonnées")
    p2 = api.lire_page("69.20Z", "33", page=2)
    pros2 = api.extraire_prospects(p2, "69.20Z", "33")
    sirets_communs = {p.siret for p in pros1} & {p.siret for p in pros2}
    noter("échantillon page 2 (pagination)", p2.page == 2 and len(pros2) > 0 and not sirets_communs,
          f"{len(pros2)} établissements retenus, {len(sirets_communs)} SIRET en commun avec la page 1")
    try:
        inexistant = api.compter_entreprises_actives("99.99Z", None)
        noter("code inexistant 99.99Z", inexistant.total_resultats == 0, f"{inexistant.total_resultats} résultat(s)")
    except ErreurCollecte as exc:
        noter("code inexistant 99.99Z", True, f"refusé par l'API (erreur de collecte) : {str(exc)[:120]}")
    return resultats, ok


def _volet_2() -> dict:
    secteurs = referentiels.charger_secteurs_tpe().secteurs
    par_code: dict[str, dict] = {}
    erreurs: dict[str, str] = {}
    for i, secteur in enumerate(secteurs, 1):
        try:
            page = api.lire_page(secteur.code, None, page=1, par_page=5, connexes=1)
        except ErreurCollecte as exc:
            erreurs[secteur.code] = str(exc)[:200]
            print(f"  {i:3d}/{len(secteurs)} {secteur.code} ERREUR")
            continue
        naf25 = Counter(
            e.get("activite_principale_naf25") for e in page.entreprises
            if isinstance(e, dict) and e.get("activite_principale") == secteur.code and e.get("activite_principale_naf25")
        )
        par_code[secteur.code] = {
            "libelle": secteur.libelle, "entreprises_actives_france": page.total_resultats,
            "naf25_observes": dict(naf25),
        }
        if page.total_resultats == 0:
            print(f"  {i:3d}/{len(secteurs)} {secteur.code} SUSPECT (0 entreprise)")
    suspects = sorted(c for c, v in par_code.items() if v["entreprises_actives_france"] == 0)
    plafonnes = sorted(c for c, v in par_code.items() if v["entreprises_actives_france"] >= api.PLAFOND_TOTAL_API)
    plusieurs_naf25 = sorted(c for c, v in par_code.items() if len(v["naf25_observes"]) > 1)
    return {"codes_testes": len(secteurs), "codes_repondus": len(par_code), "suspects_zero_resultat": suspects,
            "codes_au_plafond_api_10000": plafonnes, "codes_avec_plusieurs_naf25_observes": plusieurs_naf25,
            "erreurs_api": erreurs, "par_code": par_code}


def main() -> int:
    debut = datetime.now(timezone.utc)
    print("== Volet 1 : 5 requêtes de validation ==")
    volet_1, ok = _volet_1()
    print("== Volet 2 : existence des codes NAF du référentiel (1 requête par code) ==")
    volet_2 = _volet_2()
    rapport = {
        "date": debut.isoformat(), "naf_version_referentiel": referentiels.charger_secteurs_tpe().naf_version,
        "requetes_volet_1": 5, "requetes_volet_2": volet_2["codes_testes"], "volet_1": volet_1, "volet_2": volet_2,
    }
    chemin = RACINE / "rapports" / f"FUMEE_V2_2_{debut.date().isoformat()}.json"
    chemin.write_text(json.dumps(rapport, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nCodes testés : {volet_2['codes_testes']} ; répondus : {volet_2['codes_repondus']} ; "
          f"suspects (0 entreprise) : {volet_2['suspects_zero_resultat'] or 'aucun'} ; erreurs API : {len(volet_2['erreurs_api'])} ; "
          f"au plafond API de 10 000 : {len(volet_2['codes_au_plafond_api_10000'])} ; "
          f"à plusieurs codes NAF 2.1 observés : {len(volet_2['codes_avec_plusieurs_naf25_observes'])}")
    print(f"Rapport : {chemin}")
    return 0 if ok and not volet_2["erreurs_api"] else 1


if __name__ == "__main__":
    sys.exit(main())
