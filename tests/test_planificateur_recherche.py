"""Sous-étape 1.2 : rotation et respect de l'intervalle du planificateur de
recherche — fonction pure, aucun réseau ni base. Généralisé en sous-étape 1.4
pour être partagé par les connecteurs Reddit ET Hacker News (même mécanisme,
identifiants distingués par `source`)."""
from datetime import datetime, timedelta, timezone

from app.pipeline.planificateur_recherche import FluxRecherche, choisir_flux_a_visiter

MAINTENANT = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)


def _flux(sub: str, cle: str, source: str = "reddit") -> FluxRecherche:
    return FluxRecherche(source=source, parametre=sub, expression_cle=cle, expression_texte=f"{cle} texte")


def test_flux_jamais_visite_est_prioritaire():
    a = _flux("smallbusiness", "manually")
    b = _flux("smallbusiness", "spreadsheet")
    dernieres_visites = {b.id: MAINTENANT - timedelta(hours=1)}  # b déjà visité récemment, a jamais

    choisis = choisir_flux_a_visiter(
        [b, a], dernieres_visites, maintenant=MAINTENANT, intervalle_heures=6, max_par_passage=10,
    )

    assert choisis == [a]  # b n'est pas dû (visité il y a moins de 6h)


def test_flux_pas_encore_du_est_exclu():
    a = _flux("smallbusiness", "manually")
    dernieres_visites = {a.id: MAINTENANT - timedelta(hours=2)}

    choisis = choisir_flux_a_visiter(
        [a], dernieres_visites, maintenant=MAINTENANT, intervalle_heures=6, max_par_passage=10,
    )

    assert choisis == []


def test_flux_du_pile_a_l_intervalle_est_inclus():
    a = _flux("smallbusiness", "manually")
    dernieres_visites = {a.id: MAINTENANT - timedelta(hours=6)}

    choisis = choisir_flux_a_visiter(
        [a], dernieres_visites, maintenant=MAINTENANT, intervalle_heures=6, max_par_passage=10,
    )

    assert choisis == [a]


def test_rotation_du_plus_ancien_au_plus_recent():
    a = _flux("smallbusiness", "a")
    b = _flux("smallbusiness", "b")
    c = _flux("smallbusiness", "c")
    dernieres_visites = {
        a.id: MAINTENANT - timedelta(hours=48),
        b.id: MAINTENANT - timedelta(hours=100),
        # c jamais visité : toujours le plus prioritaire
    }

    choisis = choisir_flux_a_visiter(
        [a, b, c], dernieres_visites, maintenant=MAINTENANT, intervalle_heures=6, max_par_passage=10,
    )

    assert choisis == [c, b, a]


def test_troncature_a_max_par_passage():
    flux = [_flux("smallbusiness", f"e{i}") for i in range(10)]  # tous jamais visités

    choisis = choisir_flux_a_visiter(
        flux, {}, maintenant=MAINTENANT, intervalle_heures=6, max_par_passage=4,
    )

    assert len(choisis) == 4


def test_liste_vide():
    assert choisir_flux_a_visiter([], {}, maintenant=MAINTENANT, intervalle_heures=6, max_par_passage=10) == []


def test_id_distingue_les_sources_meme_avec_le_meme_parametre_et_la_meme_expression():
    """Sous-étape 1.4 : Reddit et HN partagent désormais le même planificateur
    générique — leurs identifiants de rotation ne doivent jamais se confondre,
    même si un subreddit et un tag HN portaient le même texte et la même clé
    d'expression."""
    reddit = _flux("comment", "manually", source="reddit")
    hn = _flux("comment", "manually", source="hn")

    assert reddit.id != hn.id
    assert reddit.id == "reddit_recherche:comment:manually"
    assert hn.id == "hn_recherche:comment:manually"

    # Visiter l'un ne doit jamais être confondu avec avoir visité l'autre.
    dernieres_visites = {reddit.id: MAINTENANT - timedelta(hours=1)}
    choisis = choisir_flux_a_visiter(
        [reddit, hn], dernieres_visites, maintenant=MAINTENANT, intervalle_heures=6, max_par_passage=10,
    )
    assert choisis == [hn]  # reddit visité il y a moins de 6h, hn jamais visité
