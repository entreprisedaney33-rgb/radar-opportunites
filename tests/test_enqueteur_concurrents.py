"""Sous-étape 3.4b (AMELIORATIONS.md) : identification des concurrents par
du code (`app.enqueteur.concurrents`). Fonction pure, aucun réseau, aucun
modèle -- opère sur des `ResultatRecherche` déjà collectés (fixtures ici)."""
from __future__ import annotations

from datetime import datetime, timezone

from app.enqueteur.concurrents import Concurrent, identifier_concurrents
from app.enqueteur.fournisseurs import ResultatRecherche


def _resultat(url: str, titre: str, *, fournisseur: str = "reddit") -> ResultatRecherche:
    return ResultatRecherche(
        url=url, titre=titre, extrait=f"Extrait pour {titre}.",
        horodatage_source=datetime(2026, 9, 20, tzinfo=timezone.utc), fournisseur=fournisseur,
    )


def test_aucun_candidat_ne_donne_aucun_concurrent():
    assert identifier_concurrents([], []) == []


def test_identifie_un_concurrent_via_le_magasin_interne():
    """Point (a) du texte de 3.4b : nom = titre du résultat, domaine = URL.
    Titre avec un marqueur d'offre (« software ») : depuis la sous-étape
    3.15, le magasin interne est lui aussi filtré sur ce marqueur (voir le
    test dédié ci-dessous, qui vérifie le cas SANS marqueur)."""
    magasin_interne = [
        _resultat("https://www.concurrentx.example/blog/1", "ConcurrentX, un vrai software", fournisseur="magasin_interne")
    ]

    concurrents = identifier_concurrents(magasin_interne, [])

    assert concurrents == [Concurrent(nom="ConcurrentX, un vrai software", domaine="www.concurrentx.example")]


def test_magasin_interne_sans_marqueur_d_offre_n_est_plus_un_concurrent():
    """Sous-étape 3.15 : reproduit le faux positif réel trouvé en 3.14
    (rapports/POINT_ETAPE_2026-09-28.md, §4) -- un article TechCrunch sans
    rapport ("Mark Wahlberg is coming to Disrupt 2026"), rapproché par
    similarité lexicale via le magasin interne, ne portait AUCUN marqueur
    d'offre dans son titre. Avant cette sous-étape, le magasin interne
    n'exigeait aucun marqueur (contrairement à la famille `concurrence`) --
    corrigé : les deux sources sont maintenant filtrées de la même façon."""
    magasin_interne = [
        _resultat(
            "https://techcrunch.com/2026/09/26/mark-wahlberg-is-coming-to-disrupt-2026/",
            "Mark Wahlberg is coming to Disrupt 2026", fournisseur="magasin_interne",
        )
    ]

    assert identifier_concurrents(magasin_interne, []) == []


def test_identifie_un_concurrent_via_marqueur_d_offre_dans_le_titre_concurrence():
    """Point (b) : uniquement les résultats de la famille `concurrence` dont
    le TITRE contient un marqueur d'offre explicite."""
    resultats_concurrence = [
        _resultat("https://outilconcu.example/page", "OutilConcu, le meilleur tool pour ça"),
        _resultat("https://simple-article.example/page", "Comment gérer cette douleur sans outil"),
    ]

    concurrents = identifier_concurrents([], resultats_concurrence)

    assert concurrents == [Concurrent(nom="OutilConcu, le meilleur tool pour ça", domaine="outilconcu.example")]


def test_reconnait_chaque_marqueur_d_offre():
    marqueurs = ["tool", "software", "logiciel", "platform", "app", "SaaS"]
    resultats_concurrence = [
        _resultat(f"https://exemple{i}.example/x", f"Un {marqueur} formidable")
        for i, marqueur in enumerate(marqueurs)
    ]

    concurrents = identifier_concurrents([], resultats_concurrence)

    assert len(concurrents) == 3  # tronqué à MAX_CONCURRENTS, voir test dédié
    domaines = {c.domaine for c in concurrents}
    assert domaines == {"exemple0.example", "exemple1.example", "exemple2.example"}


def test_dedoublonne_par_domaine_entre_les_deux_sources():
    """Le même domaine trouvé par les deux sources ne compte qu'une fois --
    la première occurrence (magasin interne, point (a)) est conservée."""
    magasin_interne = [
        _resultat("https://meme-domaine.example/a", "Nom du magasin interne, un tool", fournisseur="magasin_interne")
    ]
    resultats_concurrence = [_resultat("https://meme-domaine.example/pricing", "Meme Domaine, un vrai software")]

    concurrents = identifier_concurrents(magasin_interne, resultats_concurrence)

    assert concurrents == [Concurrent(nom="Nom du magasin interne, un tool", domaine="meme-domaine.example")]


def test_plafonne_a_trois_concurrents():
    magasin_interne = [
        _resultat(f"https://interne{i}.example/x", f"Interne {i}, un tool", fournisseur="magasin_interne")
        for i in range(5)
    ]

    concurrents = identifier_concurrents(magasin_interne, [])

    assert len(concurrents) == 3


def test_ignore_un_resultat_sans_titre():
    magasin_interne = [_resultat("https://sans-titre.example/x", "   ", fournisseur="magasin_interne")]
    assert identifier_concurrents(magasin_interne, []) == []


