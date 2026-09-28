"""Générateur de requêtes de l'Enquêteur (sous-étape 3.1 d'AMELIORATIONS.md).

Fonction PURE : mêmes entrées -> mêmes requêtes, aucun état, aucun appel
réseau ou modèle. Compose des requêtes déterministes à partir des champs de
l'hypothèse du Scout (acheteur, mots-clés, mécanisme) et des gabarits de
`app/enqueteur/gabarits.yaml`. Chargeur à validation stricte, même esprit que
`app/sources.py` / `app/lexique_douleur.py` : une famille manquante ou
inconnue est une ERREUR au chargement, jamais un défaut silencieux.

Trois familles de gabarits :
- `demande` : où quelqu'un décrit sa douleur (Reddit, Ask HN...).
- `concurrence` : qui vend déjà une solution à cette douleur.
- `prix` : appliquée à chaque concurrent déjà identifié (liste fournie en
  argument, jamais par ce module). La sous-étape 3.4 (branchement réel dans
  le pipeline, `app.enqueteur.enqueteur`) n'utilisait PAS cette famille :
  aucun mécanisme d'identification de concurrents à partir des résultats
  `concurrence` n'était écrit dans son texte -- voir son Journal et §9
  d'AMELIORATIONS.md. Résolu à la sous-étape 3.4b
  (`app.enqueteur.concurrents.identifier_concurrents`, du code, jamais un
  modèle) : `enqueter_opportunite` fournit désormais une liste de noms dès
  que des concurrents ont été identifiés. Ce module lui-même est inchangé
  par 3.4b -- `generer_requetes` savait déjà produire cette famille dès
  qu'un appelant lui fournissait une liste de noms.

Sous-étape 3.11 : `HypotheseEnqueteur.mots_cles` remplace ce qui était encore
`douleur` (la phrase ENTIÈRE de la douleur, 150 à 250 caractères en
production — aucun moteur ne répond à ça). Ce champ porte désormais des
mots-clés COURTS déjà validés (proposition du Scout,
`app.pipeline.mots_cles.valider_mots_cles`) ou dérivés par repli
(`app.pipeline.mots_cles.deriver_mots_cles_repli`) — jamais construits ici,
ce module reste un simple assembleur de gabarits. Si `mots_cles` est vide
(`""`), les familles `demande`/`concurrence` ne produisent AUCUNE requête
(§3, garde-fou « jamais de requête vide ») plutôt que de substituer une
chaîne vide dans le gabarit.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

CHEMIN_PAR_DEFAUT = Path(__file__).resolve().parent / "gabarits.yaml"

FAMILLES_VALIDES = {"demande", "concurrence", "prix"}

# Placeholders venant de l'hypothèse du Scout -> attribut correspondant de
# `HypotheseEnqueteur`. "<nom_concurrent>" n'est volontairement pas ici : il
# ne vient jamais de l'hypothèse, seulement de la liste de concurrents passée
# à `generer_requetes`.
_PLACEHOLDERS_HYPOTHESE = {
    "<mots_cles>": "mots_cles",
    "<acheteur>": "acheteur",
    "<mecanisme>": "mecanisme",
}

PLACEHOLDER_CONCURRENT = "<nom_concurrent>"


class GabaritsInvalides(Exception):
    pass


@dataclass(frozen=True)
class HypotheseEnqueteur:
    """Les seuls champs de l'hypothèse du Scout (`app.models_schemas.ScoutSortie`)
    dont l'Enquêteur a besoin pour composer une requête. Noms en français,
    délibérément indépendants du schéma Pydantic (`buyer`/`pain`/`ai_mechanism`)
    pour ne pas coupler ce module à la frontière de confiance Scout — la
    conversion se fait au branchement réel dans le pipeline (sous-étape 3.4,
    `app.pipeline.orchestrator._phase_enquete`, à partir des colonnes déjà
    persistées `acheteur`/`probleme`/`mecanisme_ia` de l'opportunité, pas
    directement de la sortie du Scout).

    `mots_cles` (sous-étape 3.11, renommé depuis `douleur`) : mots-clés COURTS
    en anglais (§2 du texte de 3.11 : « anglais pour HN et Reddit », les deux
    seuls fournisseurs actifs aujourd'hui) — la proposition validée du Scout
    (`opportunities.mots_cles_en`) ou, à défaut, un repli dérivé par du code
    de la douleur (`app.pipeline.mots_cles.deriver_mots_cles_repli`). Chaîne
    vide (`""`), jamais `None` : `generer_requetes` ci-dessous en fait le
    signal explicite « aucune requête `demande`/`concurrence` à produire »."""

    acheteur: str
    mots_cles: str
    mecanisme: str


def _valider_gabarits(brut: Any, chemin: Path) -> dict[str, list[str]]:
    if not isinstance(brut, dict):
        raise GabaritsInvalides(
            f"{chemin} : attendu un objet (une liste de gabarits par famille), trouvé {type(brut).__name__}."
        )

    familles_inconnues = set(brut) - FAMILLES_VALIDES
    if familles_inconnues:
        raise GabaritsInvalides(
            f"{chemin} : famille(s) inconnue(s) {sorted(familles_inconnues)} (attendu {sorted(FAMILLES_VALIDES)})."
        )

    familles_manquantes = FAMILLES_VALIDES - set(brut)
    if familles_manquantes:
        raise GabaritsInvalides(f"{chemin} : famille(s) manquante(s) {sorted(familles_manquantes)}.")

    resultat: dict[str, list[str]] = {}
    for famille, modeles in brut.items():
        valide = isinstance(modeles, list) and modeles and all(isinstance(m, str) and m.strip() for m in modeles)
        if not valide:
            raise GabaritsInvalides(f"{chemin} : famille {famille!r} doit être une liste non vide de gabarits texte.")
        resultat[famille] = modeles
    return resultat


def charger_gabarits(chemin: Path | None = None) -> dict[str, list[str]]:
    """Lecture + validation stricte. `chemin` permet aux tests de charger une
    fixture sans toucher au fichier réel ; sans argument, lit
    `app/enqueteur/gabarits.yaml`."""
    chemin = chemin or CHEMIN_PAR_DEFAUT
    brut = yaml.safe_load(chemin.read_text(encoding="utf-8")) or {}
    return _valider_gabarits(brut, chemin)


@lru_cache(maxsize=1)
def gabarits() -> dict[str, list[str]]:
    return charger_gabarits()


def _substituer(gabarit: str, hypothese: HypotheseEnqueteur) -> str:
    resultat = gabarit
    for placeholder, attribut in _PLACEHOLDERS_HYPOTHESE.items():
        resultat = resultat.replace(placeholder, getattr(hypothese, attribut))
    return resultat


def generer_requetes(
    hypothese: HypotheseEnqueteur,
    concurrents: list[str] | None = None,
    *,
    gabarits_charges: dict[str, list[str]] | None = None,
) -> dict[str, list[str]]:
    """Renvoie `{famille: [requêtes...]}` pour les trois familles. `concurrents`
    est la liste de noms déjà identifiés par une recherche `concurrence`
    antérieure (vide par défaut : le branchement réel de la sous-étape 3.4,
    `app.enqueteur.enqueteur`, n'alimente toujours pas cette liste -- voir la
    docstring de ce module).

    Sous-étape 3.11 : `hypothese.mots_cles` vide (`""`) -> `demande` et
    `concurrence` renvoient des listes VIDES (jamais un gabarit avec le
    placeholder remplacé par rien) -- garde-fou « jamais de requête vide »
    (§3 d'AMELIORATIONS.md). `prix` n'est pas concerné : elle ne dépend que
    de `concurrents`, jamais de `mots_cles`."""
    g = gabarits_charges if gabarits_charges is not None else gabarits()
    concurrents = concurrents or []

    if hypothese.mots_cles:
        demande = [_substituer(m, hypothese) for m in g["demande"]]
        concurrence = [_substituer(m, hypothese) for m in g["concurrence"]]
    else:
        demande, concurrence = [], []

    return {
        "demande": demande,
        "concurrence": concurrence,
        "prix": [m.replace(PLACEHOLDER_CONCURRENT, concurrent) for concurrent in concurrents for m in g["prix"]],
    }
