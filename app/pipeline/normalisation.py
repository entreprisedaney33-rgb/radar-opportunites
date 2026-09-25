"""Secteur d'une opportunité — décidé par du code, jamais par un modèle
(§2 du cahier des charges), avec une provenance explicite (sous-étape 2.1
d'AMELIORATIONS.md).

Trois étages, aucun ne l'emporte sur celui du dessus :
1. `citation_verifiee` — le Scout propose un secteur connu du code, appuyé
   d'une citation retrouvée telle quelle (espaces/casse normalisés, rien de
   plus) dans le texte réellement collecté.
2. `flux` — secteur par défaut déclaré par le flux d'origine
   (`app/sources.yaml`, sous-étape 1.1).
3. `defaut` — filet historique par mots-clés, sinon `intersectoriel`.

La proposition du Scout (secteur + citation) n'existe pas encore à ce
stade du plan : elle arrive à la sous-étape 2.2, qui branchera ses deux
nouveaux champs de sortie sur les paramètres `secteur_propose` /
`citation_propose` de `inferer_secteur` ci-dessous. D'ici là, ces deux
paramètres restent `None` et seuls les étages 2 et 3 s'appliquent — ce qui
correspond exactement au comportement d'avant cette sous-étape, à
l'exception du nouvel étage 2 (secteur du flux), qui, lui, était prévu par
la sous-étape 1.1 mais jamais réellement branché jusqu'ici."""
from __future__ import annotations

import re
from dataclasses import dataclass

from app import config as cfg

_MOTS_CLES_SECTEUR = {
    "operations_petites_entreprises": ["pme", "gérant", "petite entreprise", "artisan"],
    "services_professionnels": ["cabinet", "comptable", "avocat", "conseil", "expert-comptable"],
    "flux_documentaires": ["contrat", "document", "pdf", "dossier", "relecture", "juridique"],
    "e_commerce": ["boutique", "e-commerce", "commande", "retour client", "sav"],
    "outils_internes_it": ["ticket", "dsi", "support interne", "informatique", "helpdesk"],
    "agents_ia": ["agent ia", "agent autonome", "assistant ia"],
    "nouvelles_interfaces": ["interface vocale", "réalité augmentée"],
    "apis_x402": ["x402", "paiement par api", "micropaiement"],
    "nouveaux_modeles": ["nouveau modèle", "claude", "gpt", "gemini"],
}

SECTEUR_PAR_DEFAUT = "intersectoriel"


@dataclass(frozen=True)
class ResultatSecteur:
    secteur: str
    provenance: str  # citation_verifiee|flux|defaut
    citation: str | None


def secteurs_valides() -> set[str]:
    """Liste exacte des catégories de secteur connues du code
    (`config/secteurs.yaml`) — publique : réutilisée par le prompt du Scout
    (sous-étape 2.2, `app/roles/scout.py`) pour ne jamais lui laisser
    inventer une catégorie."""
    groupes = cfg.secteurs().get("secteurs", {})
    return {secteur for liste in groupes.values() for secteur in liste}


def _normaliser_pour_comparaison(texte: str) -> str:
    """Espaces et casse uniquement (§2.1 : « rien de plus ») — pas d'accents,
    pas de ponctuation retirée, pour rester une vérification stricte."""
    return re.sub(r"\s+", " ", texte).strip().lower()


def _inferer_par_mots_cles(texte: str) -> str:
    texte_normalise = texte.lower()
    for secteur, mots in _MOTS_CLES_SECTEUR.items():
        for mot in mots:
            if re.search(re.escape(mot), texte_normalise):
                return secteur
    return SECTEUR_PAR_DEFAUT


def inferer_secteur(
    texte: str,
    secteur_defaut_flux: str | None,
    secteur_propose: str | None = None,
    citation_propose: str | None = None,
) -> ResultatSecteur:
    """Fonction pure : aucun appel modèle, aucun accès réseau."""
    if (
        secteur_propose is not None
        and secteur_propose in secteurs_valides()
        and citation_propose
        and _normaliser_pour_comparaison(citation_propose) in _normaliser_pour_comparaison(texte)
    ):
        return ResultatSecteur(secteur_propose, "citation_verifiee", citation_propose)

    if secteur_defaut_flux is not None:
        return ResultatSecteur(secteur_defaut_flux, "flux", None)

    return ResultatSecteur(_inferer_par_mots_cles(texte), "defaut", None)