def test_ignore_un_resultat_sans_domaine_exploitable():
    magasin_interne = [_resultat("pas-une-url-valide", "Un nom quand même", fournisseur="magasin_interne")]
    assert identifier_concurrents(magasin_interne, []) == []


def test_domaine_exclu_ignore_meme_avec_un_marqueur_d_offre_dans_le_titre():
    """Sous-étape 3.15 : second faux positif réel de la même enquête
    (rapports/POINT_ETAPE_2026-09-28.md, §4) -- un titre TechCrunch autour
    de l'accord cloud Anthropic-Akamai contenait bien un marqueur d'offre
    ("platform"), donc la famille `concurrence` (point b) l'acceptait déjà
    avant cette sous-étape. Le marqueur seul ne suffit pas : un domaine listé
    dans `domaines_exclus` reste toujours ignoré."""
    resultats_concurrence = [
        _resultat(
            "https://techcrunch.com/2026/09/25/anthropic-akamai-ai-cloud-platform/",
            "Anthropic and Akamai team up to build an AI cloud platform",
        )
    ]

    concurrents = identifier_concurrents(
        [], resultats_concurrence, domaines_exclus=frozenset({"techcrunch.com"}),
    )

    assert concurrents == []


def test_domaine_exclu_couvre_les_sous_domaines():
    resultats_concurrence = [_resultat("https://blog.medium.com/un-super-saas", "Un super SaaS pour ça")]

    concurrents = identifier_concurrents(
        [], resultats_concurrence, domaines_exclus=frozenset({"medium.com"}),
    )

    assert concurrents == []


def test_domaine_non_exclu_reste_un_concurrent_valide():
    """`domaines_exclus` ne doit filtrer QUE les domaines listés -- un vrai
    concurrent avec marqueur d'offre, sur un domaine absent de la liste,
    reste identifié normalement."""
    resultats_concurrence = [_resultat("https://vrai-concurrent.example/prix", "VraiConcurrent, un super tool")]

    concurrents = identifier_concurrents(
        [], resultats_concurrence, domaines_exclus=frozenset({"techcrunch.com", "reddit.com"}),
    )

    assert concurrents == [Concurrent(nom="VraiConcurrent, un super tool", domaine="vrai-concurrent.example")]


def test_les_trois_faux_positifs_ne_produisent_plus_de_concurrent():
    """Sous-étape 3.15, critère d'acceptation du plan : les faux positifs
    documentés (rapports/POINT_ETAPE_2026-09-28.md, §4, dossier 79a9d42a) ne
    doivent plus jamais produire de concurrent -- et donc plus jamais
    déclencher la famille `prix` (`_enqueter_prix`, seule porte d'entrée vers
    un fetch `/pricing`). Les deux premiers titres sont ceux cités mot pour
    mot dans le rapport ; le rapport ne détaille pas le contenu exact des 8
    sources `prix` de ce dossier au-delà de ces deux exemples (aucun accès
    aux logs Render ni à la base de production depuis cette session, hors
    ligne) -- le troisième cas reconstitue le même type d'échec (domaine du
    radar lui-même + marqueur d'offre dans le titre) sur un autre domaine de
    la liste d'exclusion, plutôt que d'inventer un troisième titre TechCrunch
    qui n'a pas été vérifié dans les logs réels (voir Journal — sous-étape
    3.15, écart par rapport au plan)."""
    domaines_exclus = frozenset({"techcrunch.com", "reddit.com", "producthunt.com", "news.ycombinator.com"})

    # 1. TechCrunch, sans marqueur d'offre, via le magasin interne (réel).
    faux_positif_1 = [
        _resultat(
            "https://techcrunch.com/2026/09/26/mark-wahlberg-is-coming-to-disrupt-2026/",
            "Mark Wahlberg is coming to Disrupt 2026", fournisseur="magasin_interne",
        )
    ]
    assert identifier_concurrents(faux_positif_1, [], domaines_exclus=domaines_exclus) == []

    # 2. TechCrunch, AVEC marqueur d'offre ("platform"), via la famille `concurrence` (réel).
    faux_positif_2 = [
        _resultat(
            "https://techcrunch.com/2026/09/25/anthropic-akamai-ai-cloud-platform/",
            "Anthropic and Akamai team up to build an AI cloud platform",
        )
    ]
    assert identifier_concurrents([], faux_positif_2, domaines_exclus=domaines_exclus) == []

    # 3. Reddit, avec marqueur d'offre ("tool"), via la famille `concurrence`
    # (reconstitué -- même famille d'échec, domaine différent, voir docstring).
    faux_positif_3 = [
        _resultat("https://www.reddit.com/r/SaaS/comments/abc123/", "Best free tool for invoicing right now")
    ]
    assert identifier_concurrents([], faux_positif_3, domaines_exclus=domaines_exclus) == []


def test_identification_est_pure_memes_entrees_memes_sorties():
    magasin_interne = [_resultat("https://a.example/x", "A", fournisseur="magasin_interne")]
    resultats_concurrence = [_resultat("https://b.example/x", "B, un vrai tool")]

    premier = identifier_concurrents(magasin_interne, resultats_concurrence)
    second = identifier_concurrents(magasin_interne, resultats_concurrence)

    assert premier == second
