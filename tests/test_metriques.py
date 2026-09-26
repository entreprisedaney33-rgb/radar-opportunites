from datetime import date, datetime, timezone

from sqlalchemy import insert

from app.metriques import calculer_metriques, formater_comparaison, main
from app.storage.schema import journal_http, opportunities, opportunity_evidence, scores, sources, usage_events

JOUR = date(2026, 1, 15)
JOUR_AUTRE = date(2026, 1, 16)


def _dt(jour: date, heure: int = 12) -> datetime:
    return datetime(jour.year, jour.month, jour.day, heure, tzinfo=timezone.utc)


def _creer_opportunite(engine, id_, *, jour, statut, secteur, secteur_provenance=None):
    with engine.begin() as cx:
        cx.execute(insert(opportunities).values(
            id=id_, titre=f"titre {id_}", acheteur="acheteur", probleme="probleme",
            mecanisme_ia="mecanisme", secteur=secteur, secteur_provenance=secteur_provenance,
            statut=statut, cluster_id=None, date_creation=_dt(jour), date_maj=_dt(jour),
        ))


def _creer_source(engine, id_, *, jour=JOUR, flux_origine=None, requete_origine=None, etiquette=None):
    with engine.begin() as cx:
        cx.execute(insert(sources).values(
            id=id_, url_canonique=f"https://exemple.invalid/{id_}", domaine="exemple",
            date_publication=None, date_collecte=_dt(jour), type="rss",
            extrait="extrait", empreinte=id_, droits_collecte="test",
            flux_origine=flux_origine, requete_origine=requete_origine, etiquette=etiquette,
        ))


def _rattacher_preuve(engine, opportunity_id, source_id, *, claim="preuve"):
    with engine.begin() as cx:
        cx.execute(insert(opportunity_evidence).values(
            id=f"ev-{opportunity_id}-{source_id}", opportunity_id=opportunity_id, source_id=source_id,
            claim=claim, type="observe", independant=True, date_creation=_dt(JOUR),
        ))


def _noter(engine, opportunity_id, *, score_prudent, decision_critic):
    with engine.begin() as cx:
        cx.execute(insert(scores).values(
            id=f"sc-{opportunity_id}", opportunity_id=opportunity_id, run_id="run-test",
            version_poids="v1", valeurs_json={}, score_brut=score_prudent, score_prudent=score_prudent,
            couverture_preuves=0.5, flags_json=[], decision_critic=decision_critic, date_creation=_dt(JOUR),
        ))


def _cout(engine, id_, montant, jour, *, role=None, opportunity_id=None, issue=None, sortie_tronquee=None):
    with engine.begin() as cx:
        cx.execute(insert(usage_events).values(
            id=id_, run_id="run-test", fournisseur="anthropic", modele_ou_actor="test",
            appels=1, tokens_in=100, tokens_out=50, cout_declare_ou_estime=montant, devise="EUR",
            date_creation=_dt(jour), role=role, opportunity_id=opportunity_id,
            issue=issue, sortie_tronquee=sortie_tronquee,
        ))


def _appel_http(engine, id_, *, flux_ou_fournisseur, code_http=None, erreur=None, jour=JOUR, duree_ms=42.0):
    with engine.begin() as cx:
        cx.execute(insert(journal_http).values(
            id=id_, horodatage=_dt(jour), hote="exemple.invalid", flux_ou_fournisseur=flux_ou_fournisseur,
            code_http=code_http, erreur=erreur, duree_ms=duree_ms,
        ))


def _construire_jeu_de_test(engine):
    # 4 opportunités repérées le JOUR, 3 notées, 1 encore "nouveau".
    _creer_opportunite(engine, "opp1", jour=JOUR, statut="rejete", secteur="intersectoriel")
    _creer_opportunite(engine, "opp2", jour=JOUR, statut="incertain", secteur="services_professionnels")
    _creer_opportunite(engine, "opp3", jour=JOUR, statut="a_revoir", secteur="e_commerce")
    _creer_opportunite(engine, "opp4", jour=JOUR, statut="nouveau", secteur="intersectoriel")
    # Une 5e opportunité, un autre jour -- ne doit JAMAIS apparaître dans les métriques du JOUR.
    _creer_opportunite(engine, "opp5", jour=JOUR_AUTRE, statut="nouveau", secteur="intersectoriel")

    for i in range(1, 5):
        _creer_source(engine, f"src{i}a")
    _creer_source(engine, "src2b")
    _creer_source(engine, "src3b")
    _creer_source(engine, "src3c")

    _rattacher_preuve(engine, "opp1", "src1a")
    _rattacher_preuve(engine, "opp2", "src2a")
    _rattacher_preuve(engine, "opp2", "src2b")
    _rattacher_preuve(engine, "opp3", "src3a")
    _rattacher_preuve(engine, "opp3", "src3b")
    _rattacher_preuve(engine, "opp3", "src3c")
    _rattacher_preuve(engine, "opp4", "src4a")

    _noter(engine, "opp1", score_prudent=20.0, decision_critic="rejeter")
    _noter(engine, "opp2", score_prudent=50.0, decision_critic="a_verifier")
    _noter(engine, "opp3", score_prudent=90.0, decision_critic="eligible_revue_humaine")
    # opp4 : pas encore de score (toujours "nouveau").

    _cout(engine, "cout1", 0.05, JOUR)
    _cout(engine, "cout2", 0.10, JOUR)
    _cout(engine, "cout3", 99.0, JOUR_AUTRE)  # un autre jour -- ne doit pas compter


