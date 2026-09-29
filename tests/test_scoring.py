from app import config as cfg
from app.models_schemas import Affirmation, CritereAnalyst, TypeAffirmation
from app.scoring.engine import InfoSource, calculer_score, domaine_de_base, evaluer_critere

# Sous-étape 3.17 : deux sources de deux domaines différents -- le cas où deux
# faits observés valent bien 100 % (voir SCORING.md).
SOURCES = {
    "s1": InfoSource(domaine="exemple-a.test"),
    "s2": InfoSource(domaine="exemple-b.test"),
}


def _affirmation(type_, avec_source=True, source_id="s1"):
    return Affirmation(texte="fait", type=type_, source_ids=[source_id] if avec_source else [])


def test_ancres_evaluer_critere():
    # 0 critère fourni -> inconnu
    assert evaluer_critere(None, SOURCES) == (None, "inconnu")

    # aucune affirmation sourcée -> inconnu, même si le texte existe
    c_vide = CritereAnalyst(nom="x", affirmations=[], inconnues=["rien trouvé"])
    assert evaluer_critere(c_vide, SOURCES) == (None, "inconnu")

    # une seule affirmation observée/calculée sourcée -> 50%
    c_un_fort = CritereAnalyst(nom="x", affirmations=[_affirmation(TypeAffirmation.OBSERVE)])
    assert evaluer_critere(c_un_fort, SOURCES) == (0.5, "moyen")

    # seulement des hypothèses -> 50% aussi (indices partiels)
    c_hypothese = CritereAnalyst(nom="x", affirmations=[_affirmation(TypeAffirmation.HYPOTHESE)])
    assert evaluer_critere(c_hypothese, SOURCES) == (0.5, "moyen")

    # deux affirmations fortes sourcées, sur deux sources distinctes -> 100%
    c_deux_forts = CritereAnalyst(
        nom="x",
        affirmations=[
            _affirmation(TypeAffirmation.OBSERVE, source_id="s1"),
            _affirmation(TypeAffirmation.CALCULE, source_id="s2"),
        ],
    )
    assert evaluer_critere(c_deux_forts, SOURCES) == (1.0, "fort")

    # une affirmation forte mais SANS source -> ne compte pas (comme non fournie)
    c_sans_source = CritereAnalyst(nom="x", affirmations=[_affirmation(TypeAffirmation.OBSERVE, avec_source=False)])
    assert evaluer_critere(c_sans_source, SOURCES) == (None, "inconnu")


def test_poids_somment_a_100():
    total = sum(c["max"] for c in cfg.poids_scoring()["criteres"].values())
    assert total == 100


def test_score_brut_100_quand_tous_les_criteres_sont_forts():
    config = cfg.poids_scoring()
    criteres = [
        CritereAnalyst(nom=nom, affirmations=[_affirmation(TypeAffirmation.OBSERVE, source_id="s1"), _affirmation(TypeAffirmation.CALCULE, source_id="s2")])
        for nom in config["criteres"]
    ]
    resultat = calculer_score(criteres, config, SOURCES)
    assert resultat.score_brut == 100.0
    assert resultat.score_prudent == 100.0
    assert resultat.couverture_preuves == 1.0
    assert resultat.flags == []


def test_critere_inconnu_ne_penalise_pas_le_score_brut_mais_penalise_le_prudent():
    config = cfg.poids_scoring()
    noms = list(config["criteres"])
    criteres = [
        CritereAnalyst(nom=nom, affirmations=[_affirmation(TypeAffirmation.OBSERVE, source_id="s1"), _affirmation(TypeAffirmation.CALCULE, source_id="s2")])
        for nom in noms[1:]  # le premier critère reste inconnu (aucune ligne fournie)
    ]
    resultat = calculer_score(criteres, config, SOURCES)
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
    resultat = calculer_score([], config, SOURCES)  # aucun critère renseigné du tout
    assert resultat.score_prudent == 0.0
    assert resultat.score_brut == 0.0


# ------------------------------------------------------------ sous-étape 3.17 --
# « Fort » (100 %) exige deux affirmations observées/calculées citant deux
# sources DISTINCTES (domaines différents, ou signal d'origine + une autre).

def test_deux_affirmations_sur_la_meme_source_valent_50_pourcent():
    critere = CritereAnalyst(
        nom="x",
        affirmations=[
            _affirmation(TypeAffirmation.OBSERVE, source_id="s1"),
            _affirmation(TypeAffirmation.CALCULE, source_id="s1"),
        ],
    )
    assert evaluer_critere(critere, SOURCES) == (0.5, "moyen")


def test_deux_sources_du_meme_domaine_valent_50_pourcent():
    """Deux fils Hacker News distincts, aucun n'étant le signal d'origine :
    deux URL, mais un seul « site » -- pas deux sources indépendantes."""
    sources = {"h1": InfoSource(domaine="news.ycombinator.com"), "h2": InfoSource(domaine="news.ycombinator.com")}
    critere = CritereAnalyst(
        nom="x",
        affirmations=[
            _affirmation(TypeAffirmation.OBSERVE, source_id="h1"),
            _affirmation(TypeAffirmation.OBSERVE, source_id="h2"),
        ],
    )
    assert evaluer_critere(critere, sources) == (0.5, "moyen")


