"""Fournisseur « moteur web » payant, derrière un drapeau, désactivé par
défaut (sous-étape 3.5 d'AMELIORATIONS.md). Premier candidat : Brave Search
API.

Vérifié le 25/09/2026 (point 1 du texte de 3.5, avant d'écrire ce module,
sources : https://brave.com/search/api/ et la documentation officielle du
point de terminaison Web Search) :
- Brave a retiré son ancien palier gratuit illimité en février 2026 (jusque-là
  ≥ 2000 requêtes/mois selon les comptes). Ce n'est donc PLUS un fournisseur
  « gratuit » au sens strict, contrairement à Algolia HN/Reddit/magasin
  interne (3.2).
- Offre actuelle, plan « Search » : 5 $ pour 1000 requêtes, avec 5 $ de
  crédit gratuit RENOUVELÉ CHAQUE MOIS (~1000 requêtes/mois avant
  facturation), 50 requêtes/seconde maximum.
- Une carte bancaire est exigée dès l'inscription, y compris pour rester
  dans le crédit gratuit mensuel (mesure anti-fraude documentée par Brave :
  non débitée tant qu'on reste sous le crédit) -- mais AUCUN plafond de
  dépense par défaut au-delà de ce crédit n'est documenté côté code de ce
  module : un tel plafond, s'il existe, se configure côté compte Brave /
  carte, hors du contrôle de ce fichier.
Conséquence directe sur ce module : `actif_par_defaut=False` partout, sans
aucune exception, y compris pour rester dans le crédit gratuit -- un premier
appel non désiré après activation peut engager une carte enregistrée sans
plafond garanti par ce code. Ne jamais activer sans que Mathéo ait
lui-même vérifié un plafond de dépense côté Brave, EN PLUS de la clé.

Interface identique à `app.enqueteur.fournisseurs.FournisseurRecherche`
(`nom`, `rechercher(requete, limite) -> list[ResultatRecherche]`) : un autre
moteur (Tavily, Serper...) se branche en écrivant une classe similaire et en
l'enregistrant à sa place -- rien à changer dans le pipeline ni dans les
autres fournisseurs pour ce faire.

Activation (LES DEUX sont nécessaires ; ni l'une ni l'autre n'est posée par
ce module ni par cette sous-étape -- point 3 du texte, « Ne pas activer ») :
- `RADAR_BRAVE_SEARCH_API_KEY` : la clé, saisie PAR MATHÉO LUI-MÊME dans les
  variables d'environnement Render -- jamais dans le code, les tests, la
  config ou un commit (garde-fou §0.2.4/§3.4 du cahier des charges). Ce
  module ne l'affiche jamais, ne la journalise jamais.
- `RADAR_ENQUETEUR_ACTIF_BRAVE_SEARCH=1` (ou true/yes) : même mécanisme que
  les fournisseurs gratuits
  (`app.enqueteur.fournisseurs.RegistreFournisseurs.est_actif`), aucun code
  nouveau nécessaire pour ça.

Ce module n'est enregistré dans AUCUN registre utilisé par le pipeline réel
(contrairement aux fournisseurs gratuits,
`app.enqueteur.fournisseurs_gratuits.construire_registre_fournisseurs_gratuits`,
appelée par `app.pipeline.orchestrator._phase_enquete`) : il reste un
squelette autonome et testable seul, comme l'ont été les sous-étapes
3.1/3.2/3.3 avant leur branchement réel en 3.4.
`definition_fournisseur_brave_search()` existe pour qu'un branchement futur
(sous-étape 4.3, « le fournisseur web payant... ne l'est qu'à ce palier »)
n'ait qu'à l'enregistrer dans un registre, sans reprendre ce fichier.
"""
from __future__ import annotations

import logging
import os
from urllib.parse import urlencode

from app.adapters.http import ErreurCollecte, get_with_retry
from app.enqueteur.fournisseurs import DefinitionFournisseur, ResultatRecherche

logger = logging.getLogger(__name__)

NOM = "brave_search"
VARIABLE_CLE = "RADAR_BRAVE_SEARCH_API_KEY"
URL_RECHERCHE = "https://api.search.brave.com/res/v1/web/search"
# Plafond documenté du paramètre `count` de l'API (résultats par appel) --
# indépendant de `limite`, qui peut être plus petit.
MAX_RESULTATS_PAR_APPEL = 20


class FournisseurWebPayantNonConfigure(Exception):
    """Levée par `rechercher` quand `RADAR_BRAVE_SEARCH_API_KEY` est absente
    -- attrapée comme n'importe quelle panne de fournisseur par l'appelant
    (`app.enqueteur.enqueteur._rechercher_par_famille`, qui isole déjà les
    pannes de fournisseur sans jamais faire planter les autres, ni
    l'opportunité en cours d'enquête)."""


class FournisseurBraveSearch:
    """Un appel HTTP (`rechercher`) = une "requête de recherche" au sens du
    compteur budget (`app.pipeline.budget.BudgetTracker`), même unité que
    pour les fournisseurs gratuits -- indépendamment du nombre de résultats
    renvoyés par cet appel."""

    nom = NOM

    def rechercher(self, requete: str, limite: int) -> list[ResultatRecherche]:
        cle = os.environ.get(VARIABLE_CLE)
        if not cle:
            raise FournisseurWebPayantNonConfigure(
                f"{VARIABLE_CLE} absente -- fournisseur {NOM!r} non configuré, jamais appelé sans elle."
            )

        parametres = urlencode({"q": requete, "count": min(limite, MAX_RESULTATS_PAR_APPEL)})
        url = f"{URL_RECHERCHE}?{parametres}"
        try:
            resp = get_with_retry(url, headers={"X-Subscription-Token": cle})
        except ErreurCollecte as exc:
            logger.warning("Fournisseur %s indisponible pour %r : %s", NOM, requete, exc)
            return []

        try:
            donnees = resp.json()
        except ValueError:
            logger.warning("Fournisseur %s : réponse non JSON pour %r.", NOM, requete)
            return []

        resultats: list[ResultatRecherche] = []
        for hit in donnees.get("web", {}).get("results", [])[:limite]:
            url_resultat = hit.get("url")
            titre = hit.get("title") or ""
            if not url_resultat or not titre:
                continue
            resultats.append(
                ResultatRecherche(
                    url=url_resultat,
                    titre=titre,
                    extrait=hit.get("description") or titre,
                    # Non exposé de façon fiable par l'API pour un résultat
                    # web générique (pas de champ date garanti) -- traité
                    # comme le plus ancien possible par la sélection en aval
                    # (`app.enqueteur.selection._cle_recence`), même
                    # convention que pour un hit Algolia HN sans `created_at`.
                    horodatage_source=None,
                    fournisseur=self.nom,
                )
            )
        return resultats


def definition_fournisseur_brave_search() -> DefinitionFournisseur:
    """`actif_par_defaut=False` -- point 3 du texte de 3.5 (« Ne pas
    activer. »). Un branchement futur (sous-étape 4.3) n'a qu'à faire
    `registre.enregistrer(definition_fournisseur_brave_search())`."""
    return DefinitionFournisseur(nom=NOM, fabrique=FournisseurBraveSearch, actif_par_defaut=False)
