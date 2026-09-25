#!/usr/bin/env bash
# Synchronise ce dossier (la copie de travail, dans labo-ia) vers le dépôt
# GitHub dédié au déploiement Render : entreprisedaney33-rgb/radar-opportunites
# (privé). Render ne se connecte JAMAIS à labo-ia directement (données
# clients sensibles) — voir ARCHITECTURE.md.
#
# Usage : ./scripts/deployer_vers_github.sh "message de commit"
set -euo pipefail

DEPOT="https://github.com/entreprisedaney33-rgb/radar-opportunites.git"
ICI="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MESSAGE="${1:-Synchronisation depuis labo-ia}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

git clone --quiet "$DEPOT" "$TMP"
rsync -a --delete \
  --exclude='.venv/' --exclude='__pycache__/' --exclude='.pytest_cache/' \
  --exclude='*.db' --exclude='rapport_*.html' --exclude='.git/' \
  "$ICI/" "$TMP/"

cd "$TMP"
git add -A
if git diff --cached --quiet; then
  echo "Rien à synchroniser : le dépôt de déploiement est déjà à jour."
  exit 0
fi
git commit -m "$MESSAGE" --quiet
git push --quiet
echo "Synchronisé vers $DEPOT"
