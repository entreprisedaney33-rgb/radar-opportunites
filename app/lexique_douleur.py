"""Lexique des expressions de douleur (sous-étape 1.2 d'AMELIORATIONS.md).

Sert à composer les requêtes du connecteur de recherche Reddit
(`app/adapters/reddit_recherche.py`), en croisant chaque subreddit `douleur`
(`app/sources.py::subreddits_douleur`) avec chaque expression d'ici. Chargeur
à validation stricte, même esprit que `app/sources.py` : une entrée
invalide est une ERREUR au chargement, jamais un défaut silencieux (§2,
garde-fous).
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

CHEMIN_PAR_DEFAUT = Path(__file__).resolve().parent / "lexique_douleur.yaml"

LANGUES_VALIDES = {"fr", "en"}

CHAMPS_REQUIS = {"cle", "expression", "langue"}


class LexiqueInvalide(Exception):
    pass


@dataclass(frozen=True)
class ExpressionDouleur:
    cle: str
    expression: str
    langue: str


def _valider_entree(brut: Any) -> ExpressionDouleur:
    if not isinstance(brut, dict):
        raise LexiqueInvalide(f"Entrée de lexique invalide (attendu un objet) : {brut!r}")

    manquants = CHAMPS_REQUIS - brut.keys()
    if manquants:
        raise LexiqueInvalide(
            f"Expression {brut.get('cle', '?')!r} : champ(s) manquant(s) {sorted(manquants)}."
        )

    if brut["langue"] not in LANGUES_VALIDES:
        raise LexiqueInvalide(
            f"Expression {brut['cle']!r} : langue {brut['langue']!r} inconnue (attendu {sorted(LANGUES_VALIDES)})."
        )

    if not str(brut["expression"]).strip():
        raise LexiqueInvalide(f"Expression {brut['cle']!r} : texte vide.")

    return ExpressionDouleur(cle=brut["cle"], expression=brut["expression"], langue=brut["langue"])


def charger_expressions(chemin: Path | None = None) -> list[ExpressionDouleur]:
    """Lecture + validation stricte. `chemin` permet aux tests de charger une
    fixture sans toucher au fichier réel ; sans argument, lit
    `app/lexique_douleur.yaml`."""
    chemin = chemin or CHEMIN_PAR_DEFAUT
    brut = yaml.safe_load(chemin.read_text(encoding="utf-8")) or []
    if not isinstance(brut, list):
        raise LexiqueInvalide(f"{chemin} : attendu une liste d'expressions, trouvé {type(brut).__name__}.")

    expressions: list[ExpressionDouleur] = []
    cles_vues: set[str] = set()
    for entree in brut:
        expr = _valider_entree(entree)
        if expr.cle in cles_vues:
            raise LexiqueInvalide(f"Clé d'expression en double : {expr.cle!r}.")
        cles_vues.add(expr.cle)
        expressions.append(expr)
    return expressions


@lru_cache(maxsize=1)
def expressions() -> list[ExpressionDouleur]:
    return charger_expressions()
