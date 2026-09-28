"""Mots-clés courts pour les requêtes de l'Enquêteur (sous-étape 3.11
d'AMELIORATIONS.md).

Constat qui a motivé ce module : les requêtes réellement envoyées par
l'Enquêteur (`app.enqueteur.gabarits`) étaient jusqu'ici des phrases entières
de 150 à 250 caractères (la douleur complète de l'opportunité, recopiée telle
quelle) — aucun moteur ne répond à ça. Deux fonctions PURES ici (aucun appel
modèle, aucun accès réseau, testées sur fixtures) :

1. `valider_mots_cles` — vérifie qu'une proposition du Scout
   (`app.models_schemas.ScoutSortie.mots_cles_en`/`mots_cles_fr`) respecte
   le format attendu (3 à 6 mots, lettres/chiffres/espaces uniquement, jamais
   un opérateur de recherche ni une guillemet) ; renvoie `None` sinon —
   même esprit que `app.pipeline.normalisation.inferer_secteur` : une
   proposition qui ne passe pas la vérification est écartée, jamais
   corrigée à la place du Scout.
2. `deriver_mots_cles_repli` — pour une opportunité qui n'a PAS de mots-clés
   utilisables (créée avant cette sous-étape, ou proposition du Scout
   invalide/absente) : extrait jusqu'à `max_mots` mots significatifs du texte
   de la douleur (mots vides retirés), par du code déterministe — jamais une
   requête vide envoyée à un fournisseur (garde-fou §3 d'AMELIORATIONS.md)."""
from __future__ import annotations

import re

# Lettres latines + accents français courants, chiffres, jamais de
# ponctuation (apostrophe, tiret, guillemet compris) — un mot invalide
# rejette toute la proposition (jamais une correction silencieuse).
_MOT_VALIDE = re.compile(r"^[A-Za-zÀ-ÖØ-öø-ÿ0-9]+$")

MIN_MOTS_SCOUT = 3
MAX_MOTS_SCOUT = 6

MAX_MOTS_REPLI = 5
_LONGUEUR_MIN_MOT_REPLI = 3  # sous ce seuil : trop court pour être significatif (articles, etc. déjà couverts par les mots vides)

# Mots vides FR + EN — volontairement court : sert seulement à écarter les
# mots de liaison les plus fréquents avant de garder les mots restants du
# texte de la douleur, pas un lemmatiseur.
_MOTS_VIDES = {
    "le", "la", "les", "un", "une", "des", "de", "du", "et", "ou", "à", "au", "aux",
    "en", "dans", "sur", "pour", "par", "avec", "sans", "ce", "cet", "cette", "ces",
    "qui", "que", "quoi", "dont", "où", "est", "sont", "être", "avoir", "il", "elle",
    "ils", "elles", "on", "nous", "vous", "je", "tu", "se", "ne", "pas", "plus",
    "comme", "mais", "donc", "car", "chaque", "tout", "toute", "tous", "toutes",
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "without",
    "this", "that", "these", "those", "who", "what", "is", "are", "be", "it", "they",
    "we", "you", "i", "not", "do", "does", "did", "have", "has", "had", "as", "at",
    "by", "from", "if", "so", "but", "my", "our", "your", "their",
}


def _mot_valide(mot: str) -> bool:
    return bool(_MOT_VALIDE.match(mot))


def valider_mots_cles(brut: str | None) -> str | None:
    """`None` si `brut` est absent/vide, si le nombre de mots n'est pas entre
    `MIN_MOTS_SCOUT` et `MAX_MOTS_SCOUT`, ou si un seul mot contient un
    caractère hors lettres/chiffres — jamais de correction, jamais de
    troncature : une proposition invalide est écartée en entier. Espaces
    multiples/en tête/en fin normalisés (rien de plus, même esprit que
    `app.pipeline.normalisation._normaliser_pour_comparaison`)."""
    if not brut:
        return None
    normalise = re.sub(r"\s+", " ", brut.strip())
    if not normalise:
        return None
    mots = normalise.split(" ")
    if not (MIN_MOTS_SCOUT <= len(mots) <= MAX_MOTS_SCOUT):
        return None
    if not all(_mot_valide(mot) for mot in mots):
        return None
    return normalise


def deriver_mots_cles_repli(texte: str, max_mots: int = MAX_MOTS_REPLI) -> str | None:
    """`None` seulement si aucun mot significatif ne reste (texte vide, ou
    entièrement composé de mots vides/trop courts) — jamais une chaîne vide :
    l'appelant (`app/pipeline/orchestrator.py`) traite `None` comme « aucune
    requête à envoyer », jamais comme une requête vide envoyée quand même."""
    tokens = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9]+", texte.lower())
    significatifs = [t for t in tokens if len(t) >= _LONGUEUR_MIN_MOT_REPLI and t not in _MOTS_VIDES]
    if not significatifs:
        return None
    # dédoublonné en gardant l'ordre d'apparition -- un mot répété plusieurs
    # fois dans la douleur ne doit pas monopoliser les `max_mots` gardés.
    vus: set[str] = set()
    uniques: list[str] = []
    for mot in significatifs:
        if mot in vus:
            continue
        vus.add(mot)
        uniques.append(mot)
    return " ".join(uniques[:max_mots])
