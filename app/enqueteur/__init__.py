"""L'Enquêteur (étape 3 d'AMELIORATIONS.md) : donne à l'Analyst plusieurs
sources réelles par opportunité, collectées par du code déterministe — jamais
par un modèle (§7 du cahier des charges).

Sous-étape 3.1 (squelette) : interface `FournisseurRecherche` + registre
(`fournisseurs.py`), générateur de requêtes pur (`gabarits.py`). Sous-étape
3.2 : les 3 fournisseurs gratuits (`fournisseurs_gratuits.py`, Algolia HN,
Reddit, magasin interne). Sous-étape 3.3 : fetch, extraction et stockage des
pages (`fetch.py`), sélection (`selection.py`). Sous-étape 3.4 : branchement
réel dans le pipeline (`enqueteur.py::enqueter_opportunite`, appelé par
`app.pipeline.orchestrator._phase_enquete` -- ordre Scout -> Enquêteur ->
Analyst -> Critic).
"""