def test_metriques_sur_jeu_de_test_complet(engine_test):
    _construire_jeu_de_test(engine_test)

    m = calculer_metriques(engine_test, JOUR)

    assert m["jour"] == "2026-01-15"
    assert m["opportunites_reperees"] == 4
    assert m["opportunites_analysees"] == 3
    assert m["par_statut"] == {"rejete": 1, "incertain": 1, "a_revoir": 1, "nouveau": 1}
    assert m["par_decision_critic"] == {"rejeter": 1, "a_verifier": 1, "eligible_revue_humaine": 1}

    assert m["score_prudent"]["min"] == 20.0
    assert m["score_prudent"]["max"] == 90.0
    assert m["score_prudent"]["mediane"] == 50.0
    assert m["score_prudent"]["p90"] == 90.0
    assert m["score_prudent"]["nb_superieur_60"] == 1
    assert m["score_prudent"]["nb_superieur_80"] == 1

    # opp2 (services_professionnels) et opp3 (e_commerce) sont hors intersectoriel -> 2/4.
    assert m["part_hors_intersectoriel"] == 0.5
    assert m["par_secteur"] == {"intersectoriel": 2, "services_professionnels": 1, "e_commerce": 1}
    # Sous-étape 2.2, point 3 : aucune opportunité de ce jeu de test n'a de
    # secteur_provenance (créées sans -- comme tout l'historique avant 2.1).
    assert m["par_secteur_provenance"] == {"aucune": 4}

    assert m["sources_par_dossier"]["min"] == 1
    assert m["sources_par_dossier"]["max"] == 3
    assert m["sources_par_dossier"]["mediane"] == 2.0
    # opp1 et opp4 n'ont qu'une seule source -> 2/4.
    assert m["sources_par_dossier"]["part_une_seule_source"] == 0.5
    # Aucune preuve `preuve_enquete` dans ce jeu de test (sous-étape 3.4).
    assert m["sources_par_dossier"]["par_fournisseur"] == {}

    # Le type d'objection n'existe pas encore dans le modèle -- jamais inventé.
    assert m["objections_critic_par_type"] is None

    # Sous-étape 1.4 : le jeu de test ci-dessus ne pose que des preuves
    # "preuve" (pas "Scout: ..."), donc aucune de ces répartitions ne doit
    # rien trouver -- voir test_metriques_repartition_par_flux_et_expression
    # ci-dessous pour le cas qui en pose.
    assert m["par_type_flux"] == {}
    assert m["par_flux"] == {}
    assert m["top_10_expressions_lexique"] == {}
    assert m["signaux_concurrence_stockes"] == 0

    assert m["cout_jour_eur"] == 0.15
    assert m["cout_moyen_par_dossier_analyse_eur"] == round(0.15 / 3, 4)


