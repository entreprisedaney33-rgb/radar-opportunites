"""Sous-étape 1.2 : chargement + validation stricte du lexique de douleur."""
import pytest

from app.lexique_douleur import LexiqueInvalide, charger_expressions


def test_charge_le_vrai_fichier():
    expressions = charger_expressions()
    assert len(expressions) >= 20
    cles = {e.cle for e in expressions}
    assert len(cles) == len(expressions)  # aucune clé en double
    assert all(e.langue in {"fr", "en"} for e in expressions)
    assert all(e.expression.strip() for e in expressions)


def test_fixture_valide_minimale(tmp_path):
    chemin = tmp_path / "lexique.yaml"
    chemin.write_text(
        "- cle: a\n  expression: \"manually\"\n  langue: en\n"
        "- cle: b\n  expression: \"à la main\"\n  langue: fr\n",
        encoding="utf-8",
    )
    expressions = charger_expressions(chemin)
    assert [e.cle for e in expressions] == ["a", "b"]


def test_champ_manquant_refuse(tmp_path):
    chemin = tmp_path / "lexique.yaml"
    chemin.write_text("- cle: a\n  langue: en\n", encoding="utf-8")
    with pytest.raises(LexiqueInvalide):
        charger_expressions(chemin)


def test_langue_inconnue_refusee(tmp_path):
    chemin = tmp_path / "lexique.yaml"
    chemin.write_text("- cle: a\n  expression: \"x\"\n  langue: de\n", encoding="utf-8")
    with pytest.raises(LexiqueInvalide):
        charger_expressions(chemin)


def test_expression_vide_refusee(tmp_path):
    chemin = tmp_path / "lexique.yaml"
    chemin.write_text("- cle: a\n  expression: \"  \"\n  langue: fr\n", encoding="utf-8")
    with pytest.raises(LexiqueInvalide):
        charger_expressions(chemin)


def test_cle_en_double_refusee(tmp_path):
    chemin = tmp_path / "lexique.yaml"
    chemin.write_text(
        "- cle: a\n  expression: \"x\"\n  langue: en\n"
        "- cle: a\n  expression: \"y\"\n  langue: en\n",
        encoding="utf-8",
    )
    with pytest.raises(LexiqueInvalide):
        charger_expressions(chemin)


def test_fichier_qui_n_est_pas_une_liste_refuse(tmp_path):
    chemin = tmp_path / "lexique.yaml"
    chemin.write_text("cle: a\n", encoding="utf-8")
    with pytest.raises(LexiqueInvalide):
        charger_expressions(chemin)
