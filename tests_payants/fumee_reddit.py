"""Test de fumée RÉEL de l'API Reddit (sous-étape 4.0) — hors suite par défaut,
à lancer à la main : `python tests_payants/fumee_reddit.py`.

3 requêtes au plus : (1) le jeton, (2) UNE requête de données sur
`/r/smallbusiness/new` (limit=1), (3) réservée si un 401 impose de renouveler.
Lit les identifiants dans l'environnement, sinon dans
`~/.config/radar-opportunites/env` (hors dépôt). N'affiche JAMAIS un secret,
seulement les codes de retour et des comptes.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.adapters import reddit_api  # noqa: E402


def _charger_fichier_local() -> None:
    chemin = Path.home() / ".config" / "radar-opportunites" / "env"
    if not chemin.exists():
        return
    for ligne in chemin.read_text(encoding="utf-8").splitlines():
        cle, _, valeur = ligne.partition("=")
        cle = cle.strip()
        if cle in reddit_api.VARIABLES_ENV and not os.environ.get(cle):
            os.environ[cle] = valeur.strip().strip('"')


def main() -> int:
    _charger_fichier_local()
    client = reddit_api.obtenir_client()
    if client is None:
        print("ÉCHEC : variables Reddit absentes (RADAR_REDDIT_CLIENT_ID / _SECRET / _USER_AGENT).")
        return 1
    try:
        listing = client.get_json("/r/smallbusiness/new", {"limit": 1}, contexte="reddit_api:fumee")
    except Exception as exc:  # noqa: BLE001 -- on veut le type, jamais un secret
        print(f"ÉCHEC : {type(exc).__name__} : {exc}")
        return 1
    enfants = listing.get("data", {}).get("children", [])
    print(f"OK : jeton obtenu, r/smallbusiness a répondu ({len(enfants)} post reçu).")
    return 0 if enfants else 1


if __name__ == "__main__":
    sys.exit(main())
