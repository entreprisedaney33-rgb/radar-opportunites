"""Sous-étape 3.13 : `app.adapters.schema_strict.rendre_schema_strict` --
fonction pure, testée directement sur les trois schémas réels envoyés à
l'API (ScoutSortie/AnalystSortie/CriticSortie), pas seulement sur un exemple
synthétique -- c'est exactement ce que `model_client.py::_un_appel` envoie."""
from __future__ import annotations

from app.adapters.schema_strict import rendre_schema_strict
from app.models_schemas import AnalystSortie, CriticSortie, ScoutSortie


def _noeuds_objet(schema: dict) -> list[dict]:
    """Tous les nœuds `{"type": "object", "properties": {...}}`, au premier
    niveau et dans `$defs` -- récursion volontairement naïve (pas de
    `_durcir_recursivement` réutilisé : on veut une vérification
    INDÉPENDANTE de l'implémentation)."""
    trouves: list[dict] = []

    def visiter(noeud):
        if isinstance(noeud, dict):
            if noeud.get("type") == "object" and isinstance(noeud.get("properties"), dict):
                trouves.append(noeud)
            for v in noeud.values():
                visiter(v)
        elif isinstance(noeud, list):
            for item in noeud:
                visiter(item)

    visiter(schema)
    return trouves


def test_schema_simple_sans_imbrication_scout_sortie():
    brut = ScoutSortie.model_json_schema()
    assert brut.get("additionalProperties") is None or brut.get("additionalProperties") is not False

    durci = rendre_schema_strict(brut)
    assert durci["additionalProperties"] is False
    assert set(durci["required"]) == set(durci["properties"].keys())


def test_schema_avec_imbrications_analyst_sortie_durci_partout():
    durci = rendre_schema_strict(AnalystSortie.model_json_schema())
    noeuds = _noeuds_objet(durci)
    assert len(noeuds) >= 4  # AnalystSortie + CritereAnalyst + Affirmation + ValeurFinanciere (au moins)
    for noeud in noeuds:
        assert noeud["additionalProperties"] is False, noeud
        assert set(noeud["required"]) == set(noeud["properties"].keys()), noeud


def test_schema_critic_sortie_durci_partout():
    durci = rendre_schema_strict(CriticSortie.model_json_schema())
    noeuds = _noeuds_objet(durci)
    assert len(noeuds) >= 2  # CriticSortie + Objection
    for noeud in noeuds:
        assert noeud["additionalProperties"] is False
        assert set(noeud["required"]) == set(noeud["properties"].keys())


def test_ne_modifie_pas_le_schema_recu():
    brut = ScoutSortie.model_json_schema()
    brut_avant = dict(brut)
    rendre_schema_strict(brut)
    assert brut == brut_avant  # copie profonde, jamais en place


def test_idempotent():
    une_fois = rendre_schema_strict(AnalystSortie.model_json_schema())
    deux_fois = rendre_schema_strict(une_fois)
    assert une_fois == deux_fois


def test_champ_optionnel_reste_requis_mais_type_nullable():
    """`ScoutSortie.secteur: str | None = None` -- optionnel côté Python,
    mais le mode strict exige qu'il soit dans `required` malgré tout (le
    JSON Schema Pydantic le type déjà en `anyOf: [.., {"type": "null"}]`,
    inchangé par la transformation -- c'est CE typage qui le rend
    facultatif, pas son absence de `required`)."""
    durci = rendre_schema_strict(ScoutSortie.model_json_schema())
    assert "secteur" in durci["required"]
    champ = durci["properties"]["secteur"]
    assert any(sous.get("type") == "null" for sous in champ.get("anyOf", []))
