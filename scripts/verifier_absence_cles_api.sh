#!/usr/bin/env bash
# Sous-étape 3.5 (AMELIORATIONS.md, point 2) : refuse de pousser si le DIFF
# vers le dépôt de déploiement PUBLIC contient une chaîne ressemblant à une
# clé d'API -- protection indépendante de verifier_absence_fichiers_interdits.sh
# (qui scanne le contenu final déjà synchronisé, pas le diff en train d'être
# committé) et de l'exclusion .env (qui ne protège qu'un NOM de fichier, pas
# une clé glissée ailleurs -- par exemple codée en dur dans un .py par
# erreur, ou collée dans un commentaire).
#
# Usage : lit le diff sur l'entrée standard.
#   git diff --cached | ./scripts/verifier_absence_cles_api.sh
# Sortie : rien, code 0, si rien de suspect. Message explicite sur stderr
# (jamais la clé complète), code 1, sinon.
set -euo pipefail

DIFF="$(cat)"

# Motifs "courants" d'une clé glissée dans un diff (texte de 3.5, point 2) :
# préfixes connus (sk-, BSA), et affectations key=/token= suivies d'une
# chaîne assez longue (16+ caractères) pour être une vraie clé plutôt qu'un
# mot de passe factice court (les fixtures de test de ce dépôt utilisent des
# valeurs courtes comme "faux-secret-de-test", jamais 16 caractères
# alphanumériques d'affilée -- une variable documentée mais vide, comme
# RADAR_BRAVE_SEARCH_API_KEY= dans env.example, n'a elle non plus rien après
# le signe "=" et ne déclenche donc jamais ce motif).
MOTIF_PREFIXE_SK='(^|[^A-Za-z0-9_-])sk-[A-Za-z0-9_-]{16,}'
MOTIF_PREFIXE_BSA='(^|[^A-Za-z0-9_-])BSA[A-Za-z0-9_-]{16,}'
MOTIF_AFFECTATION='(api[_-]?key|apikey|token)[[:space:]]*[:=][[:space:]]*"?[A-Za-z0-9_-]{16,}'

if grep -qE "$MOTIF_PREFIXE_SK" <<< "$DIFF"; then
  echo "ARRÊT : le diff contient une chaîne ressemblant à une clé (préfixe sk-) -- rien n'est poussé." >&2
  exit 1
fi
if grep -qE "$MOTIF_PREFIXE_BSA" <<< "$DIFF"; then
  echo "ARRÊT : le diff contient une chaîne ressemblant à une clé (préfixe BSA, Brave Search) -- rien n'est poussé." >&2
  exit 1
fi
if grep -qiE "$MOTIF_AFFECTATION" <<< "$DIFF"; then
  echo "ARRÊT : le diff contient une affectation key=/token= suivie d'une chaîne longue -- rien n'est poussé." >&2
  exit 1
fi

exit 0
