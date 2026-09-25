"""Normalisation légère d'un signal brut : secteur proposé par mots-clés
(règle simple, pas de modèle — le Scout affinera ensuite)."""
from __future__ import annotations

import re

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


def inferer_secteur(texte: str) -> str:
    texte_normalise = texte.lower()
    for secteur, mots in _MOTS_CLES_SECTEUR.items():
        for mot in mots:
            if re.search(re.escape(mot), texte_normalise):
                return secteur
    return SECTEUR_PAR_DEFAUT