def test_sources_par_dossier_ventilees_par_fournisseur(engine_test):
    """Sous-étape 3.4, point 5 : sources par dossier ventilées par
    fournisseur de l'Enquêteur -- seules les sources étiquetées
    `preuve_enquete` (app.enqueteur.fetch.ETIQUETTE_PREUVE_ENQUETE) ou, depuis
    la sous-étape 3.4b, `prix` (app.enqueteur.fetch.ETIQUETTE_PREUVE_PRIX)
    comptent ici, jamais un `signal_concurrence` ni une source normale."""
    _creer_opportunite(engine_test, "opp1", jour=JOUR, statut="a_revoir", secteur="e_commerce")

    _creer_source(engine_test, "src-scout", flux_origine="r/ecommerce")  # étiquette=None : source normale
    _creer_source(engine_test, "src-hn", flux_origine="algolia_hn", etiquette="preuve_enquete")
    _creer_source(engine_test, "src-reddit-a", flux_origine="reddit", etiquette="preuve_enquete")
    _creer_source(engine_test, "src-reddit-b", flux_origine="reddit", etiquette="preuve_enquete")
    _creer_source(engine_test, "src-offre", flux_origine="Show HN", etiquette="signal_concurrence")

    _rattacher_preuve(engine_test, "opp1", "src-scout")
    _rattacher_preuve(engine_test, "opp1", "src-hn")
    _rattacher_preuve(engine_test, "opp1", "src-reddit-a")
    _rattacher_preuve(engine_test, "opp1", "src-reddit-b")
    # src-offre n'est jamais rattachée en preuve ici (elle ne le serait pas
    # non plus par le vrai pipeline, sous-étape 1.1) -- posée quand même pour
    # prouver qu'elle ne fausserait rien si elle l'était par erreur.

    m = calculer_metriques(engine_test, JOUR)

    assert m["sources_par_dossier"]["par_fournisseur"] == {"algolia_hn": 1, "reddit": 2}


def test_sources_par_dossier_par_fournisseur_compte_aussi_les_preuves_prix(engine_test):
    """Sous-étape 3.4b : une page trouvée pour la famille de requêtes `prix`
    (étiquette `prix`, pas `preuve_enquete`) reste une preuve d'enquête au
    sens de cette ventilation -- sinon elle disparaîtrait silencieusement de
    ce compteur dès qu'un concurrent est identifié."""
    _creer_opportunite(engine_test, "opp1", jour=JOUR, statut="a_revoir", secteur="e_commerce")

    _creer_source(engine_test, "src-hn", flux_origine="algolia_hn", etiquette="preuve_enquete")
    _creer_source(engine_test, "src-prix-recherche", flux_origine="reddit", etiquette="prix")
    _creer_source(engine_test, "src-prix-direct", flux_origine="fetch_direct_pricing", etiquette="prix")

    _rattacher_preuve(engine_test, "opp1", "src-hn")
    _rattacher_preuve(engine_test, "opp1", "src-prix-recherche")
    _rattacher_preuve(engine_test, "opp1", "src-prix-direct")

    m = calculer_metriques(engine_test, JOUR)

    assert m["sources_par_dossier"]["par_fournisseur"] == {
        "algolia_hn": 1, "reddit": 1, "fetch_direct_pricing": 1,
    }


def test_metriques_cout_par_role_et_par_opportunite(engine_test):
    """Sous-étape 0.7 : les colonnes role/opportunity_id d'usage_events sont
    ventilées par app.metriques — l'historique (role=None) est regroupé sous
    'sans_role', jamais ignoré ni jamais crashé."""
    _construire_jeu_de_test(engine_test)
    # opp1 : deux appels tracés (scout puis analyst) -> 0,05 + 0,03 = 0,08.
    _cout(engine_test, "cout-scout-opp1", 0.02, JOUR, role="scout", opportunity_id="opp1")
    _cout(engine_test, "cout-analyst-opp1", 0.06, JOUR, role="analyst", opportunity_id="opp1")
    # opp2 : un seul appel tracé -> 0,04.
    _cout(engine_test, "cout-critic-opp2", 0.04, JOUR, role="critic", opportunity_id="opp2")

    m = calculer_metriques(engine_test, JOUR)

    # cout1 (0.05) et cout2 (0.10) de _construire_jeu_de_test n'ont pas de
    # role -> regroupés sous "sans_role".
    assert m["cout_par_role_eur"]["sans_role"] == round(0.05 + 0.10, 4)
    assert m["cout_par_role_eur"]["scout"] == 0.02
    assert m["cout_par_role_eur"]["analyst"] == 0.06
    assert m["cout_par_role_eur"]["critic"] == 0.04

    # Moyenne UNIQUEMENT sur les lignes avec opportunity_id (opp1: 0.08, opp2: 0.04).
    assert m["cout_moyen_par_opportunite_eur"] == round((0.08 + 0.04) / 2, 4)


