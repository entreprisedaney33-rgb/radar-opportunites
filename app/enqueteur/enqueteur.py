"""Orchestration de l'Enquêteur pour UNE opportunité (sous-étape 3.4
d'AMELIORATIONS.md) : relie les modules jusque-là autonomes -- gabarits de
requêtes (3.1), fournisseurs (3.1/3.2), sélection + fetch + stockage (3.3) --
sur une opportunité réelle du pipeline, avec les compteurs de budget posés en
3.1 (`app.pipeline.budget.BudgetTracker`) et le rattachement des sources
trouvées à l'opportunité (`app.enqueteur.fetch.collecter_preuves`, étendu
pour cette sous-étape).

Ordre du cahier des charges (§2) : Scout -> Enquêteur -> Analyst -> Critic
(voir `app.pipeline.orchestrator._phase_enquete`, seul appelant réel).

Ne lève jamais `BudgetDepasse` : chaque requête de recherche et chaque fetch
de page sont protégés individuellement (même esprit que
`app.pipeline.orchestrator._collecter`, où une source en panne n'arrête
jamais la collecte des autres) -- un plafond atteint arrête simplement
l'enquête de CETTE opportunité là où elle en est. Les preuves déjà trouvées
restent utiles à l'Analyst, et l'opportunité n'est jamais laissée bloquée en
attente (point 3 du texte de 3.4).

Familles `demande` et `concurrence` de `app/enqueteur/gabarits.yaml` :
recherchées systématiquement (`_rechercher_toutes_familles`), comme depuis la
sous-étape 3.4. Famille `prix`, sous-étape 3.4b : identification de
concurrents par du code (`app.enqueteur.concurrents.identifier_concurrents`,
jamais un modèle -- résout la question laissée ouverte en 3.4, voir §9
d'AMELIORATIONS.md) à partir des résultats ci-dessus, puis, seulement si au
moins un concurrent est identifié, recherche de la famille `prix` PLUS
tentative de fetch direct de `<domaine>/pricing` pour chacun
(`_enqueter_prix`) -- dans les mêmes plafonds de requêtes/fetchs que le
reste de l'enquête. Les pages ainsi trouvées sont étiquetées `prix`
(`app.enqueteur.fetch.ETIQUETTE_PREUVE_PRIX`), jamais `preuve_enquete`."""
from __future__ import annotations

import logging
from dataclasses import replace

from sqlalchemy.engine import Engine

from app.enqueteur.concurrents import Concurrent, identifier_concurrents
from app.enqueteur.fetch import ETIQUETTE_PREUVE_PRIX, collecter_preuves
from app.enqueteur.fournisseurs import RegistreFournisseurs, ResultatRecherche
from app.enqueteur.gabarits import HypotheseEnqueteur, generer_requetes
from app.pipeline.budget import BudgetDepasse, BudgetTracker

logger = logging.getLogger(__name__)

# Recherchées pour TOUTE opportunité enquêtée (`_rechercher_toutes_familles`).
# La famille "prix" est traitée à part (`_enqueter_prix`) : elle ne dépend pas
# de l'hypothèse du Scout mais des concurrents identifiés à partir de ces
# deux familles-ci -- voir la docstring de module.
FAMILLES_UTILISEES = ("demande", "concurrence")


def _rechercher_par_famille(
    hypothese: HypotheseEnqueteur, *, registre: RegistreFournisseurs, budget: BudgetTracker,
    limite_par_requete: int, familles: tuple[str, ...] = FAMILLES_UTILISEES,
    concurrents: list[str] | None = None,
) -> dict[str, list[ResultatRecherche]]:
    """Une "requête de recherche" (compteur budget) = UN appel à
    `fournisseur.rechercher(...)`, quel que soit le nombre de requêtes HTTP
    que ce fournisseur fait réellement en interne (ex. `FournisseurAlgoliaHN`,
    qui interroge 2 tags par appel) -- c'est l'unité de l'interface
    `FournisseurRecherche`, pas une comptabilité HTTP bas niveau.

    Sous-étape 3.6 : un fournisseur qui déclare `sans_reseau = True` (le
    magasin interne, seul cas aujourd'hui -- voir
    `app.enqueteur.fournisseurs_gratuits.FournisseurMagasinInterne`) est
    appelé normalement, mais SANS jamais engager ni journaliser le compteur
    `max_requetes_recherche_par_jour` : ce plafond protège des services tiers
    (Algolia HN, Reddit) d'un martèlement, pas une lecture en base locale qui
    ne fait aucun appel HTTP.

    Renvoie les résultats groupés PAR FAMILLE (`{famille: [résultats...]}`,
    une clé par élément de `familles`) plutôt qu'une liste à plat : la
    sous-étape 3.4b a besoin de distinguer les résultats de la famille
    `concurrence` du reste pour identifier des concurrents
    (`app.enqueteur.concurrents.identifier_concurrents`). Un plafond de
    budget atteint arrête la recherche immédiatement et renvoie ce qui a déjà
    été trouvé -- jamais d'exception qui remonte (même esprit que le reste du
    module)."""
    toutes_familles = generer_requetes(hypothese, concurrents)
    fournisseurs = registre.fournisseurs_actifs()

    par_famille: dict[str, list[ResultatRecherche]] = {famille: [] for famille in familles}
    for famille in familles:
        for requete in toutes_familles[famille]:
            for fournisseur in fournisseurs:
                nom_fournisseur = getattr(fournisseur, "nom", "inconnu")
                sans_reseau = getattr(fournisseur, "sans_reseau", False)
                if not sans_reseau:
                    try:
                        budget.verifier_et_engager_requete_recherche()
                    except BudgetDepasse as exc:
                        logger.info("Enquêteur : %s -- arrêt des recherches pour cette opportunité.", exc)
                        return par_famille
                try:
                    trouves = fournisseur.rechercher(requete, limite_par_requete)
                except Exception as exc:  # un fournisseur en panne n'arrête pas les autres
                    logger.warning(
                        "Enquêteur : fournisseur %s indisponible pour %r : %s", nom_fournisseur, requete, exc,
                    )
                    trouves = []
                if not sans_reseau:
                    budget.enregistrer_requete_recherche(fournisseur=nom_fournisseur)
                par_famille[famille].extend(replace(r, requete_origine=requete) for r in trouves)
    return par_famille


