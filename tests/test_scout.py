"""Sous-étape 2.2 : le Scout propose désormais son propre secteur, appuyé
d'une citation mot pour mot -- jamais un simple écho du secteur donné en
entrée. Tests sans réseau ni appel modèle réel (réponses simulées)."""
from app.adapters.model_client import AccesModeleIndisponible
from app.models_schemas import ScoutSortie
from app.pipeline.normalisation import secteurs_valides
from app.roles.scout import PROMPT_SYSTEME, _prompt_utilisateur, _scout_heuristique, executer_scout


def test_repli_heuristique_ne_propose_ni_secteur_ni_citation():
    """Le repli honnête n'invente rien -- il n'analyse pas le texte, donc il
    ne peut pas justifier un secteur par une citation."""
    sortie = _scout_heuristique("sig1", "un texte quelconque", "e_commerce")
    assert sortie.secteur is None
    assert sortie.secteur_citation is None


def test_prompt_utilisateur_liste_les_secteurs_valides_et_le_secteur_indicatif():
    prompt = _prompt_utilisateur("sig1", "texte du signal", "e_commerce")
    for secteur in secteurs_valides():
        assert secteur in prompt
    assert "e_commerce" in prompt
    assert "texte du signal" in prompt


def test_prompt_systeme_precise_que_null_vaut_mieux_qu_une_citation_approximative():
    assert "null" in PROMPT_SYSTEME.lower()
    assert "citation" in PROMPT_SYSTEME.lower()


def test_executer_scout_transmet_le_secteur_et_la_citation_du_modele():
    class FauxModelClient:
        def appeler_structure(self, **kwargs):
            return ScoutSortie(
                opportunity_candidate="t", buyer="b", pain="p", ai_mechanism="m", why_now="w",
                signal_ids=["sig1"], secteur="flux_documentaires", secteur_citation="extrait exact",
            )

    sortie, via_modele = executer_scout(
        signal_id="sig1", texte="texte contenant extrait exact quelque part",
        secteur="intersectoriel", model_client=FauxModelClient(), modele="m",
    )
    assert via_modele is True
    assert sortie.secteur == "flux_documentaires"
    assert sortie.secteur_citation == "extrait exact"


def test_executer_scout_repli_heuristique_si_modele_indisponible():
    class FauxModelClientIndisponible:
        def appeler_structure(self, **kwargs):
            raise AccesModeleIndisponible("test")

    sortie, via_modele = executer_scout(
        signal_id="sig1", texte="texte", secteur="e_commerce",
        model_client=FauxModelClientIndisponible(), modele="m",
    )
    assert via_modele is False
    assert sortie.secteur is None
    assert sortie.secteur_citation is None


def test_reste_de_la_sortie_scout_inchange_non_regression():
    """Point 4 : le reste de la sortie Scout (tous les champs déjà présents
    avant 2.2) n'est pas affecté par l'ajout de secteur_citation."""
    sortie = _scout_heuristique("sig1", "Premier titre.\nReste du texte ignoré ici.", "e_commerce")
    assert sortie.opportunity_candidate == "Premier titre."
    assert sortie.signal_ids == ["sig1"]
    assert sortie.missing_facts == ["acheteur", "mécanisme IA précis", "économie chiffrée"]
    assert sortie.cluster_id is None