def test_metriques_fiabilite_sorties_taux_et_cout_perdu(engine_test):
    """Sous-étape 3.10, point 4 : taux de sorties valides et coût des appels
    perdus, par rôle -- "valide"/"normalisee" comptent comme exploitées,
    "relancee"/"perdue" comme non exploitées ; seule "perdue" entre dans
    `cout_appels_perdus_eur` (au sens strict : la tentative qui n'a produit
    AUCUN résultat, même après relance)."""
    _construire_jeu_de_test(engine_test)
    # Critic : 1 valide (0.02), 1 paire relancée/perdue (0.01 + 0.015).
    _cout(engine_test, "c-critic-valide", 0.02, JOUR, role="critic", issue="valide")
    _cout(engine_test, "c-critic-relancee", 0.01, JOUR, role="critic", issue="relancee")
    _cout(engine_test, "c-critic-perdue", 0.015, JOUR, role="critic", issue="perdue")
    # Analyst : 1 normalisee (0.03).
    _cout(engine_test, "c-analyst-normalisee", 0.03, JOUR, role="analyst", issue="normalisee")
    # Un compteur de l'Enquêteur (jamais un rôle modèle) : jamais dans fiabilite_sorties.
    _cout(engine_test, "c-enqueteur", 0.0, JOUR, role="enqueteur_recherche")

    m = calculer_metriques(engine_test, JOUR)
    fiab = m["fiabilite_sorties"]

    assert "enqueteur_recherche" not in fiab
    critic = fiab["critic"]
    assert critic["appels"] == 3
    assert critic["valides"] == 1
    assert critic["relancees"] == 1
    assert critic["perdues"] == 1
    assert critic["taux_sorties_valides"] == round(1 / 3, 4)
    assert critic["cout_appels_perdus_eur"] == 0.015  # UNIQUEMENT "perdue"
    assert critic["cout_non_exploite_eur"] == round(0.01 + 0.015, 4)  # "relancee" + "perdue"

    analyst = fiab["analyst"]
    assert analyst["appels"] == 1
    assert analyst["normalisees"] == 1
    assert analyst["taux_sorties_valides"] == 1.0
    assert analyst["cout_appels_perdus_eur"] == 0.0


def test_metriques_fiabilite_sorties_troncature_et_historique_sans_issue(engine_test):
    """`sortie_tronquee` compté indépendamment de `issue` (une sortie peut
    être tronquée ET valider quand même, cas limite) ; une ligne antérieure
    à la sous-étape 3.10 (`issue` NULL) est groupée sous
    `sans_donnee_fiabilite`, jamais ignorée ni jamais crashée, et exclue du
    dénominateur du taux."""
    _construire_jeu_de_test(engine_test)
    _cout(engine_test, "c-scout-tronque-valide", 0.02, JOUR, role="scout", issue="valide", sortie_tronquee=True)
    _cout(engine_test, "c-scout-historique", 0.01, JOUR, role="scout")  # issue=None : avant 3.10

    m = calculer_metriques(engine_test, JOUR)
    scout = m["fiabilite_sorties"]["scout"]

    assert scout["appels"] == 2
    assert scout["tronquees"] == 1
    assert scout["sans_donnee_fiabilite"] == 1
    assert scout["valides"] == 1
    assert scout["taux_sorties_valides"] == 1.0  # dénominateur = 2 - 1 (sans_donnee) = 1


def _ligne_usage(engine, id_, *, jour=JOUR, fournisseur="anthropic", modele="claude-sonnet-5",
                  tokens_in=None, tokens_out=None, cout=0.0, role=None):
    with engine.begin() as cx:
        cx.execute(insert(usage_events).values(
            id=id_, run_id="run-test", fournisseur=fournisseur, modele_ou_actor=modele,
            appels=1, tokens_in=tokens_in, tokens_out=tokens_out, cout_declare_ou_estime=cout,
            devise="EUR", date_creation=_dt(jour), role=role, opportunity_id=None,
        ))


def test_cout_recalcule_tarifs_courants_diffère_de_l_ancien_tarif(engine_test):
    """Sous-étape 3.6 (préalable) : deux appels Sonnet 5 journalisés à
    l'ANCIEN tarif (3 $/15 $, taux 0,92 — celui utilisé avant la correction de
    la sous-étape 0.7) doivent être recalculés au tarif COURANT de
    config/tarifs.yaml (2 $/10 $, taux 0,877), pas au tarif d'origine."""
    from app.adapters.model_client import estimer_cout_eur

    ancien_tarif_usd = 3.0 + 15.0  # 1 000 000 tokens_in + 1 000 000 tokens_out, ancien tarif
    ancien_cout_eur = round(ancien_tarif_usd * 0.92, 4)
    _ligne_usage(
        engine_test, "sonnet-ancien-tarif", modele="claude-sonnet-5",
        tokens_in=1_000_000, tokens_out=1_000_000, cout=ancien_cout_eur,
    )

    m = calculer_metriques(engine_test, JOUR)

    attendu_courant = estimer_cout_eur("claude-sonnet-5", 1_000_000, 1_000_000)
    recalcul = m["cout_jour_recalcule_tarifs_courants"]
    assert recalcul["eur"] == round(attendu_courant, 4)
    assert recalcul["eur"] != m["cout_jour_eur"]
    assert recalcul["ecart_vs_enregistre_eur"] == round(attendu_courant - m["cout_jour_eur"], 4)
    assert recalcul["evenements_anthropic_couverts"] == 1
    assert recalcul["evenements_anthropic_sans_tokens"] == 0


