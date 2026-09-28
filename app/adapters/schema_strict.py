"""Durcit un schéma JSON généré par Pydantic pour le mode strict de l'API
Anthropic (sous-étape 3.13 d'AMELIORATIONS.md).

Cause vérifiée de la panne du 26/09/2026 (logs Render, requête
`req_011CfS9X5RPi6Wxd1w4Ww7fZ` et des centaines d'autres, identiques,
depuis) : `"strict": True` (ajouté en sous-étape 3.10 au bloc `outil` de
`app/adapters/model_client.py`) exige, pour chaque objet du schéma JSON
envoyé, `additionalProperties: false` -- absent du schéma que
`BaseModel.model_json_schema()` produit par défaut :

    Error code: 400 - {'type': 'error', 'error': {'type':
    'invalid_request_error', 'message': "tools.0.custom: For 'object' type,
    'additionalProperties' must be explicitly set to false"}}

Le mode strict exige aussi que la totalité des propriétés d'un objet soit
listée dans `required` (un champ optionnel reste optionnel via son TYPE --
`anyOf: [{...}, {"type": "null"}]`, généré tel quel par Pydantic pour tout
champ `X | None`, jamais en étant absent de `required`).

Fonction pure, récursive (un modèle Pydantic imbriqué produit un schéma avec
des sous-schémas sous `$defs`, référencés par `$ref` -- jamais aplatis) :
aucun accès réseau, aucune donnée inventée, seulement une transformation de
structure. Ne modifie jamais le schéma reçu en place (copie profonde)."""
from __future__ import annotations

import copy
from typing import Any


def rendre_schema_strict(schema_json: dict[str, Any]) -> dict[str, Any]:
    """Renvoie une COPIE de `schema_json` où chaque nœud objet
    (`"type": "object"` avec des `"properties"`) porte
    `additionalProperties: false` et une liste `required` couvrant la
    totalité de ses propriétés -- y compris dans les sous-schémas de
    `$defs` (modèles Pydantic imbriqués, ex. `AnalystSortie.criteres` ->
    `CritereAnalyst` -> `Affirmation`)."""
    schema_copie = copy.deepcopy(schema_json)
    _durcir_recursivement(schema_copie)
    return schema_copie


def _durcir_recursivement(noeud: Any) -> None:
    if isinstance(noeud, dict):
        if noeud.get("type") == "object" and isinstance(noeud.get("properties"), dict):
            noeud["additionalProperties"] = False
            noeud["required"] = list(noeud["properties"].keys())
        for valeur in noeud.values():
            _durcir_recursivement(valeur)
    elif isinstance(noeud, list):
        for item in noeud:
            _durcir_recursivement(item)
