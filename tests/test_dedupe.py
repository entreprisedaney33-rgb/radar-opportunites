from app.pipeline.dedupe import (
    canonicaliser_url,
    empreinte_contenu,
    proposer_cluster,
)


def test_canonicalisation_ignore_les_parametres_de_tracking():
    a = canonicaliser_url("https://Exemple.com/article/1?utm_source=hn&id=1")
    b = canonicaliser_url("https://exemple.com/article/1/?id=1&utm_campaign=x")
    assert a == b


def test_empreinte_identique_pour_texte_identique_a_la_casse_pres():
    assert empreinte_contenu("Bonjour   le monde") == empreinte_contenu("bonjour le monde")


def test_empreinte_differente_pour_textes_differents():
    assert empreinte_contenu("bonjour le monde") != empreinte_contenu("au revoir le monde")


def _opp(id_, secteur, acheteur, probleme):
    return {"id": id_, "secteur": secteur, "acheteur": acheteur, "probleme": probleme}


def test_meme_secteur_meme_acheteur_texte_tres_proche_fusion_automatique():
    existantes = [_opp("o1", "services_professionnels", "cabinet comptable indépendant",
                        "rapprochement manuel des factures fournisseurs chaque semaine")]
    suggestion = proposer_cluster(
        secteur="services_professionnels",
        acheteur="cabinet comptable indépendant",
        texte="rapprochement manuel des factures fournisseurs chaque semaine",
        existantes=existantes,
    )
    assert suggestion is not None
    assert suggestion.fusion_automatique is True
    assert suggestion.opportunity_id == "o1"


def test_vocabulaire_commun_mais_acheteur_different_ne_fusionne_pas():
    """Cas explicite du cahier des charges : ne pas fusionner des acheteurs
    différents sur le seul vocabulaire commun."""
    existantes = [_opp("o1", "e_commerce", "boutique de vêtements en ligne",
                        "traitement manuel des retours clients tous les jours")]
    suggestion = proposer_cluster(
        secteur="e_commerce",
        acheteur="fabricant de meubles",
        texte="traitement manuel des retours clients tous les jours",
        existantes=existantes,
    )
    assert suggestion is None


def test_secteurs_differents_jamais_fusionnes():
    existantes = [_opp("o1", "e_commerce", "boutique en ligne", "rapprochement manuel de factures")]
    suggestion = proposer_cluster(
        secteur="services_professionnels",
        acheteur="boutique en ligne",
        texte="rapprochement manuel de factures",
        existantes=existantes,
    )
    assert suggestion is None


def test_similarite_faible_ne_declenche_aucune_suggestion():
    existantes = [_opp("o1", "e_commerce", "boutique en ligne", "gestion des stocks en entrepôt")]
    suggestion = proposer_cluster(
        secteur="e_commerce", acheteur="boutique en ligne",
        texte="campagne publicitaire sur les réseaux sociaux", existantes=existantes,
    )
    assert suggestion is None