def test_cout_recalcule_compte_a_part_les_evenements_anthropic_sans_tokens(engine_test):
    """Un appel modèle qui a échoué (réseau, voir model_client.py) journalise
    fournisseur="anthropic" avec tokens_in/tokens_out=None et cout=0.0 : il ne
    peut pas être recalculé, mais ne doit jamais être confondu avec un
    évènement réellement couvert par le recalcul."""
    _ligne_usage(engine_test, "sonnet-echec-reseau", tokens_in=None, tokens_out=None, cout=0.0)

    m = calculer_metriques(engine_test, JOUR)

    recalcul = m["cout_jour_recalcule_tarifs_courants"]
    assert recalcul["eur"] == 0.0
    assert recalcul["evenements_anthropic_couverts"] == 0
    assert recalcul["evenements_anthropic_sans_tokens"] == 1


def test_cout_recalcule_ignore_les_evenements_non_anthropic(engine_test):
    """Les compteurs de l'Enquêteur (fournisseur=algolia_hn|reddit|..., jamais
    "anthropic") ne coûtent jamais rien et n'ont jamais de tokens — ils ne
    doivent apparaître ni dans le recalcul, ni dans son compte d'évènements
    sans tokens (ce n'est pas une lacune, ils n'ont juste rien à voir avec un
    appel modèle)."""
    _ligne_usage(
        engine_test, "enqueteur-algolia", fournisseur="algolia_hn", modele="algolia_hn",
        tokens_in=None, tokens_out=None, cout=0.0, role="enqueteur_recherche",
    )

    m = calculer_metriques(engine_test, JOUR)

    recalcul = m["cout_jour_recalcule_tarifs_courants"]
    assert recalcul["eur"] == 0.0
    assert recalcul["evenements_anthropic_couverts"] == 0
    assert recalcul["evenements_anthropic_sans_tokens"] == 0


def test_metriques_compteurs_enqueteur(engine_test):
    """Sous-étape 3.1 : compteurs journaliers de l'Enquêteur (requêtes de
    recherche, fetchs de page — role="enqueteur_recherche"/"enqueteur_fetch",
    toujours à coût 0 en V1) et les plafonds configurés, sortis de
    config/quotas.yaml."""
    from app import config as cfg

    _construire_jeu_de_test(engine_test)
    _cout(engine_test, "recherche-1", 0.0, JOUR, role="enqueteur_recherche")
    _cout(engine_test, "recherche-2", 0.0, JOUR, role="enqueteur_recherche")
    _cout(engine_test, "fetch-1", 0.0, JOUR, role="enqueteur_fetch")

    m = calculer_metriques(engine_test, JOUR)

    quotas = cfg.quotas()
    assert m["enqueteur"] == {
        "requetes_recherche_jour": 2,
        "plafond_requetes_recherche_par_jour": quotas["max_requetes_recherche_par_jour"],
        "fetchs_pages_jour": 1,
        "plafond_fetchs_pages_par_jour": quotas["max_fetchs_pages_par_jour"],
    }
    # Gratuit en V1 : ne pollue jamais le coût du jour ni le coût par rôle.
    assert m["cout_par_role_eur"].get("enqueteur_recherche", 0.0) == 0.0
    assert m["cout_par_role_eur"].get("enqueteur_fetch", 0.0) == 0.0


def test_metriques_compteurs_enqueteur_a_zero_sans_evenement(engine_test):
    from app import config as cfg

    _construire_jeu_de_test(engine_test)
    m = calculer_metriques(engine_test, JOUR)
    quotas = cfg.quotas()
    assert m["enqueteur"]["requetes_recherche_jour"] == 0
    assert m["enqueteur"]["fetchs_pages_jour"] == 0
    assert m["enqueteur"]["plafond_requetes_recherche_par_jour"] == quotas["max_requetes_recherche_par_jour"]
    assert m["enqueteur"]["plafond_fetchs_pages_par_jour"] == quotas["max_fetchs_pages_par_jour"]


