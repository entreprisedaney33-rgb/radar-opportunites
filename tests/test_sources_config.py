"""Sous-étape 1.1 : chargement + validation stricte de app/sources.yaml —
un type ou un secteur inconnu est une erreur au démarrage, jamais un défaut
silencieux."""
import pytest

from app.sources import CHEMIN_PAR_DEFAUT, ConfigSourcesInvalide, SourceConfig, charger_sources, subreddits_douleur


def _ecrire(tmp_path, contenu):
    chemin = tmp_path / "sources.yaml"
    chemin.write_text(contenu, encoding="utf-8")
    return chemin


def test_le_fichier_reel_charge_sans_erreur():
    sources = charger_sources()
    assert CHEMIN_PAR_DEFAUT.exists()
    assert len(sources) == 17  # 4 historiques + 3 reddit (1.1) + 10 reddit (1.2)
    ids = {s.id for s in sources}
    assert ids == {
        "hn_rss", "hn_show_rss", "producthunt_rss", "techcrunch_rss",
        "reddit_smallbusiness_rss", "reddit_accounting_rss", "reddit_ecommerce_rss",
        "reddit_msp_rss", "reddit_sysadmin_rss", "reddit_bookkeeping_rss", "reddit_tax_rss",
        "reddit_entrepreneur_rss", "reddit_freelance_rss", "reddit_shopify_rss", "reddit_fba_rss",
        "reddit_propertymanagement_rss", "reddit_logistics_rss",
    }
    types = {s.id: s.type for s in sources}
    assert types["hn_rss"] == "offre"  # reclassé en sous-étape 1.3 (AMELIORATIONS.md)
    assert types["hn_show_rss"] == "offre"
    assert types["producthunt_rss"] == "offre"
    assert types["techcrunch_rss"] == "offre"
    assert types["reddit_smallbusiness_rss"] == "douleur"
    assert types["reddit_accounting_rss"] == "douleur"
    assert types["reddit_ecommerce_rss"] == "douleur"
    for id_reddit_1_2 in (
        "reddit_msp_rss", "reddit_sysadmin_rss", "reddit_bookkeeping_rss", "reddit_tax_rss",
        "reddit_entrepreneur_rss", "reddit_freelance_rss", "reddit_shopify_rss", "reddit_fba_rss",
        "reddit_propertymanagement_rss", "reddit_logistics_rss",
    ):
        assert types[id_reddit_1_2] == "douleur"


def test_subreddits_douleur_extrait_les_noms_depuis_les_sources_reelles():
    """Sous-étape 1.2 : une seule liste de vérité (app/sources.yaml) pour le
    connecteur de recherche — pas de deuxième liste de subs dupliquée."""
    subs = subreddits_douleur()
    assert "smallbusiness" in subs
    assert "Accounting" in subs
    assert "ecommerce" in subs
    assert "msp" in subs
    assert "sysadmin" in subs
    assert "PropertyManagement" in subs
    assert len(subs) == len(set(subs))  # dédoublonné
    # Les flux `offre` (Show HN, Product Hunt, TechCrunch) n'en font jamais partie.
    assert "show" not in [s.lower() for s in subs]


def test_subreddits_douleur_ignore_les_flux_offre_et_non_reddit(tmp_path):
    liste = [
        SourceConfig(
            id="a", nom="Reddit douleur", url="https://www.reddit.com/r/exemple/.rss", type="douleur",
            secteur_par_defaut=None, langue="fr", actif=True, budget_appels_par_nuit=10,
        ),
        SourceConfig(
            id="b", nom="Reddit offre", url="https://www.reddit.com/r/exemple_offre/.rss", type="offre",
            secteur_par_defaut=None, langue="fr", actif=True, budget_appels_par_nuit=10,
        ),
        SourceConfig(
            id="c", nom="Pas Reddit", url="https://hnrss.org/frontpage", type="douleur",
            secteur_par_defaut=None, langue="fr", actif=True, budget_appels_par_nuit=10,
        ),
        SourceConfig(
            id="d", nom="Même sub, doublon", url="https://www.reddit.com/r/exemple/hot/.rss", type="douleur",
            secteur_par_defaut=None, langue="fr", actif=True, budget_appels_par_nuit=10,
        ),
    ]
    assert subreddits_douleur(liste) == ["exemple"]


def test_config_valide_minimale(tmp_path):
    chemin = _ecrire(tmp_path, """
- id: a
  nom: "Source A"
  url: "https://exemple.invalid/a"
  type: douleur
  secteur_par_defaut: e_commerce
  langue: fr
  actif: true
  budget_appels_par_nuit: 5
""")
    sources = charger_sources(chemin)
    assert len(sources) == 1
    assert sources[0].secteur_par_defaut == "e_commerce"
    assert sources[0].actif is True


def test_secteur_par_defaut_peut_etre_absent(tmp_path):
    chemin = _ecrire(tmp_path, """
- id: a
  nom: "Source A"
  url: "https://exemple.invalid/a"
  type: offre
  secteur_par_defaut: null
  langue: en
  actif: true
""")
    sources = charger_sources(chemin)
    assert sources[0].secteur_par_defaut is None
    assert sources[0].budget_appels_par_nuit == 10  # valeur par défaut


def test_type_inconnu_leve_une_erreur(tmp_path):
    chemin = _ecrire(tmp_path, """
- id: a
  nom: "Source A"
  url: "https://exemple.invalid/a"
  type: neutre
  secteur_par_defaut: null
  langue: fr
  actif: true
""")
    with pytest.raises(ConfigSourcesInvalide, match="type"):
        charger_sources(chemin)


def test_secteur_inconnu_leve_une_erreur(tmp_path):
    chemin = _ecrire(tmp_path, """
- id: a
  nom: "Source A"
  url: "https://exemple.invalid/a"
  type: douleur
  secteur_par_defaut: secteur_qui_n_existe_pas
  langue: fr
  actif: true
""")
    with pytest.raises(ConfigSourcesInvalide, match="secteur_par_defaut"):
        charger_sources(chemin)


def test_champ_manquant_leve_une_erreur(tmp_path):
    chemin = _ecrire(tmp_path, """
- id: a
  nom: "Source A"
  url: "https://exemple.invalid/a"
  type: douleur
""")
    with pytest.raises(ConfigSourcesInvalide, match="manquant"):
        charger_sources(chemin)


def test_identifiant_en_double_leve_une_erreur(tmp_path):
    chemin = _ecrire(tmp_path, """
- id: a
  nom: "Source A"
  url: "https://exemple.invalid/a"
  type: douleur
  secteur_par_defaut: null
  langue: fr
  actif: true
- id: a
  nom: "Source A bis"
  url: "https://exemple.invalid/a2"
  type: offre
  secteur_par_defaut: null
  langue: fr
  actif: true
""")
    with pytest.raises(ConfigSourcesInvalide, match="double"):
        charger_sources(chemin)


def test_fichier_qui_n_est_pas_une_liste_leve_une_erreur(tmp_path):
    chemin = _ecrire(tmp_path, "cle: valeur\n")
    with pytest.raises(ConfigSourcesInvalide, match="liste"):
        charger_sources(chemin)
