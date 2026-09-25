#!/usr/bin/env bash
# Synchronise ce dossier (la copie de travail, dans labo-ia) vers le dépôt
# GitHub dédié au déploiement Render : entreprisedaney33-rgb/radar-opportunites
# (public). Render ne se connecte JAMAIS à labo-ia directement (données
# clients sensibles) — voir ARCHITECTURE.md.
#
# Le dépôt de déploiement étant PUBLIC, aucun secret ne doit jamais y
# atterrir : triple protection —
#   1. .env, .env.* et tout motif de scripts/exclusions_deploiement.txt sont
#      exclus de la copie (rsync --exclude) ;
#   2. après la copie et avant tout commit/push, verifier_absence_fichiers_interdits.sh
#      revérifie indépendamment la copie destinée au dépôt ; s'il trouve
#      malgré tout un fichier interdit, le script s'arrête (rien n'est
#      poussé) ;
#   3. sous-étape 3.5 (AMELIORATIONS.md) : une fois le diff préparé
#      (`git add -A`), verifier_absence_cles_api.sh le scanne pour une
#      chaîne ressemblant à une clé d'API (préfixes sk-/BSA, affectations
#      key=/token=) — protection sur le CONTENU du diff, indépendante des
#      deux précédentes (basées sur des noms de fichiers ou le contenu final
#      déjà synchronisé) ; si elle trouve quelque chose, rien n'est committé
#      ni poussé.
#
# Usage : ./scripts/deployer_vers_github.sh "message de commit"
#
# RADAR_DEPOT_DEPLOIEMENT et RADAR_SOURCE_DEPLOIEMENT permettent de
# surcharger le dépôt cible et le dossier source (utilisé uniquement par les
# tests, sur une arborescence fixture — voir tests/test_deploiement.py).
set -euo pipefail

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPOT="${RADAR_DEPOT_DEPLOIEMENT:-https://github.com/entreprisedaney33-rgb/radar-opportunites.git}"
ICI="${RADAR_SOURCE_DEPLOIEMENT:-$(cd "$SCRIPTS_DIR/.." && pwd)}"
MESSAGE="${1:-Synchronisation depuis labo-ia}"
FICHIER_EXCLUSIONS="$SCRIPTS_DIR/exclusions_deploiement.txt"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

MOTIFS_INTERDITS=(--exclude='.env' --exclude='.env.*')
if [[ -f "$FICHIER_EXCLUSIONS" ]]; then
  while IFS= read -r ligne; do
    ligne="${ligne%%#*}"
    ligne="$(echo "$ligne" | xargs)"
    [[ -z "$ligne" ]] && continue
    MOTIFS_INTERDITS+=(--exclude="$ligne")
  done < "$FICHIER_EXCLUSIONS"
fi

git clone --quiet "$DEPOT" "$TMP"
rsync -a --delete \
  --exclude='.venv/' --exclude='__pycache__/' --exclude='.pytest_cache/' \
  --exclude='*.db' --exclude='rapport_*.html' --exclude='.git/' \
  "${MOTIFS_INTERDITS[@]}" \
  "$ICI/" "$TMP/"

if ! "$SCRIPTS_DIR/verifier_absence_fichiers_interdits.sh" "$TMP" "$FICHIER_EXCLUSIONS"; then
  exit 1
fi

cd "$TMP"
git add -A
if git diff --cached --quiet; then
  echo "Rien à synchroniser : le dépôt de déploiement est déjà à jour."
  exit 0
fi

if ! git diff --cached | "$SCRIPTS_DIR/verifier_absence_cles_api.sh"; then
  exit 1
fi

git commit -m "$MESSAGE" --quiet
git push --quiet
echo "Synchronisé vers $DEPOT"