def test_appels_http_par_flux_compte_429_403_autres_erreurs_et_succes(engine_test):
    """Sous-étape 3.7, point 2 : par flux/fournisseur, le jour -- nombre
    d'appels, 429, 403, autres erreurs, taux de succès."""
    _construire_jeu_de_test(engine_test)
    _appel_http(engine_test, "h1", flux_ou_fournisseur="rss:product_hunt", code_http=200)
    _appel_http(engine_test, "h2", flux_ou_fournisseur="rss:product_hunt", code_http=200)
    _appel_http(engine_test, "h3", flux_ou_fournisseur="rss:product_hunt", code_http=429)
    _appel_http(engine_test, "h4", flux_ou_fournisseur="rss:product_hunt", code_http=403)
    _appel_http(engine_test, "h5", flux_ou_fournisseur="rss:product_hunt", erreur="timeout")
    _appel_http(engine_test, "h6", flux_ou_fournisseur="reddit_recherche:smallbusiness:manually_en", code_http=200)
    # Un autre jour -- ne doit jamais compter dans les métriques du JOUR.
    _appel_http(engine_test, "h7", flux_ou_fournisseur="rss:product_hunt", code_http=200, jour=JOUR_AUTRE)

    m = calculer_metriques(engine_test, JOUR)

    assert m["appels_http_par_flux"]["rss:product_hunt"] == {
        "appels": 5, "http_429": 1, "http_403": 1, "autres_erreurs": 1, "succes": 2, "taux_succes": 0.4,
    }
    assert m["appels_http_par_flux"]["reddit_recherche:smallbusiness:manually_en"] == {
        "appels": 1, "http_429": 0, "http_403": 0, "autres_erreurs": 0, "succes": 1, "taux_succes": 1.0,
    }


def test_appels_http_par_flux_vide_sans_appel_journalise(engine_test):
    _construire_jeu_de_test(engine_test)
    m = calculer_metriques(engine_test, JOUR)
    assert m["appels_http_par_flux"] == {}


def test_metriques_repartition_par_flux_et_expression(engine_test):
    """Sous-étape 1.4, point 2 : répartition des opportunités par type de
    flux (douleur/offre), par flux, par expression du lexique (top 10), et
    nombre d'items `signal_concurrence` stockés -- calculée à partir de la
    preuve d'ORIGINE posée par le Scout (claim `"Scout: ..."`)."""
    _creer_opportunite(engine_test, "opp_douleur", jour=JOUR, statut="nouveau", secteur="intersectoriel")
    # Opportunité fusionnée à partir de DEUX signaux (dedup) : deux preuves
    # d'origine "Scout:", donc comptée deux fois (voir docstring de
    # calculer_metriques) -- une fois par flux/expression qui l'a trouvée.
    _creer_opportunite(engine_test, "opp_fusionnee", jour=JOUR, statut="nouveau", secteur="intersectoriel")

    _creer_source(
        engine_test, "src_reddit", flux_origine="Reddit r/smallbusiness — recherche « manually »",
        requete_origine="manually",
    )
    _creer_source(
        engine_test, "src_hn_1", flux_origine="Hacker News — recherche « manually » (Ask HN)",
        requete_origine="manually",
    )
    _creer_source(
        engine_test, "src_hn_2", flux_origine="Hacker News — recherche « spreadsheet » (commentaires)",
        requete_origine="spreadsheet",
    )
    _creer_source(engine_test, "src_offre", flux_origine="Product Hunt", etiquette="signal_concurrence")

    _rattacher_preuve(engine_test, "opp_douleur", "src_reddit", claim="Scout: douleur reddit")
    _rattacher_preuve(engine_test, "opp_fusionnee", "src_hn_1", claim="Scout: douleur hn 1")
    _rattacher_preuve(engine_test, "opp_fusionnee", "src_hn_2", claim="Scout: douleur hn 2")
    # Une preuve qui n'est PAS de l'origine (posée par l'Analyst, pas le
    # Scout) ne doit jamais compter ici.
    _rattacher_preuve(engine_test, "opp_douleur", "src_hn_2", claim="Analyst: preuve supplémentaire")

    m = calculer_metriques(engine_test, JOUR)

    assert m["par_type_flux"] == {"douleur": 3}  # aucune opportunité n'est jamais issue d'un flux `offre`
    assert m["par_flux"] == {
        "Reddit r/smallbusiness — recherche « manually »": 1,
        "Hacker News — recherche « manually » (Ask HN)": 1,
        "Hacker News — recherche « spreadsheet » (commentaires)": 1,
    }
    assert m["top_10_expressions_lexique"] == {"manually": 2, "spreadsheet": 1}
    # src_offre est stocké le JOUR (date_collecte) : compté même si aucune
    # opportunité ne le cite jamais.
    assert m["signaux_concurrence_stockes"] == 1


