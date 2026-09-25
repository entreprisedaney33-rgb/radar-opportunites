"""Adaptateur de démonstration — DONNÉES FICTIVES, clairement étiquetées.

Sert quand aucun accès réseau/API réel n'est disponible (§0 du cahier des
charges : « implémente le système avec des adaptateurs et des données de
démonstration clairement étiquetées »). Ne doit JAMAIS être confondu avec une
vraie collecte : chaque texte commence par `[DEMO]` et chaque URL utilise un
domaine `exemple.invalid` réservé par la RFC 2606 (ne résout jamais vers un
vrai site).
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.adapters.base import SignalBrut

_SIGNAUX_DEMO = [
    ("https://exemple.invalid/forum/rapprochement-factures", "operations_petites_entreprises",
     "[DEMO] Un gérant de PME décrit 6h/semaine passées à rapprocher manuellement emails et factures fournisseurs."),
    ("https://exemple.invalid/avis/cabinet-compta-devis", "services_professionnels",
     "[DEMO] Un cabinet comptable indique refuser des dossiers faute de temps pour la saisie des devis clients."),
    ("https://exemple.invalid/blog/e-commerce-retours", "e_commerce",
     "[DEMO] Une boutique en ligne évoque le coût du traitement manuel des retours et des réponses SAV répétitives."),
    ("https://exemple.invalid/forum/it-tickets-internes", "outils_internes_it",
     "[DEMO] Une DSI mentionne un backlog de tickets internes de premier niveau, toujours les mêmes questions."),
    ("https://exemple.invalid/annonce/appel-offre-public", "hypotheses_emergentes",
     "[DEMO] Un appel d'offres public mentionne un besoin de tri automatisé de candidatures, volume non précisé."),
    ("https://exemple.invalid/forum/flux-documentaire-juridique", "flux_documentaires",
     "[DEMO] Un cabinet juridique décrit la relecture manuelle de contrats types avant chaque signature."),
]


class AdaptateurDemo:
    id_source = "demo_fixtures"

    def collecter(self, budget_appels: int) -> list[SignalBrut]:
        maintenant = datetime.now(timezone.utc)
        return [
            SignalBrut(
                url=url,
                domaine="demo",
                texte=texte,
                date_publication=maintenant,
                type_source="demo",
                droits_collecte="donnée de démonstration fictive, aucune collecte réelle",
                flux_origine="demo_fixtures",
                type_flux="douleur",
            )
            for url, _secteur, texte in _SIGNAUX_DEMO[:budget_appels]
        ]
