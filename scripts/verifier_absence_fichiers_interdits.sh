#!/usr/bin/env bash
# Vérifie qu'aucun fichier interdit (secrets) ni aucune URL Postgres avec
# identifiants ne se trouve dans un dossier, avant de le pousser vers un
# dépôt public. Utilisé par deployer_vers_github.sh comme deuxième filet
# (après l'exclusion à la synchronisation), sur la copie destinée au dépôt de
# déploiement — et testable seul, sur n'importe quel dossier (voir
# tests/test_deploiement.py).
#
# Usage : ./scripts/verifier_absence_fichiers_interdits.sh <dossier> [fichier_exclusions]
# Sortie : rien, code 0, si rien n'est trouvé. Message explicite sur stderr,
# code 1, sinon.
set -euo pipefail

DOSSIER="$1"
FICHIER_EXCLUSIONS="${2:-}"

# Motifs interdits : .env sous toutes ses formes, plus ceux du fichier
# d'exclusions (un motif par ligne, lignes vides et commentaires # ignorés).
MOTIFS_INTERDITS=(".env" ".env.*")
if [[ -n "$FICHIER_EXCLUSIONS" && -f "$FICHIER_EXCLUSIONS" ]]; then
  while IFS= read -r ligne; do
    ligne="${ligne%%#*}"
    ligne="$(echo "$ligne" | xargs)"
    [[ -z "$ligne" ]] && continue
    MOTIFS_INTERDITS+=("$ligne")
  done < "$FICHIER_EXCLUSIONS"
fi

FIND_EXPR=()
for motif in "${MOTIFS_INTERDITS[@]}"; do
  if [[ ${#FIND_EXPR[@]} -eq 0 ]]; then
    FIND_EXPR+=(-name "$motif")
  else
    FIND_EXPR+=(-o -name "$motif")
  fi
done

TROUVES="$(find "$DOSSIER" -type f \( "${FIND_EXPR[@]}" \) 2>/dev/null || true)"
if [[ -n "$TROUVES" ]]; then
  echo "ARRÊT : fichier(s) interdit(s) trouvé(s) dans $DOSSIER — rien ne doit être poussé :" >&2
  echo "$TROUVES" >&2
  exit 1
fi

# Deuxième vérification, indépendante de la liste de noms de fichiers
# ci-dessus : une URL Postgres avec identifiants (postgres:// ou
# postgresql://, login:mot_de_passe@) glissée n'importe où dans le contenu.
MOTIF_URL_AVEC_IDENTIFIANTS='postgres(ql)?://[^/@[:space:]]+:[^/@[:space:]]+@'
TROUVES_URL="$(grep -rEln --exclude-dir=.git "$MOTIF_URL_AVEC_IDENTIFIANTS" "$DOSSIER" 2>/dev/null || true)"
if [[ -n "$TROUVES_URL" ]]; then
  echo "ARRÊT : URL Postgres avec identifiants trouvée dans $DOSSIER — rien ne doit être poussé :" >&2
  echo "$TROUVES_URL" >&2
  exit 1
fi

exit 0
