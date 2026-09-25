"""Identification des concurrents par du code (sous-étape 3.4b
d'AMELIORATIONS.md) : résout la question laissée ouverte à la sous-étape 3.4
(§9) -- la famille de gabarits `prix` (`app.enqueteur.gabarits`) exigeait une
liste de noms de concurrents, mais aucun mécanisme pour la produire n'existait
encore.

Fonction PURE, aucun réseau, aucun modèle (§7 du cahier des charges :
« Aucun secteur, aucune décision de triage posé par un modèle sans preuve
textuelle vérifiée par le code » -- même principe appliqué ici à
l'identification d'un concurrent). Compose la liste à partir de résultats
DÉJÀ collectés par l'Enquêteur (3.1-3.4), jamais en interrogeant quoi que ce
soit elle-même.

Deux sources, combinées dans cet ordre puis dédoublonnées par domaine, au
plus `MAX_CONCURRENTS` au total :
(a) les résultats du fournisseur « magasin interne »
    (`app.enqueteur.fournisseurs_gratuits.FournisseurMagasinInterne`, sous-étape
    3.2) -- des items `signal_concurrence` déjà rapprochés de l'opportunité
    par similarité lexicale (`app.pipeline.dedupe.similarite_lexicale`),
    quelle que soit la famille de requête qui les a produits ;
(b) les résultats de la seule famille `concurrence`
    (`app/enqueteur/gabarits.yaml`) dont le TITRE contient un marqueur
    d'offre explicite (`MARQUEURS_OFFRE`) -- un signal que la page décrit un
    produit à vendre, pas seulement un article qui mentionne la douleur.

Pour chaque candidat retenu : `nom` = `ResultatRecherche.titre` (tel quel,
jamais reformulé), `domaine` = le nom d'hôte de `ResultatRecherche.url`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.enqueteur.fournisseurs import ResultatRecherche

MAX_CONCURRENTS = 3

# Mots entiers cherchés dans le titre, insensible à la casse -- ni une liste
# exhaustive ni un classement par IA : un simple marqueur lexical, du code
# déterministe comme le reste de ce module. Recherchés sur des FRONTIÈRES DE
# MOT (pas une sous-chaîne brute) : "app" en sous-chaîne matcherait aussi
# "rapport" ou "apparaître", faux positifs qu'un marqueur d'offre n'est pas
# censé produire.
MARQUEURS_OFFRE = ("tool", "software", "logiciel", "platform", "app", "saas")

_MOTIF_MARQUEURS_OFFRE = re.compile(
    r"\b(?:" + "|".join(re.escape(marqueur) for marqueur in MARQUEURS_OFFRE) + r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Concurrent:
    nom: str
    domaine: str


def _domaine(url: str) -> str:
    return urlsplit(url).netloc.lower()


def _contient_marqueur_offre(titre: str) -> bool:
    return _MOTIF_MARQUEURS_OFFRE.search(titre) is not None


def _vers_concurrent(resultat: ResultatRecherche) -> Concurrent | None:
    domaine = _domaine(resultat.url)
    nom = resultat.titre.strip()
    if not domaine or not nom:
        return None
    return Concurrent(nom=nom, domaine=domaine)


def identifier_concurrents(
    resultats_magasin_interne: list[ResultatRecherche],
    resultats_concurrence: list[ResultatRecherche],
    *,
    max_concurrents: int = MAX_CONCURRENTS,
) -> list[Concurrent]:
    """`resultats_magasin_interne` : uniquement les résultats produits par le
    fournisseur `magasin_interne` (point (a) du texte de 3.4b), quelle que
    soit la famille de requête qui les a trouvés. `resultats_concurrence` :
    uniquement les résultats de la famille `concurrence` (point (b), filtrés
    ici sur le marqueur d'offre du titre -- l'appelant ne doit PAS déjà les
    avoir filtrés).

    Dédoublonnage par domaine (le même domaine identifié deux fois -- une
    fois par chaque source -- ne compte qu'une fois, le premier trouvé étant
    conservé), ordre (a) puis (b), tronqué à `max_concurrents`. Un résultat
    sans domaine exploitable (URL invalide) ou sans titre est ignoré."""
    candidats: list[ResultatRecherche] = list(resultats_magasin_interne)
    candidats += [r for r in resultats_concurrence if _contient_marqueur_offre(r.titre)]

    concurrents: list[Concurrent] = []
    domaines_vus: set[str] = set()
    for resultat in candidats:
        concurrent = _vers_concurrent(resultat)
        if concurrent is None or concurrent.domaine in domaines_vus:
            continue
        domaines_vus.add(concurrent.domaine)
        concurrents.append(concurrent)
        if len(concurrents) >= max_concurrents:
            break
    return concurrents
