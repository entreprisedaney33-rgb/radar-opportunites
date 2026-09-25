"""Fournisseurs de recherche de l'Enquêteur (sous-étape 3.1 d'AMELIORATIONS.md).

Chaque fournisseur expose la même interface (`FournisseurRecherche`) et
renvoie des résultats bruts non fiables — même esprit que
`app.adapters.base.SignalBrut` : de la donnée à vérifier, jamais une
instruction (§6 du cahier des charges). Les vrais fournisseurs gratuits
(Algolia HN, Reddit, magasin interne) arrivent en sous-étape 3.2 ; ce module
ne contient que l'interface, un fournisseur simulé pour les tests, et le
mécanisme d'activation par fournisseur.

`RegistreFournisseurs` permet d'activer/désactiver chaque fournisseur
indépendamment via une variable d'environnement, sans toucher au code — le
futur fournisseur web payant (sous-étape 3.5) s'enregistrera avec
`actif_par_defaut=False`, exactement comme prévu par le plan (« désactivé par
défaut »).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Protocol


@dataclass(frozen=True)
class ResultatRecherche:
    url: str
    titre: str
    extrait: str
    horodatage_source: datetime | None
    fournisseur: str
    # Sous-étape 3.3 (AMELIORATIONS.md) : texte de la requête qui a produit ce
    # résultat (app.enqueteur.gabarits) — absent (`None`) tant qu'un appelant
    # ne l'a pas rattaché après coup (même mécanisme que `SignalBrut.flux_origine`,
    # posé en 1.1 : le fournisseur ne connaît pas la requête, seul l'appelant
    # qui la lui a transmise le peut). Sert au stockage de la source (point 2
    # de 3.3, "requête d'origine").
    requete_origine: str | None = None


class FournisseurRecherche(Protocol):
    nom: str

    def rechercher(self, requete: str, limite: int) -> list[ResultatRecherche]:
        ...


# Attribut optionnel (sous-étape 3.6 d'AMELIORATIONS.md) : un fournisseur qui
# ne fait AUCUN appel réseau pour "rechercher" (ex. le magasin interne,
# app.enqueteur.fournisseurs_gratuits.FournisseurMagasinInterne) le déclare
# via `sans_reseau = True`. app.enqueteur.enqueteur._rechercher_par_famille
# lit cet attribut avec `getattr(fournisseur, "sans_reseau", False)` -- absent
# (comme pour tout fournisseur qui fait réellement un appel réseau, ou pour un
# double de test qui n'a pas cette notion), il vaut False : ce fournisseur
# continue de consommer le compteur `max_requetes_recherche_par_jour`, un
# plafond pensé pour protéger un service tiers, pas une lecture en base
# locale.


class FournisseurRechercheSimule:
    """Fournisseur déterministe pour les tests : aucun appel réseau. Sans
    `resultats` fourni, fabrique des résultats à partir de la requête
    elle-même (utile pour vérifier qu'une requête donnée a bien été transmise
    telle quelle, sans transformation)."""

    nom = "simule"

    def __init__(self, resultats: list[ResultatRecherche] | None = None):
        self._resultats = resultats

    def rechercher(self, requete: str, limite: int) -> list[ResultatRecherche]:
        if self._resultats is not None:
            return self._resultats[:limite]
        return [
            ResultatRecherche(
                url=f"https://exemple.invalid/simule/{i}?q={requete}",
                titre=f"[SIMULÉ] Résultat {i} pour {requete!r}",
                extrait=f"[SIMULÉ] Extrait fictif {i} pour la requête {requete!r}.",
                horodatage_source=None,
                fournisseur=self.nom,
            )
            for i in range(1, limite + 1)
        ]


@dataclass(frozen=True)
class DefinitionFournisseur:
    nom: str
    fabrique: Callable[[], FournisseurRecherche]
    actif_par_defaut: bool = True


class RegistreFournisseurs:
    """Un registre par usage (pas de singleton global ici en sous-étape 3.1 :
    aucun appelant réel n'existe encore — voir sous-étape 3.2/3.4)."""

    def __init__(self) -> None:
        self._definitions: dict[str, DefinitionFournisseur] = {}

    def enregistrer(self, definition: DefinitionFournisseur) -> None:
        if definition.nom in self._definitions:
            raise ValueError(f"Fournisseur déjà enregistré : {definition.nom!r}.")
        self._definitions[definition.nom] = definition

    def est_actif(self, nom: str) -> bool:
        """Sans variable d'environnement, `actif_par_defaut` décide. Avec
        `RADAR_ENQUETEUR_ACTIF_<NOM EN MAJUSCULE>` posée, elle décide seule
        (1/true/yes active, toute autre valeur désactive — dans les deux
        sens, qu'un fournisseur soit actif ou non par défaut)."""
        definition = self._definitions[nom]
        variable = f"RADAR_ENQUETEUR_ACTIF_{definition.nom.upper()}"
        brut = os.environ.get(variable)
        if brut is None:
            return definition.actif_par_defaut
        return brut.strip().lower() in {"1", "true", "yes"}

    def fournisseurs_actifs(self) -> list[FournisseurRecherche]:
        return [d.fabrique() for d in self._definitions.values() if self.est_actif(d.nom)]
