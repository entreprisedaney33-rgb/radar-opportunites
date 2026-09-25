"""Reproduit deux déformations vues en usage réel (premier run manuel sur
Render, 2026-09-25) : le modèle enveloppe parfois sa réponse dans une clé
unique, et rend parfois une liste sous forme de chaîne JSON."""
import pytest

from app.adapters.model_client import _estimer_cout_eur, _normaliser_sortie_outil
from app.models_schemas import AnalystSortie, CriticSortie


def test_tarifs_sonnet_5_et_haiku_verifies_le_2026_09_25():
    """Sous-étape 0.7, point 6 : tarifs vérifiés sur
    platform.claude.com/docs/en/about-claude/pricing — Sonnet 5 est à
    2 $/10 $ (le prix "introductif" est devenu le prix standard depuis le
    1er septembre 2026), Haiku 4.5 était déjà correct à 1 $/5 $."""
    cout_haiku = _estimer_cout_eur("claude-haiku-4-5-20251001", 1_000_000, 1_000_000)
    cout_sonnet = _estimer_cout_eur("claude-sonnet-5", 1_000_000, 1_000_000)
    usd_vers_eur = 0.877

    assert cout_haiku == pytest.approx((1.0 + 5.0) * usd_vers_eur)
    assert cout_sonnet == pytest.approx((2.0 + 10.0) * usd_vers_eur)


def test_deballe_une_enveloppe_a_cle_unique():
    brut = {
        "repondre": {
            "opportunity_id": "o1",
            "criteres": [],
            "prochain_test_moins_couteux": "entretien",
            "niveau_preuve_global": "faible",
        }
    }
    normalise = _normaliser_sortie_outil(brut, AnalystSortie)
    resultat = AnalystSortie.model_validate(normalise)
    assert resultat.opportunity_id == "o1"


def test_deballe_avec_une_autre_cle_enveloppe():
    brut = {"parameter": {"opportunity_id": "o1", "criteres": [],
                           "prochain_test_moins_couteux": "x", "niveau_preuve_global": "moyen"}}
    normalise = _normaliser_sortie_outil(brut, AnalystSortie)
    assert AnalystSortie.model_validate(normalise).opportunity_id == "o1"


def test_ne_deballe_pas_si_les_champs_attendus_sont_deja_presents():
    brut = {"opportunity_id": "o1", "criteres": [], "prochain_test_moins_couteux": "x",
            "niveau_preuve_global": "faible"}
    assert _normaliser_sortie_outil(brut, AnalystSortie) == brut


def test_decode_une_liste_rendue_comme_chaine_json():
    brut = {
        "opportunity_id": "o1",
        "objections": '[{"texte": "Le dossier Analyst manque de preuves.", "source_ids": []}]',
        "decision": "a_verifier",
        "motif": "à vérifier",
    }
    normalise = _normaliser_sortie_outil(brut, CriticSortie)
    resultat = CriticSortie.model_validate(normalise)
    assert resultat.objections[0].texte == "Le dossier Analyst manque de preuves."


def test_chaine_non_json_reste_telle_quelle_et_la_validation_echoue_normalement():
    import pytest
    from pydantic import ValidationError

    brut = {"opportunity_id": "o1", "objections": "pas du json", "decision": "a_verifier", "motif": "x"}
    normalise = _normaliser_sortie_outil(brut, CriticSortie)
    with pytest.raises(ValidationError):
        CriticSortie.model_validate(normalise)
