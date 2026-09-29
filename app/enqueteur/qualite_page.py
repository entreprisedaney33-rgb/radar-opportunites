"""Contrôles de qualité d'une page fetchée (sous-étape 3.17 d'AMELIORATIONS.md).

Fonctions PURES, aucun réseau, aucun modèle. Deux garde-fous, écrits après le
rapport du 29/09 (`rapports/POINT_ETAPE_2026-09-29.md`) qui a trouvé des pages
d'erreur stockées comme preuves (`lemonde.fr/pricing`, 209 caractères, « A
required part of this site couldn't load… », rattachée à 45 dossiers en tant
que « page de prix ») et des pages étiquetées `prix` qui ne parlent d'aucun
prix (un article sur des agents d'achat, un article sur un rachat) :

1. `motif_page_inexploitable` : une page dont le contenu porte un marqueur
   d'erreur (404, introuvable, page not found, accès refusé...) ou trop courte
   n'est jamais stockée -- même esprit que la sous-étape 3.3 (« page vide ou
   injoignable jamais stockée »), appliqué au cas que 3.3 ne voyait pas : une
   page d'erreur servie avec un code HTTP 200.
2. `contient_marqueur_prix` : une page ne garde l'étiquette `prix` que si son
   texte contient au moins un marqueur de prix. Sinon l'étiquette est refusée
   (la page reste une preuve d'enquête ordinaire, jamais une preuve de prix).
"""
from __future__ import annotations

import re

# Les marqueurs d'erreur sont cherchés dans le TITRE et le DÉBUT du texte
# seulement (`ZONE_MARQUEURS_ERREUR` caractères) : une page d'erreur les porte
# en tête, alors qu'un long article légitime peut mentionner « 404 » ou
# « introuvable » n'importe où dans son corps.
ZONE_MARQUEURS_ERREUR = 500

_MOTIF_ERREUR = re.compile(
    r"\b404\b"
    r"|\bintrouvable\b"
    r"|\bpage\s+not\s+found\b"
    r"|\bacc[èe]s\s+refus[ée]\b"
    r"|\baccess\s+denied\b"
    r"|\b403\s+forbidden\b",
    re.IGNORECASE,
)

# €/$ collés à un chiffre (« 99 $ », « €49 ») -- un « $ » seul (code, jargon)
# n'est pas un prix. Les autres marqueurs sont des mots entiers : « plan »
# n'est pas « planning » ni « explanation ».
_MOTIF_PRIX = re.compile(
    r"\d\s*[€$]|[€$]\s*\d"
    r"|/\s*(?:mois|an|month|year|mo|yr)\b"
    r"|\bper\s+(?:month|year|user|seat)\b"
    r"|\bpar\s+(?:mois|an|utilisateur)\b"
    r"|\bplans?\b"
    r"|\btarifs?\b",
    re.IGNORECASE,
)


def motif_page_inexploitable(titre: str, texte: str, *, longueur_min: int) -> str | None:
    """`None` si la page est exploitable ; sinon le motif du refus (texte
    court, pour le journal). Longueur mesurée sur le texte extrait, espaces
    normalisés."""
    if len(texte.strip()) < longueur_min:
        return f"page trop courte ({len(texte.strip())} < {longueur_min} caractères)"
    debut = f"{titre} {texte[:ZONE_MARQUEURS_ERREUR]}"
    trouve = _MOTIF_ERREUR.search(debut)
    if trouve:
        return f"marqueur d'erreur « {trouve.group(0)} » dans le titre ou le début de la page"
    return None


def contient_marqueur_prix(texte: str) -> bool:
    return _MOTIF_PRIX.search(texte) is not None
