"""Sous-étape 2.1 : le secteur d'une opportunité devient une affirmation
sourcée — `inferer_secteur` est une fonction pure à trois étages, aucun
étage inférieur ne l'emportant sur celui du dessus (citation_verifiee >
flux > defaut). Aucun appel modèle, aucun accès réseau."""
from app.pipeline.normalisation import SECTEUR_PAR_DEFAUT, ResultatSecteur, inferer_secteur


def test_citation_verifiee_quand_le_secteur_propose_est_connu_et_la_citation_retrouvee():
    texte = "On perd des heures chaque mois à faire le rapprochement bancaire à la main."
    resultat = inferer_secteur(
        texte, secteur_defaut_flux="e_commerce",
        secteur_propose="flux_documentaires", citation_propose="rapprochement bancaire à la main",
    )
    assert resultat == ResultatSecteur("flux_documentaires", "citation_verifiee", "rapprochement bancaire à la main")


def test_secteur_flux_quand_aucune_proposition_du_scout():
    resultat = inferer_secteur("texte quelconque sans rien de particulier", secteur_defaut_flux="e_commerce")
    assert resultat == ResultatSecteur("e_commerce", "flux", None)


def test_defaut_par_mots_cles_quand_ni_citation_ni_flux():
    resultat = inferer_secteur("Notre boutique e-commerce croule sous les demandes de SAV.", secteur_defaut_flux=None)
    assert resultat == ResultatSecteur("e_commerce", "defaut", None)


def test_defaut_intersectoriel_quand_rien_ne_correspond():
    resultat = inferer_secteur("Un texte totalement générique, sans aucun mot-clé connu.", secteur_defaut_flux=None)
    assert resultat == ResultatSecteur(SECTEUR_PAR_DEFAUT, "defaut", None)


def test_citation_partiellement_inventee_ne_valide_jamais_letage_1():
    texte = "On perd des heures chaque mois à faire le rapprochement bancaire à la main."
    resultat = inferer_secteur(
        texte, secteur_defaut_flux="e_commerce",
        secteur_propose="flux_documentaires", citation_propose="rapprochement bancaire automatisé par IA",
    )
    # La citation n'existe pas telle quelle dans le texte -> retombe sur le flux, jamais sur le secteur proposé.
    assert resultat == ResultatSecteur("e_commerce", "flux", None)


def test_citation_avec_espaces_ou_casse_differents_reste_verifiee():
    texte = "On perd   des heures\nchaque mois à faire le Rapprochement   Bancaire à la main."
    resultat = inferer_secteur(
        texte, secteur_defaut_flux=None,
        secteur_propose="flux_documentaires", citation_propose="rapprochement bancaire à la main",
    )
    assert resultat == ResultatSecteur("flux_documentaires", "citation_verifiee", "rapprochement bancaire à la main")


def test_secteur_propose_inconnu_du_code_traite_comme_absent():
    texte = "citation bidon présente dans le texte"
    resultat = inferer_secteur(
        texte, secteur_defaut_flux="e_commerce",
        secteur_propose="secteur_qui_nexiste_pas", citation_propose="citation bidon présente dans le texte",
    )
    assert resultat == ResultatSecteur("e_commerce", "flux", None)


def test_citation_absente_ne_valide_jamais_letage_1_meme_avec_secteur_connu():
    resultat = inferer_secteur(
        "un texte quelconque", secteur_defaut_flux="e_commerce",
        secteur_propose="flux_documentaires", citation_propose=None,
    )
    assert resultat == ResultatSecteur("e_commerce", "flux", None)
