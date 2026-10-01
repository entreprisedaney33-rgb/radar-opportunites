#!/usr/bin/env python3
"""Enregistre les identifiants France Travail (client id + secret) dans ~/.config/radar-opportunites/env.

    python scripts/enregistrer_identifiants_france_travail.py

Le client id est demandé normalement ; le secret est saisi SANS affichage (ni historique de terminal, ni
copie dans un fichier du dépôt). Le fichier est créé hors du dépôt avec les droits 600 ; ses autres lignes
(par exemple RADAR_DATABASE_URL) sont conservées telles quelles. Rien n'est affiché ni envoyé nulle part.
"""
from __future__ import annotations

import getpass
import os
import stat
import sys
from pathlib import Path

FICHIER = Path.home() / ".config" / "radar-opportunites" / "env"
CLES = ("RADAR_FT_CLIENT_ID", "RADAR_FT_CLIENT_SECRET")


def main() -> int:
    client_id = input("Identifiant client (client id) : ").strip()
    secret = getpass.getpass("Clé secrète (client secret, rien ne s'affiche) : ").strip()
    if not client_id or not secret or any(c in client_id + secret for c in "\n\r"):
        print("Identifiant ou secret vide : rien n'a été écrit.", file=sys.stderr)
        return 1
    FICHIER.parent.mkdir(parents=True, exist_ok=True)
    gardees = []
    if FICHIER.exists():
        gardees = [l for l in FICHIER.read_text(encoding="utf-8").splitlines() if not l.startswith(tuple(f"{c}=" for c in CLES))]
    gardees += [f"RADAR_FT_CLIENT_ID={client_id}", f"RADAR_FT_CLIENT_SECRET={secret}"]
    descripteur = os.open(FICHIER, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(descripteur, "w", encoding="utf-8") as f:
        f.write("\n".join(gardees) + "\n")
    os.chmod(FICHIER, stat.S_IRUSR | stat.S_IWUSR)
    print(f"Enregistré dans {FICHIER} (droits 600). Pense à les copier toi-même dans Render > Environment du worker.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