def _rechercher_toutes_familles(
    hypothese: HypotheseEnqueteur, *, registre: RegistreFournisseurs, budget: BudgetTracker,
    limite_par_requete: int,
) -> list[ResultatRecherche]:
    """Familles `demande` + `concurrence` à plat, dans l'ordre -- inchangé
    depuis la sous-étape 3.4, désormais un simple aplatissement de
    `_rechercher_par_famille`."""
    par_famille = _rechercher_par_famille(
        hypothese, registre=registre, budget=budget, limite_par_requete=limite_par_requete,
    )
    return [resultat for famille in FAMILLES_UTILISEES for resultat in par_famille[famille]]


def _enqueter_prix(
    engine: Engine,
    opportunity_id: str,
    hypothese: HypotheseEnqueteur,
    concurrents: list[Concurrent],
    *,
    registre: RegistreFournisseurs,
    budget: BudgetTracker,
    quotas: dict,
) -> list[str]:
    """Sous-étape 3.4b. Appelée uniquement si `concurrents` est non vide
    (voir `enqueter_opportunite`). Deux sources de pages, mêlées puis
    sélectionnées/stockées ensemble par un seul appel à `collecter_preuves`
    (mêmes garde-fous -- diversité de domaine, taille max, robots.txt -- et
    mêmes compteurs `BudgetTracker` que le reste de l'enquête) :
    - la famille `prix` de `app/enqueteur/gabarits.yaml`
      (« <nom> pricing », « <nom> tarifs ») pour chaque concurrent, via les
      fournisseurs actifs du registre (`_rechercher_par_famille`, mêmes
      compteurs `requetes_recherche` que `demande`/`concurrence`) ;
    - une tentative de fetch DIRECT de `<domaine>/pricing` pour chaque
      concurrent (un `ResultatRecherche` construit ici, jamais issu d'une
      recherche) -- `robots.txt` est vérifié comme pour n'importe quelle
      autre page (`app.enqueteur.fetch.recuperer_page`), donc une page
      interdite n'est jamais stockée.

    Les pages obtenues sont étiquetées `prix`
    (`app.enqueteur.fetch.ETIQUETTE_PREUVE_PRIX`) plutôt que
    `preuve_enquete` -- l'Analyst les reçoit avec le reste des preuves de
    l'opportunité (`opportunity_evidence` commun, aucune distinction au
    chargement, voir `app.pipeline.orchestrator._charger_preuves`)."""
    resultats_recherche = _rechercher_par_famille(
        hypothese, registre=registre, budget=budget,
        limite_par_requete=quotas["enqueteur_resultats_par_requete"],
        familles=("prix",), concurrents=[c.nom for c in concurrents],
    )["prix"]

    resultats_fetch_direct = [
        ResultatRecherche(
            url=f"https://{c.domaine}/pricing",
            titre=f"Tarifs -- {c.nom}",
            extrait="",
            horodatage_source=None,
            fournisseur="fetch_direct_pricing",
            requete_origine=f"{c.nom} /pricing (fetch direct)",
        )
        for c in concurrents
    ]

    return collecter_preuves(
        engine, resultats_recherche + resultats_fetch_direct,
        max_resultats=quotas["max_resultats_enquete_par_opportunite"],
        delai_entre_fetchs=quotas["enqueteur_fetch_delai_secondes"],
        opportunity_id=opportunity_id, budget=budget,
        etiquette=ETIQUETTE_PREUVE_PRIX,
    )


def enqueter_opportunite(
    engine: Engine,
    opportunity_id: str,
    hypothese: HypotheseEnqueteur,
    *,
    registre: RegistreFournisseurs,
    budget: BudgetTracker,
    quotas: dict,
) -> list[str]:
    """Enquête complète sur UNE opportunité : requêtes (3.1) -> fournisseurs
    actifs (3.1/3.2) -> sélection + fetch + rattachement à l'opportunité (3.3,
    étendue en 3.4), PUIS, sous-étape 3.4b, identification de concurrents par
    du code et enquête de prix si au moins un a été trouvé. Renvoie les
    `source_id` nouvellement stockés (peut être vide : aucun résultat, tout
    hors périmètre, ou plafond de budget déjà atteint -- jamais une
    exception)."""
    par_famille = _rechercher_par_famille(
        hypothese, registre=registre, budget=budget,
        limite_par_requete=quotas["enqueteur_resultats_par_requete"],
    )
    resultats = [resultat for famille in FAMILLES_UTILISEES for resultat in par_famille[famille]]

    source_ids: list[str] = []
    if resultats:
        source_ids = collecter_preuves(
            engine, resultats,
            max_resultats=quotas["max_resultats_enquete_par_opportunite"],
            delai_entre_fetchs=quotas["enqueteur_fetch_delai_secondes"],
            opportunity_id=opportunity_id, budget=budget,
        )

    concurrents = identifier_concurrents(
        [resultat for resultat in resultats if resultat.fournisseur == "magasin_interne"],
        par_famille["concurrence"],
    )
    if concurrents:
        source_ids += _enqueter_prix(
            engine, opportunity_id, hypothese, concurrents,
            registre=registre, budget=budget, quotas=quotas,
        )
    return source_ids
