"""Test de fumée RÉEL de la sous-étape V2.3 : API Offres d'emploi v2 de France Travail (gratuite, 0 €).

    PYTHONPATH=. python tests_payants/fumee_offres.py

Hors suite par défaut (appelle le vrai réseau et exige les identifiants, dans l'environnement ou dans
~/.config/radar-opportunites/env : RADAR_FT_CLIENT_ID et RADAR_FT_CLIENT_SECRET). Six requêtes au plus :
  1. le jeton OAuth2 (client_credentials) ;
  2. une recherche réelle (code 69.20Z, 7 derniers jours) -> statut, Content-Range, offres normalisables ;
  3. une recherche sans résultat attendu (code NAF absurde de date lointaine) -> 204 ou liste vide ;
  4. deux requêtes sur les INDEX de pagination, pour savoir lequel des deux plafonds annoncés est réel
     (documentation officielle : 3000-3149 ; article tiers : 1000-1149) ;
  5. le débit : les requêtes se suivent à 4 par seconde (limite annoncée : 10).
N'affiche jamais le jeton, le secret ni le texte d'une offre. Écrit `rapports/FUMEE_V2_3_<date>.json`.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.adapters import france_travail as ft
from app.adapters import http
from app.adapters.http import ErreurCollecte

RACINE = Path(__file__).resolve().parent.parent


def _tenter(nom: str, fonction, resultats: list[dict]):
    try:
        valeur, detail = fonction()
        resultats.append({"verification": nom, "ok": True, "detail": detail})
        print(f"[OK] {nom} : {detail}")
        return valeur
    except (ErreurCollecte, ft.IdentifiantsAbsents) as exc:
        resultats.append({"verification": nom, "ok": False, "detail": str(exc)[:300]})
        print(f"[ÉCHEC] {nom} : {str(exc)[:300]}")
        return None


def _brut(jeton: ft.Jeton, code: str, debut: datetime, fin: datetime, premier: int, dernier: int):
    """Une requête dont on veut le STATUT réel (y compris 400) : appel direct de bas niveau, sans retry."""
    import requests

    url = ft.construire_url(code, debut, fin, premier=0, dernier=0).replace("range=0-0", f"range={premier}-{dernier}")
    reponse = requests.get(url, headers={"Authorization": f"Bearer {jeton.valeur}", "Accept": "application/json"}, timeout=15)
    return reponse.status_code, reponse.headers.get("Content-Range")


def main() -> int:
    resultats: list[dict] = []
    maintenant = datetime.now(timezone.utc)

    jeton = _tenter("jeton OAuth2 client_credentials", lambda: (ft.obtenir_jeton(), "jeton obtenu (valeur jamais affichée)"), resultats)
    if jeton is None:
        print("Sans jeton, le reste ne peut pas tourner : voir le message ci-dessus.")
        _ecrire(resultats, maintenant, {})
        return 1
    constats: dict = {}

    def recherche_reelle():
        page = ft.rechercher(jeton, "69.20Z", maintenant - timedelta(days=7), maintenant)
        lignes = [ft.normaliser_offre(o) for o in page.offres]
        valides = [l for l in lignes if l]
        constats["recherche_69_20Z_7j"] = {"total_annonce": page.total, "offres_page_1": len(page.offres), "normalisables": len(valides),
                                           "avec_salaire_lisible": sum(1 for l in valides if l["salaire_annuel_min_eur"] is not None),
                                           "avec_code_naf": sum(1 for l in valides if l["code_naf"]),
                                           "avec_commune": sum(1 for l in valides if l["commune"])}
        return page, f"total annoncé {page.total}, {len(page.offres)} offres lues, {len(valides)} normalisables"

    _tenter("recherche réelle 69.20Z (7 jours)", recherche_reelle, resultats)

    def recherche_vide():
        page = ft.rechercher(jeton, "99.99Z", maintenant - timedelta(days=1), maintenant)
        return page, f"{page.total} offre(s) (204 ou liste vide attendu)"

    _tenter("recherche sans résultat attendu", recherche_vide, resultats)

    for premier, dernier in ((1000, 1149), (3000, 3149)):
        def indexes(p=premier, d=dernier):
            statut, content_range = _brut(jeton, "69.20Z", maintenant - timedelta(days=365), maintenant, p, d)
            constats[f"range_{p}_{d}"] = {"statut_http": statut, "content_range": content_range}
            return statut, f"range {p}-{d} -> HTTP {statut}, Content-Range {content_range}"

        _tenter(f"index de pagination {premier}-{dernier}", indexes, resultats)

    _ecrire(resultats, maintenant, constats)
    return 0 if all(r["ok"] for r in resultats) else 1


def _ecrire(resultats: list[dict], quand: datetime, constats: dict) -> None:
    chemin = RACINE / "rapports" / f"FUMEE_V2_3_{quand.date().isoformat()}.json"
    chemin.write_text(json.dumps({"date": quand.isoformat(), "verifications": resultats, "constats": constats},
                                 ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Rapport : {chemin}")


if __name__ == "__main__":
    sys.exit(main())
