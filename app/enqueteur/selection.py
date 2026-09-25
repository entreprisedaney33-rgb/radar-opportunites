"""Sélection des résultats à enquêter (sous-étape 3.3 d'AMELIORATIONS.md,
point 1 : « max 8 par opportunité, priorité aux résultats les plus récents et
aux domaines non encore représentés »).

Fonction pure, aucun réseau : opère sur le pool de `ResultatRecherche` déjà
produit par les fournisseurs de l'Enquêteur (3.1/3.2) pour UNE opportunité (un
appel = une enquête). « Domaines non encore représentés » est lu ici comme
une diversité DANS ce pool, pas comme un historique inter-passages : aucun
lien source<->opportunité n'existe avant la sous-étape 3.4 (qui seule branche
ce module sur le pipeline réel), donc il n'y a rien d'autre à comparer.
Algorithme : d'abord un résultat par domaine distinct (le plus récent de
chaque domaine), puis, s'il reste de la place, les résultats restants par
ordre de récence, tous domaines confondus.
"""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlsplit

from app.enqueteur.fournisseurs import ResultatRecherche
from app.pipeline import dedupe

_DATE_MIN = datetime.min.replace(tzinfo=timezone.utc)


def _domaine(url: str) -> str:
    return urlsplit(url).netloc.lower()


def _cle_recence(resultat: ResultatRecherche) -> datetime:
    """Un résultat sans horodatage (fournisseur qui ne le renseigne pas, ex.
    `FournisseurAlgoliaHN` sur un hit sans `created_at`) est traité comme le
    plus ancien possible : ne jamais le préférer à un résultat daté à
    recence égale de domaine."""
    horodatage = resultat.horodatage_source
    if horodatage is None:
        return _DATE_MIN
    return horodatage if horodatage.tzinfo else horodatage.replace(tzinfo=timezone.utc)


def selectionner_resultats(resultats: list[ResultatRecherche], *, max_resultats: int) -> list[ResultatRecherche]:
    """Dédoublonne par URL canonique (plusieurs fournisseurs peuvent renvoyer
    la même page), puis sélectionne au plus `max_resultats` par le critère
    ci-dessus. Renvoie toujours la sélection triée par récence décroissante
    (la diversité de domaine n'est qu'un critère de sélection, pas un ordre
    final imposé)."""
    vus: set[str] = set()
    uniques: list[ResultatRecherche] = []
    for resultat in resultats:
        canonique = dedupe.canonicaliser_url(resultat.url)
        if canonique in vus:
            continue
        vus.add(canonique)
        uniques.append(resultat)

    tries = sorted(uniques, key=_cle_recence, reverse=True)

    selectionnes: list[ResultatRecherche] = []
    domaines_pris: set[str] = set()
    restants: list[ResultatRecherche] = []
    for resultat in tries:
        domaine = _domaine(resultat.url)
        if domaine not in domaines_pris and len(selectionnes) < max_resultats:
            selectionnes.append(resultat)
            domaines_pris.add(domaine)
        else:
            restants.append(resultat)

    for resultat in restants:
        if len(selectionnes) >= max_resultats:
            break
        selectionnes.append(resultat)

    selectionnes.sort(key=_cle_recence, reverse=True)
    return selectionnes