def test_metriques_repartition_par_secteur_provenance(engine_test):
    """Sous-étape 2.2, point 3 : répartition des opportunités du jour par
    provenance du secteur (citation_verifiee/flux/defaut, posée en 2.1) --
    NULL (historique pré-2.1) regroupé sous 'aucune', jamais ignoré."""
    _creer_opportunite(
        engine_test, "opp_cv", jour=JOUR, statut="nouveau",
        secteur="flux_documentaires", secteur_provenance="citation_verifiee",
    )
    _creer_opportunite(
        engine_test, "opp_flux1", jour=JOUR, statut="nouveau",
        secteur="e_commerce", secteur_provenance="flux",
    )
    _creer_opportunite(
        engine_test, "opp_flux2", jour=JOUR, statut="nouveau",
        secteur="e_commerce", secteur_provenance="flux",
    )
    _creer_opportunite(
        engine_test, "opp_defaut", jour=JOUR, statut="nouveau",
        secteur="intersectoriel", secteur_provenance="defaut",
    )
    _creer_opportunite(
        engine_test, "opp_historique", jour=JOUR, statut="nouveau",
        secteur="intersectoriel",  # secteur_provenance absente -> NULL
    )

    m = calculer_metriques(engine_test, JOUR)

    assert m["par_secteur_provenance"] == {
        "citation_verifiee": 1, "flux": 2, "defaut": 1, "aucune": 1,
    }


def test_metriques_jour_vide_ne_plante_pas(engine_test):
    _construire_jeu_de_test(engine_test)

    m = calculer_metriques(engine_test, date(2026, 1, 1))  # aucune donnée ce jour-là

    assert m["opportunites_reperees"] == 0
    assert m["opportunites_analysees"] == 0
    assert m["par_statut"] == {}
    assert m["par_decision_critic"] == {}
    assert m["score_prudent"] == {
        "min": None, "mediane": None, "p90": None, "max": None,
        "nb_superieur_60": 0, "nb_superieur_80": 0,
    }
    assert m["par_secteur"] == {}
    assert m["part_hors_intersectoriel"] is None
    assert m["sources_par_dossier"]["part_une_seule_source"] is None
    assert m["cout_jour_eur"] == 0.0
    assert m["cout_jour_recalcule_tarifs_courants"] == {
        "eur": 0.0, "ecart_vs_enregistre_eur": 0.0,
        "evenements_anthropic_couverts": 0, "evenements_anthropic_sans_tokens": 0,
    }
    assert m["cout_moyen_par_dossier_analyse_eur"] is None
    assert m["cout_par_role_eur"] == {}
    assert m["cout_moyen_par_opportunite_eur"] is None


def test_metriques_ignore_les_autres_jours(engine_test):
    """opp5 (JOUR_AUTRE) et cout3 (JOUR_AUTRE) ne doivent jamais fuiter dans
    les métriques du JOUR, ni inversement."""
    _construire_jeu_de_test(engine_test)

    m_autre = calculer_metriques(engine_test, JOUR_AUTRE)
    assert m_autre["opportunites_reperees"] == 1
    assert m_autre["cout_jour_eur"] == 99.0


def test_formater_comparaison_affiche_les_deux_jours_cote_a_cote(engine_test):
    _construire_jeu_de_test(engine_test)

    m_jour = calculer_metriques(engine_test, JOUR)
    m_autre = calculer_metriques(engine_test, JOUR_AUTRE)

    texte = formater_comparaison(JOUR, m_jour, JOUR_AUTRE, m_autre)

    assert JOUR.isoformat() in texte
    assert JOUR_AUTRE.isoformat() in texte
    assert "Opportunités repérées" in texte
    # 4 opportunités le JOUR, 1 le JOUR_AUTRE -- les deux valeurs apparaissent
    # bien sur la même ligne, chacune dans sa colonne.
    ligne_reperees = next(l for l in texte.splitlines() if l.startswith("Opportunités repérées"))
    assert "4" in ligne_reperees
    assert "1" in ligne_reperees