def test_deux_sous_domaines_du_meme_editeur_valent_50_pourcent():
    sources = {"a": InfoSource(domaine="www.lemonde.fr"), "b": InfoSource(domaine="abonne.lemonde.fr")}
    critere = CritereAnalyst(
        nom="x",
        affirmations=[
            _affirmation(TypeAffirmation.OBSERVE, source_id="a"),
            _affirmation(TypeAffirmation.OBSERVE, source_id="b"),
        ],
    )
    assert evaluer_critere(critere, sources) == (0.5, "moyen")


def test_signal_d_origine_plus_une_autre_source_du_meme_domaine_valent_100_pourcent():
    """L'exception écrite : le signal d'origine (un commentaire HN) compte
    comme une source à part, même face à un autre fil du même domaine."""
    sources = {
        "origine": InfoSource(domaine="news.ycombinator.com", origine=True),
        "autre": InfoSource(domaine="news.ycombinator.com"),
    }
    critere = CritereAnalyst(
        nom="x",
        affirmations=[
            _affirmation(TypeAffirmation.OBSERVE, source_id="origine"),
            _affirmation(TypeAffirmation.OBSERVE, source_id="autre"),
        ],
    )
    assert evaluer_critere(critere, sources) == (1.0, "fort")


def test_un_seul_signal_d_origine_cite_deux_fois_reste_a_50_pourcent():
    sources = {"origine": InfoSource(domaine="news.ycombinator.com", origine=True)}
    critere = CritereAnalyst(
        nom="x",
        affirmations=[
            _affirmation(TypeAffirmation.OBSERVE, source_id="origine"),
            _affirmation(TypeAffirmation.CALCULE, source_id="origine"),
        ],
    )
    assert evaluer_critere(critere, sources) == (0.5, "moyen")


def test_les_inferences_ne_font_jamais_100_pourcent():
    """Une inférence ou une hypothèse ne compte jamais pour un fait fort,
    quel qu'en soit le nombre et quelles que soient les sources."""
    critere = CritereAnalyst(
        nom="x",
        affirmations=[
            _affirmation(TypeAffirmation.HYPOTHESE, source_id="s1"),
            _affirmation(TypeAffirmation.HYPOTHESE, source_id="s2"),
            _affirmation(TypeAffirmation.HYPOTHESE, source_id="s1"),
        ],
    )
    assert evaluer_critere(critere, SOURCES) == (0.5, "moyen")


def test_un_fait_fort_plus_une_hypothese_sur_deux_sources_reste_a_50_pourcent():
    critere = CritereAnalyst(
        nom="x",
        affirmations=[
            _affirmation(TypeAffirmation.OBSERVE, source_id="s1"),
            _affirmation(TypeAffirmation.HYPOTHESE, source_id="s2"),
        ],
    )
    assert evaluer_critere(critere, SOURCES) == (0.5, "moyen")


def test_une_source_absente_de_la_table_ne_fait_jamais_un_fait_fort():
    critere = CritereAnalyst(
        nom="x",
        affirmations=[
            _affirmation(TypeAffirmation.OBSERVE, source_id="s1"),
            _affirmation(TypeAffirmation.OBSERVE, source_id="inconnue"),
        ],
    )
    assert evaluer_critere(critere, SOURCES) == (0.5, "moyen")


def test_un_dossier_dont_tout_repose_sur_une_seule_source_plafonne_a_50():
    config = cfg.poids_scoring()
    criteres = [
        CritereAnalyst(
            nom=nom,
            affirmations=[_affirmation(TypeAffirmation.OBSERVE, source_id="s1"), _affirmation(TypeAffirmation.CALCULE, source_id="s1")],
        )
        for nom in config["criteres"]
    ]
    resultat = calculer_score(criteres, config, SOURCES)
    assert resultat.score_prudent == 50.0
    assert all(v["niveau_preuve"] == "moyen" for v in resultat.valeurs.values())


def test_domaine_de_base():
    assert domaine_de_base("www.lemonde.fr") == "lemonde.fr"
    assert domaine_de_base("abonne.lemonde.fr") == "lemonde.fr"
    assert domaine_de_base("news.ycombinator.com") == "ycombinator.com"
    assert domaine_de_base("techcrunch.com") == "techcrunch.com"
    assert domaine_de_base("www.bbc.co.uk") == "bbc.co.uk"
    assert domaine_de_base("Blog.Exemple.COM:8080") == "exemple.com"


def test_la_version_des_poids_a_change_avec_la_regle():
    """Chaque score garde la version des poids utilisée à son calcul
    (config/poids_scoring.yaml) : la règle des sources distinctes est une
    nouvelle version, jamais un écrasement silencieux de l'ancienne."""
    assert cfg.poids_scoring()["version"] == "2026.09.2"
