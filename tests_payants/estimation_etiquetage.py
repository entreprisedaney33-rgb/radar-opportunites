"""Estimation du coût d'étiquetage de l'HISTORIQUE (stock d'offres actives des 90 derniers jours) -- ne dépense RIEN.

    PYTHONPATH=. python tests_payants/estimation_etiquetage.py

Compte, par code NAF retenu, les offres actives créées depuis 90 jours (API France Travail gratuite : une requête par
code, une seule offre demandée, le total est lu dans `Content-Range`), puis multiplie par le coût MESURÉ d'une offre
au test de fumée réel (`rapports/FUMEE_V2_4_<date>.json`), avec une marge d'incertitude. N'appelle jamais le modèle.
Écrit `rapports/ESTIMATION_ETIQUETAGE_V2_4_<date>.json` (comptes seulement).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import referentiels
from app.agregation import intervalle_wilson
from app.adapters import france_travail as ft
from app.adapters.http import ErreurCollecte

RACINE = Path(__file__).resolve().parent.parent


def main() -> int:
    maintenant = datetime.now(timezone.utc)
    fumees = sorted((RACINE / "rapports").glob("FUMEE_V2_4_*.json"))
    if not fumees:
        print("Lance d'abord tests_payants/fumee_etiquetage.py : le coût par offre doit être MESURÉ, pas supposé.")
        return 1
    mesure = json.loads(fumees[-1].read_text(encoding="utf-8"))
    cout_offre = float(mesure["cout_par_offre_eur"])
    jeton = ft.obtenir_jeton()
    codes = [s.code for s in referentiels.secteurs_tpe().non_exclus()]
    comptes: dict[str, int] = {}
    erreurs: dict[str, str] = {}
    for i, code in enumerate(codes, 1):
        try:
            page = ft.rechercher(jeton, code, maintenant - timedelta(days=90), maintenant, premier=0, dernier=0)
            comptes[code] = page.total
        except ErreurCollecte as exc:
            erreurs[code] = str(exc)[:150]
    total = sum(comptes.values())
    top = sorted(comptes.items(), key=lambda kv: -kv[1])[:10]
    scenarios = {
        "tout_etiqueter": total * cout_offre,
        "tout_etiqueter_prudent_plus_30_pct": total * cout_offre * 1.3,
    }
    hors_interim = {c: n for c, n in comptes.items() if not c.startswith("78.")}
    scenarios["tout_etiqueter_sans_les_codes_78_interim_placement"] = sum(hors_interim.values()) * cout_offre
    echantillons = {}
    for k in (30, 60, 100, 150, 250):
        offres_k = sum(min(n, k) for n in comptes.values())
        bas, haut = intervalle_wilson(round(0.2 * k), k)  # précision sur une part de 20 % avec k offres étiquetées
        echantillons[k] = {"offres_a_etiqueter": offres_k, "cout_central_eur": round(offres_k * cout_offre, 2),
                           "cout_prudent_plus_30_pct_eur": round(offres_k * cout_offre * 1.3, 2),
                           "demi_largeur_ic95_sur_une_part_de_20_pct_en_points": round(100 * (haut - bas) / 2, 1)}
    rapport = {
        "date": maintenant.isoformat(), "codes_interroges": len(codes), "codes_repondus": len(comptes), "erreurs": erreurs,
        "offres_actives_90_jours_total": total, "plus_gros_codes": top,
        "codes_sans_offre": sorted(c for c, n in comptes.items() if n == 0),
        "cout_par_offre_mesure_eur": cout_offre, "jetons_mesures": {"entree": mesure["jetons_entree_moyens"], "sortie": mesure["jetons_sortie_moyens"]},
        "estimation_eur": {k: round(v, 2) for k, v in scenarios.items()}, "echantillon_par_code": echantillons,
        "codes_au_dessus_de_60_offres": sum(1 for n in comptes.values() if n > 60), "offres_par_code": comptes,
    }
    chemin = RACINE / "rapports" / f"ESTIMATION_ETIQUETAGE_V2_4_{maintenant.date().isoformat()}.json"
    chemin.write_text(json.dumps(rapport, ensure_ascii=False, indent=2), encoding="utf-8")
    for cle in ("codes_interroges", "codes_repondus", "erreurs", "offres_actives_90_jours_total", "plus_gros_codes", "codes_sans_offre",
                "cout_par_offre_mesure_eur", "estimation_eur", "echantillon_par_code", "codes_au_dessus_de_60_offres"):
        print(f"{cle}: {rapport[cle]}")
    print(f"Rapport : {chemin}")
    return 0 if not erreurs else 1


if __name__ == "__main__":
    sys.exit(main())