def test_main_avec_comparer_ecrit_les_deux_jours_et_affiche_la_comparaison(tmp_path, monkeypatch, capsys):
    from sqlalchemy import create_engine

    from app.storage.db import migrer

    chemin_db = tmp_path / "metriques_cli_test.db"
    moteur = create_engine(f"sqlite:///{chemin_db}", future=True, connect_args={"check_same_thread": False})
    migrer(moteur)
    _construire_jeu_de_test(moteur)

    monkeypatch.setenv("RADAR_DATABASE_URL", f"sqlite:///{chemin_db}")

    import app.metriques as metriques_mod
    monkeypatch.setattr(metriques_mod, "DOSSIER_RAPPORTS", tmp_path / "rapports_test")

    code = main(["--jour", JOUR.isoformat(), "--comparer", JOUR_AUTRE.isoformat()])

    assert code == 0
    assert (tmp_path / "rapports_test" / f"{JOUR.isoformat()}.json").exists()
    assert (tmp_path / "rapports_test" / f"{JOUR_AUTRE.isoformat()}.json").exists()

    sortie = capsys.readouterr().out
    assert "Opportunités repérées" in sortie
    assert JOUR.isoformat() in sortie
    assert JOUR_AUTRE.isoformat() in sortie


def test_main_sans_radar_database_url_message_clair_sans_repli(tmp_path, monkeypatch, capsys):
    import app.metriques as metriques_mod

    monkeypatch.delenv("RADAR_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    # Chemin isolé : ce test ne doit jamais dépendre du vrai ~/.config de la
    # machine qui l'exécute (qui peut très bien contenir ce fichier une fois
    # la sous-étape 0.5 utilisée pour de vrai).
    monkeypatch.setattr(metriques_mod, "FICHIER_ENV_LECTURE_SEULE", tmp_path / "n-existe-pas" / "env")

    code = main(["--jour", JOUR.isoformat()])

    assert code == 1
    erreur = capsys.readouterr().err
    assert "RADAR_DATABASE_URL" in erreur


def test_main_ignore_database_url_meme_si_definie(tmp_path, monkeypatch, capsys):
    """DATABASE_URL (celle du reste de l'app, avec écriture) ne doit jamais
    servir de repli : sans RADAR_DATABASE_URL, la commande s'arrête même si
    DATABASE_URL, elle, est définie."""
    import app.metriques as metriques_mod

    monkeypatch.setenv("DATABASE_URL", "sqlite:///./ne-devrait-jamais-etre-ouvert.db")
    monkeypatch.delenv("RADAR_DATABASE_URL", raising=False)
    monkeypatch.setattr(metriques_mod, "FICHIER_ENV_LECTURE_SEULE", tmp_path / "n-existe-pas" / "env")

    code = main(["--jour", JOUR.isoformat()])

    assert code == 1
    assert "RADAR_DATABASE_URL" in capsys.readouterr().err


def test_main_utilise_le_fichier_env_si_variable_absente(tmp_path, monkeypatch, capsys):
    """Repli sur le fichier écrit par scripts/creer_acces_lecture.py (0.5)
    quand RADAR_DATABASE_URL n'est pas dans l'environnement."""
    from sqlalchemy import create_engine

    from app.storage.db import migrer

    chemin_db = tmp_path / "metriques_fichier_env.db"
    moteur = create_engine(f"sqlite:///{chemin_db}", future=True, connect_args={"check_same_thread": False})
    migrer(moteur)
    _construire_jeu_de_test(moteur)

    fichier_env = tmp_path / "config" / "radar-opportunites" / "env"
    fichier_env.parent.mkdir(parents=True)
    fichier_env.write_text(f"RADAR_DATABASE_URL=sqlite:///{chemin_db}\n", encoding="utf-8")

    import app.metriques as metriques_mod

    monkeypatch.delenv("RADAR_DATABASE_URL", raising=False)
    monkeypatch.setattr(metriques_mod, "FICHIER_ENV_LECTURE_SEULE", fichier_env)
    monkeypatch.setattr(metriques_mod, "DOSSIER_RAPPORTS", tmp_path / "rapports_test")

    code = main(["--jour", JOUR.isoformat()])

    assert code == 0
    assert '"opportunites_reperees": 4' in capsys.readouterr().out


def test_main_variable_environnement_prioritaire_sur_le_fichier(tmp_path, monkeypatch):
    """Si RADAR_DATABASE_URL est définie, le fichier n'est même pas lu."""
    import app.metriques as metriques_mod

    fichier_env = tmp_path / "env"
    fichier_env.write_text("RADAR_DATABASE_URL=sqlite:///ne-devrait-jamais-etre-ouvert.db\n", encoding="utf-8")

    monkeypatch.setenv("RADAR_DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setattr(metriques_mod, "FICHIER_ENV_LECTURE_SEULE", fichier_env)

    moteur = metriques_mod._engine_lecture_seule()
    assert "ne-devrait-jamais-etre-ouvert" not in str(moteur.url)


def test_main_comparer_date_invalide(capsys):
    code = main(["--jour", "2026-01-15", "--comparer", "pas-une-date"])
    assert code == 1
    assert "Format de date invalide" in capsys.readouterr().err
