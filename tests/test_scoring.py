from app import config as cfg
from app.models_schemas import Affirmation, CritereAnalyst, TypeAffirmation
from app.scoring.engine import calculer_score, evaluer_critere


def _affirmation(type_, avec_source=True):
    return Affirmation(texte="fait", type=type_, source_ids=["s1"] if avec_source else [])


def test_ancres_evaluer_critere():
    # 0 critère fourni -> inconnu
    assert evaluer_critere(None) == (None, "inconnu")

    # aucune affirmation sourcée -> inconnu, même si le texte existe
    c_vide = CritereAnalyst(nom="x", affirmations=[], inconnues=["rien trouvé"])
    assert evaluer_critere(c_vide) == (None, "inconnu")

    # une seule affirmation observée/calculée sourcée -> 50%
    c_un_fort = CritereAnalyst(nom="x", affirmations=[_affirmation(TypeAffirmation.OBSERVE)])
    assert evaluer_critere(c_un_fort) == (0.5, "moyen")

    # seulement des hypothèses -> 50% aussi (indices partiels)
    c_hypothese = CritereAnalyst(nom="x", affirmations=[_affirmation(TypeAffirmation.HYPOTHESE)])
    assert evaluer_critere(c_hypothese) == (0.5, "moyen")

    # deux affirmations fortes sourcées -> 100%
    c_deux_forts = CritereAnalyst(nom="x", affirmations=[_affirmation(TypeAffirmation.OBSERVE), _affirmation(TypeAffirmation.CALCULE)])
    assert evaluer_critere(c_deux_forts) == (1.0, "fort")

    # une affirmation forte mais SANS source -> ne compte pas (comme non fournie)
    c_sans_source = CritereAnalyst(nom="x", affirmations=[_affirmation(TypeAffirmation.OBSERVE, avec_source=False)])
    assert evaluer_critere(c_sans_source) == (None, "inconnu")


def test_poids_somment_a_100():
    total = sum(c["max"] for c in cfg.poids_scoring()["criteres"].values())
    assert total == 100


def test_score_brut_100_quand_tous_les_criteres_sont_forts():
    config = cfg.poids_scoring()
    criteres = [
        CritereAnalyst(nom=nom, affirmations=[_affirmation(TypeAffirmation.OBSERVE), _affirmation(TypeAffirmation.CALCULE)])
        for nom in config["criteres"]
    ]
    resultat = calculer_score(criteres, config)
    assert resultat.score_brut == 100.0
    assert resultat.score_prudent == 100.0
    assert resultat.couverture_preuves == 1.0
    assert resultat.flags == []


def test_critere_inconnu_ne_penalise_pas_le_score_brut_mais_penalise_le_prudent():
    config = cfg.poids_scoring()
    noms = list(config["criteres"])
    criteres = [
        CritereAnalyst(nom=nom, affirmations=[_affirmation(TypeAffirmation.OBSERVE), _affirmation(TypeAffirmation.CALCULE)])
        for nom in noms[1:]  # le premier critère reste inconnu (aucune ligne fournie)
    ]
    resultat = calculer_score(criteres, config)
    # score_brut : calculé uniquement sur les critères connus -> 100%
    assert resultat.score_brut == 100.0
    # score_prudent : le critère inconnu ne rapporte que fraction_inconnue * son max (0 par défaut)
    max_premier = config["criteres"][noms[0]]["max"]
    assert resultat.score_prudent == 100.0 - max_premier
    assert resultat.couverture_preuves < 1.0
    assert f"{noms[0]}: inconnu" in resultat.flags


def test_score_ne_defaute_jamais_a_50_pourcent_pour_inconnu():
    """Garde-fou explicite du cahier des charges : une case sans preuve ne
    reçoit PAS automatiquement 50%."""
    config = cfg.poids_scoring()
    resultat = calculer_score([], config)  # aucun critère renseigné du tout
    assert resultat.score_prudent == 0.0
    assert resultat.score_brut == 0.0
