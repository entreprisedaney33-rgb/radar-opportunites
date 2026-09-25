"""Tests de bout en bout du pipeline, en mode démo (aucun réseau, aucun
appel modèle), qui prouvent les propriétés exigées par le §7 (Phase 2) :
pas de doublon sur reprise, PAUSE_ALL arrête tout immédiatement."""
import pytest

from app.pipeline.orchestrator import ArretPause, OptionsRun, executer_run
from app.storage import repo


def test_run_demo_traite_tous_les_signaux_sans_appel_modele(engine_test):
    run_id, resume = executer_run(engine_test, OptionsRun(mode="dry-run", forcer_demo=True, max_signaux=6, max_analyses=2))

    run = repo.get_run(engine_test, run_id)
    assert run["statut"] == "termine"
    assert resume.signaux_lus == 6
    assert resume.opportunites_nouvelles + resume.opportunites_fusionnees == 6
    assert resume.analyses_terminees <= 2
    # Mode démo, sans clé modèle : aucun coût ne peut avoir été engagé.
    assert run["couts_json"]["total_eur_estime"] == 0.0


def test_reprise_ne_double_pas_les_dossiers(engine_test):
    """Relancer le pipeline sur la même base ne doit RIEN retraiter : les
    mêmes signaux de démo ont la même URL et la même empreinte de contenu à
    chaque appel, donc `signal_deja_traite` doit tous les faire ignorer."""
    executer_run(engine_test, OptionsRun(mode="dry-run", forcer_demo=True, max_signaux=6, max_analyses=2))
    _, resume2 = executer_run(engine_test, OptionsRun(mode="dry-run", forcer_demo=True, max_signaux=6, max_analyses=2))

    assert resume2.signaux_lus == 0
    assert resume2.signaux_deja_vus_ignores == 6
    assert resume2.opportunites_nouvelles == 0
    assert resume2.opportunites_fusionnees == 0


def test_budget_depasse_interrompt_le_run_proprement(engine_test, monkeypatch):
    """Un budget épuisé doit arrêter la collecte EN COURS, pas planter le
    run ni continuer à dépenser."""
    from app.pipeline.budget import BudgetDepasse

    class FauxModelClient:
        def __init__(self, *a, **kw):
            pass

        def appeler_structure(self, **kwargs):
            raise BudgetDepasse("plafond de test dépassé")

    monkeypatch.setattr("app.pipeline.orchestrator.ModelClient", FauxModelClient)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "cle-de-test")
    from app import config as cfg

    cfg.get_settings.cache_clear()
    try:
        run_id, resume = executer_run(
            engine_test, OptionsRun(mode="reel", forcer_demo=True, max_signaux=6, max_analyses=2),
        )
    finally:
        cfg.get_settings.cache_clear()

    assert resume.budget_atteint is True
    run = repo.get_run(engine_test, run_id)
    assert run["statut"] == "interrompu"
    # Le run s'est arrêté au tout premier signal : aucune opportunité créée.
    assert resume.opportunites_nouvelles == 0


def test_pause_all_bloque_immediatement(engine_test, monkeypatch):
    from app import config as cfg

    monkeypatch.setenv("RADAR_PAUSE_ALL", "1")
    cfg.get_settings.cache_clear()
    try:
        with pytest.raises(ArretPause):
            executer_run(engine_test, OptionsRun(mode="dry-run", forcer_demo=True))
    finally:
        cfg.get_settings.cache_clear()


def test_aucun_dossier_jamais_abandonne_pour_toujours(engine_test):
    """Le 25/09/2026, Mathéo a signalé des dossiers restés bloqués au statut
    `nouveau` pour toujours car le passage qui les avait créés avait déjà
    épuisé son quota d'analyses. Le retard doit être repris au passage
    suivant, jamais oublié."""
    run1, resume1 = executer_run(
        engine_test, OptionsRun(mode="dry-run", forcer_demo=True, max_signaux=6, max_analyses=1),
    )
    assert resume1.analyses_terminees == 1
    opportunites = repo.lister_opportunites_ouvertes(engine_test)
    en_attente_apres_1 = [o for o in opportunites if o["statut"] == "nouveau"]
    assert len(en_attente_apres_1) == 5  # 6 créées, 1 seule analysée ce passage-ci

    # Deuxième passage : aucun nouveau signal (tous déjà vus), mais le
    # retard de 5 doit être repris automatiquement.
    run2, resume2 = executer_run(
        engine_test, OptionsRun(mode="dry-run", forcer_demo=True, max_signaux=6, max_analyses=10),
    )
    assert resume2.signaux_lus == 0
    assert resume2.analyses_terminees == 5

    encore_en_attente = [o["statut"] for o in repo.lister_opportunites_ouvertes(engine_test) if o["statut"] == "nouveau"]
    assert encore_en_attente == []


def test_signal_offre_va_au_magasin_de_preuves_jamais_en_opportunite(engine_test, monkeypatch):
    """Sous-étape 1.1 : un item d'un flux `offre` (signal de concurrence)
    est tracé comme source mais ne crée jamais de signal ni d'opportunité."""
    from sqlalchemy import select

    from app.adapters.base import SignalBrut
    from app.pipeline import orchestrator as orch
    from app.sources import SourceConfig
    from app.storage.schema import signals, sources

    class FauxAdaptateurOffre:
        id_source = "faux_offre"

        def collecter(self, budget_appels, **_kw):
            return [SignalBrut(
                url="https://exemple.invalid/offre/1", domaine="faux",
                texte="Un concurrent lance un nouvel outil de facturation.",
                date_publication=None, type_source="rss", droits_collecte="test",
                flux_origine="ignoré (surchargé par la config)",
            )]

    config_offre = SourceConfig(
        id="faux_offre", nom="Faux flux offre", url="https://exemple.invalid", type="offre",
        secteur_par_defaut=None, langue="fr", actif=True, budget_appels_par_nuit=5,
    )
    monkeypatch.setattr(
        orch, "_construire_adaptateurs",
        lambda engine, forcer_demo, quotas: [(FauxAdaptateurOffre(), 5, config_offre)],
    )

    run_id, resume = executer_run(engine_test, OptionsRun(mode="dry-run", max_signaux=5))

    assert resume.signaux_lus == 0
    assert resume.signaux_concurrence_stockes == 1
    assert repo.lister_opportunites_ouvertes(engine_test) == []

    with engine_test.connect() as cx:
        lignes_sources = cx.execute(select(sources)).mappings().all()
        lignes_signals = cx.execute(select(signals)).all()
    assert len(lignes_sources) == 1
    assert lignes_sources[0]["etiquette"] == "signal_concurrence"
    assert lignes_sources[0]["flux_origine"] == "Faux flux offre"
    assert lignes_signals == []


def test_signal_douleur_porte_son_flux_origine(engine_test, monkeypatch):
    """Sous-étape 1.1 : un item d'un flux `douleur` continue d'alimenter le
    Scout normalement, et sa source garde le nom du flux qui l'a trouvé."""
    from sqlalchemy import select

    from app.adapters.base import SignalBrut
    from app.pipeline import orchestrator as orch
    from app.sources import SourceConfig
    from app.storage.schema import sources

    class FauxAdaptateurDouleur:
        id_source = "faux_douleur"

        def collecter(self, budget_appels, **_kw):
            return [SignalBrut(
                url="https://exemple.invalid/douleur/1", domaine="faux",
                texte="Je passe 6h par semaine à rapprocher des factures à la main.",
                date_publication=None, type_source="rss", droits_collecte="test",
                flux_origine="ignoré (surchargé par la config)",
            )]

    config_douleur = SourceConfig(
        id="faux_douleur", nom="Faux flux douleur", url="https://exemple.invalid", type="douleur",
        secteur_par_defaut="e_commerce", langue="fr", actif=True, budget_appels_par_nuit=5,
    )
    monkeypatch.setattr(
        orch, "_construire_adaptateurs",
        lambda engine, forcer_demo, quotas: [(FauxAdaptateurDouleur(), 5, config_douleur)],
    )

    run_id, resume = executer_run(engine_test, OptionsRun(mode="dry-run", max_signaux=5))

    assert resume.signaux_lus == 1
    assert resume.signaux_concurrence_stockes == 0
    assert len(repo.lister_opportunites_ouvertes(engine_test)) == 1

    with engine_test.connect() as cx:
        ligne_source = cx.execute(select(sources)).mappings().first()
    assert ligne_source["flux_origine"] == "Faux flux douleur"
    assert ligne_source["etiquette"] is None


def test_quota_offre_independant_n_affame_jamais_le_quota_douleur(engine_test, monkeypatch):
    """Sous-étape 1.2 (périmètre ajouté) : les flux `offre` ont leur propre
    quota de collecte, distinct de celui des flux `douleur`, qui ne peut
    jamais l'affamer. Le flux `offre` est placé EN PREMIER et peut fournir
    bien plus d'items que son propre quota : avant 1.2 (quota unique
    partagé), il aurait pu à lui seul épuiser tout `max_signaux` avant même
    que le flux `douleur` (placé après) ne soit essayé."""
    from app import config as cfg
    from app.adapters.base import SignalBrut
    from app.pipeline import orchestrator as orch
    from app.sources import SourceConfig

    class FauxAdaptateurOffre:
        id_source = "faux_offre"

        def collecter(self, budget_appels, **_kw):
            return [
                SignalBrut(
                    url=f"https://exemple.invalid/offre/{i}", domaine="faux",
                    texte="Un concurrent lance un outil.", date_publication=None,
                    type_source="rss", droits_collecte="test", flux_origine="ignoré",
                )
                for i in range(budget_appels)
            ]

    class FauxAdaptateurDouleur:
        id_source = "faux_douleur"

        def collecter(self, budget_appels, **_kw):
            return [
                SignalBrut(
                    url=f"https://exemple.invalid/douleur/{i}", domaine="faux",
                    texte=f"Douleur numéro {i} : des heures par semaine perdues à la main.",
                    date_publication=None, type_source="rss", droits_collecte="test",
                    flux_origine="ignoré",
                )
                for i in range(budget_appels)
            ]

    config_offre = SourceConfig(
        id="faux_offre", nom="Faux flux offre", url="https://exemple.invalid", type="offre",
        secteur_par_defaut=None, langue="fr", actif=True, budget_appels_par_nuit=20,
    )
    config_douleur = SourceConfig(
        id="faux_douleur", nom="Faux flux douleur", url="https://exemple.invalid", type="douleur",
        secteur_par_defaut="e_commerce", langue="fr", actif=True, budget_appels_par_nuit=20,
    )
    monkeypatch.setattr(
        orch, "_construire_adaptateurs",
        lambda engine, forcer_demo, quotas: [
            (FauxAdaptateurOffre(), 20, config_offre),
            (FauxAdaptateurDouleur(), 20, config_douleur),
        ],
    )
    quotas_reduits = dict(cfg.quotas())
    quotas_reduits["max_signaux_offre_par_passage"] = 3
    quotas_reduits["max_signaux_par_passage"] = 5
    monkeypatch.setattr(cfg, "quotas", lambda: quotas_reduits)

    run_id, resume = executer_run(engine_test, OptionsRun(mode="dry-run"))

    assert resume.signaux_concurrence_stockes == 3  # plafonné par max_signaux_offre_par_passage
    assert resume.signaux_lus == 5  # plafonné par max_signaux_par_passage, jamais réduit par l'offre


def test_construire_adaptateurs_recherche_reddit_persiste_et_tourne(engine_test):
    """Sous-étape 1.2, bout en bout avec la vraie base (aucun réseau ici :
    `_construire_adaptateurs_recherche_reddit` ne fait que choisir et marquer
    les combinaisons, jamais les interroger — voir `_collecter`) : les
    combinaisons choisies sont marquées visitées, et un deuxième appel
    immédiat (même passage) choisit d'AUTRES combinaisons (rotation), plus
    jamais les deux premières tout de suite."""
    from app import config as cfg
    from app.pipeline import orchestrator as orch

    quotas = dict(cfg.quotas())
    quotas["max_flux_recherche_par_passage"] = 2
    quotas["intervalle_heures_recherche_reddit"] = 6

    premier_lot = orch._construire_adaptateurs_recherche_reddit(engine_test, quotas)
    assert len(premier_lot) == 2
    ids_premier_lot = {adaptateur.id_source for adaptateur, _, _ in premier_lot}

    deuxieme_lot = orch._construire_adaptateurs_recherche_reddit(engine_test, quotas)
    assert len(deuxieme_lot) == 2
    ids_deuxieme_lot = {adaptateur.id_source for adaptateur, _, _ in deuxieme_lot}

    assert ids_premier_lot.isdisjoint(ids_deuxieme_lot)

    dernieres_visites = repo.lire_dernieres_visites_recherche(engine_test)
    assert (ids_premier_lot | ids_deuxieme_lot) <= set(dernieres_visites.keys())


def test_construire_adaptateurs_recherche_hn_persiste_et_tourne(engine_test):
    """Sous-étape 1.4 : même propriété que Reddit ci-dessus, sur le
    connecteur HN branché dans le même planificateur générique."""
    from app import config as cfg
    from app.pipeline import orchestrator as orch

    quotas = dict(cfg.quotas())
    quotas["max_flux_recherche_par_passage_hn"] = 2
    quotas["intervalle_heures_recherche_hn"] = 6

    premier_lot = orch._construire_adaptateurs_recherche_hn(engine_test, quotas)
    assert len(premier_lot) == 2
    ids_premier_lot = {adaptateur.id_source for adaptateur, _, _ in premier_lot}

    deuxieme_lot = orch._construire_adaptateurs_recherche_hn(engine_test, quotas)
    assert len(deuxieme_lot) == 2
    ids_deuxieme_lot = {adaptateur.id_source for adaptateur, _, _ in deuxieme_lot}

    assert ids_premier_lot.isdisjoint(ids_deuxieme_lot)
    assert all(id_.startswith("hn_recherche:") for id_ in ids_premier_lot | ids_deuxieme_lot)

    dernieres_visites = repo.lire_dernieres_visites_recherche(engine_test)
    assert (ids_premier_lot | ids_deuxieme_lot) <= set(dernieres_visites.keys())


def test_construire_adaptateurs_branche_reddit_et_hn_en_tete(engine_test):
    """Sous-étape 1.4 : `_construire_adaptateurs` doit combiner les DEUX
    connecteurs de recherche (pas seulement Reddit) avant les flux frontpage
    statiques, pour la même raison que 1.2 (voir la docstring de la
    fonction) : sinon les flux frontpage pourraient épuiser le quota douleur
    d'un passage avant que la recherche HN n'y goûte jamais."""
    from app import config as cfg
    from app.pipeline import orchestrator as orch

    quotas = dict(cfg.quotas())

    adaptateurs = orch._construire_adaptateurs(engine_test, forcer_demo=False, quotas=quotas)

    ids = [getattr(a, "id_source", None) for a, _, _ in adaptateurs]
    assert any(id_ and id_.startswith("reddit_recherche:") for id_ in ids)
    assert any(id_ and id_.startswith("hn_recherche:") for id_ in ids)


def test_resultat_hn_devient_un_signal_dans_le_pipeline(engine_test, monkeypatch):
    """Sous-étape 1.4 : preuve de bout en bout que le connecteur HN branché
    ci-dessus produit bien un signal réel dans le pipeline (pas seulement un
    adaptateur construit) — le VRAI connecteur (`AdaptateurRechercheHN`),
    avec son parseur réel, sur une réponse HTTP simulée (aucun réseau)."""
    from app.adapters import hn_recherche
    from app.pipeline import orchestrator as orch

    class FauxReponseHN:
        def json(self):
            return {
                "hits": [
                    {
                        "objectID": "111",
                        "title": "Ask HN: comment gérer la facturation ?",
                        "story_text": "Je passe des heures par semaine à rapprocher des factures à la main.",
                        "created_at": "2026-09-25T10:00:00Z",
                    }
                ],
                "nbHits": 1,
            }

    monkeypatch.setattr(hn_recherche, "get_with_retry", lambda url, **kw: FauxReponseHN())
    connecteur_hn_reel = hn_recherche.AdaptateurRechercheHN("ask_hn", "manually_en", "manually")
    monkeypatch.setattr(
        orch, "_construire_adaptateurs",
        lambda engine, forcer_demo, quotas: [(connecteur_hn_reel, 5, None)],
    )

    run_id, resume = executer_run(engine_test, OptionsRun(mode="dry-run", max_signaux=5))

    assert resume.signaux_lus == 1
    assert resume.signaux_concurrence_stockes == 0
    opportunites = repo.lister_opportunites_ouvertes(engine_test)
    assert len(opportunites) == 1

    from sqlalchemy import select

    from app.storage.schema import sources

    with engine_test.connect() as cx:
        ligne_source = cx.execute(select(sources)).mappings().first()
    assert ligne_source["requete_origine"] == "manually"
    assert "Hacker News" in ligne_source["flux_origine"]
    assert ligne_source["url_canonique"] == "https://news.ycombinator.com/item?id=111"

    requetes = repo.requetes_pour_source(engine_test, ligne_source["id"])
    assert len(requetes) == 1
    assert requetes[0]["requete_origine"] == "manually"


def test_tirage_controle_exclut_les_opportunites_deja_tirees(engine_test):
    """Sous-étape 0.7 : une opportunité rejetée déjà retirée une fois par
    l'échantillon de contrôle n'est plus jamais re-proposée."""
    from app.pipeline import orchestrator as orch

    opp_deja_tiree = repo.creer_opportunite(
        engine_test, titre="a", acheteur="x", probleme="p", mecanisme_ia="m",
        secteur="e_commerce", statut="rejete", cluster_id=None,
    )
    opp_jamais_tiree = repo.creer_opportunite(
        engine_test, titre="b", acheteur="x", probleme="p", mecanisme_ia="m",
        secteur="e_commerce", statut="rejete", cluster_id=None,
    )
    run_id = repo.creer_run(engine_test, mode="reel", version_code="t", version_config="t", quotas={})
    repo.inserer_tirage_controle_rejete(
        engine_test, opportunity_id=opp_deja_tiree, run_id=run_id,
        decision_avant="rejete", decision_apres="incertain",
    )

    selection = orch._selectionner_pour_analyse(engine_test, max_analyses=10, fraction_echantillon_rejetes=1.0)

    ids = {o["id"] for o in selection}
    assert opp_deja_tiree not in ids
    assert opp_jamais_tiree in ids
    tire = next(o for o in selection if o["id"] == opp_jamais_tiree)
    assert tire["tirage_controle"] is True
    assert tire["decision_avant"] == "rejete"


def test_executer_continu_ne_recree_pas_de_run_si_budget_du_jour_deja_atteint(engine_test, monkeypatch):
    """Sous-étape 0.7, point 2 : au (re)démarrage du worker, si la dépense du
    jour UTC est déjà au plafond, aucun nouveau run n'est créé — le worker
    entre directement dans la boucle d'attente du changement de jour."""
    from app.pipeline import orchestrator as orch
    from app import config as cfg

    quotas = cfg.quotas()
    run_deja_clos = repo.creer_run(engine_test, mode="reel", version_code="t", version_config="t", quotas={})
    repo.inserer_usage_event(
        engine_test, run_id=run_deja_clos, fournisseur="anthropic", modele_ou_actor="m", appels=1,
        tokens_in=1, tokens_out=1, cout=quotas["budget_eur_par_jour"], role="analyst",
    )
    repo.terminer_run(engine_test, run_deja_clos, statut="termine", couts={}, erreurs=[], resume={})

    class ArretTest(Exception):
        pass

    def faux_sleep(_secondes):
        raise ArretTest()

    monkeypatch.setattr(orch.time, "sleep", faux_sleep)

    with pytest.raises(ArretTest):
        orch.executer_continu(engine_test, forcer_demo=True)

    # Un seul run existe toujours en base : celui créé avant l'appel — le
    # worker n'en a pas créé de nouveau alors que le budget du jour est
    # déjà au plafond.
    with engine_test.connect() as cx:
        from sqlalchemy import select

        from app.storage.schema import runs

        lignes = cx.execute(select(runs.c.id)).all()
    assert [r[0] for r in lignes] == [run_deja_clos]


def test_executer_continu_cloture_le_run_encore_en_cours_si_budget_deja_atteint(engine_test, monkeypatch):
    """Sous-étape 3.7, point 3 : si le run du jour est encore `en_cours` (le
    worker a été redémarré sans que ce run n'ait été clôturé proprement) ET
    que le budget du jour est déjà atteint, ce chemin marque désormais ce run
    `termine` (`resume_json.budget_atteint = True`) au lieu de le laisser
    `en_cours` pendant toute l'attente de minuit UTC -- bug réel observé le
    25/09/2026 (voir §9 d'AMELIORATIONS.md, sous-étape 7.1)."""
    from app.pipeline import orchestrator as orch
    from app import config as cfg

    quotas = cfg.quotas()
    run_en_cours = repo.creer_run(engine_test, mode="reel", version_code="t", version_config="t", quotas={})
    repo.inserer_usage_event(
        engine_test, run_id=run_en_cours, fournisseur="anthropic", modele_ou_actor="m", appels=1,
        tokens_in=1, tokens_out=1, cout=quotas["budget_eur_par_jour"], role="analyst",
    )
    # `run_en_cours` reste "en_cours" -- jamais clôturé, simulant un
    # redémarrage du worker en plein passage.

    class ArretTest(Exception):
        pass

    def faux_sleep(_secondes):
        raise ArretTest()

    monkeypatch.setattr(orch.time, "sleep", faux_sleep)

    with pytest.raises(ArretTest):
        orch.executer_continu(engine_test, forcer_demo=True)

    run_relu = repo.get_run(engine_test, run_en_cours)
    assert run_relu["statut"] == "termine"
    assert run_relu["resume_json"]["budget_atteint"] is True

    # Aucun nouveau run créé : un seul existe toujours en base.
    with engine_test.connect() as cx:
        from sqlalchemy import select

        from app.storage.schema import runs

        lignes = cx.execute(select(runs.c.id)).all()
    assert [r[0] for r in lignes] == [run_en_cours]


def test_executer_continu_ne_laisse_rien_en_plan_et_boucle(engine_test, monkeypatch):
    """`executer_continu` ne retourne jamais normalement : on la fait
    s'arrêter en simulant une pause juste après le premier passage, et on
    vérifie que ce premier passage a bien traité tout ce qu'il a trouvé."""
    from app.pipeline import orchestrator as orch

    appels = {"n": 0}

    def fausse_pause(engine):
        appels["n"] += 1
        return appels["n"] > 1  # pas de pause au 1er tour, pause a partir du 2e

    monkeypatch.setattr(orch, "_pause_demandee", fausse_pause)

    class ArretTest(Exception):
        pass

    def faux_sleep(_secondes):
        raise ArretTest()

    monkeypatch.setattr(orch.time, "sleep", faux_sleep)

    with pytest.raises(ArretTest):
        orch.executer_continu(engine_test, forcer_demo=True)

    opportunites = repo.lister_opportunites_ouvertes(engine_test)
    assert len(opportunites) == 6  # les 6 signaux de démo, tous transformés en dossiers
    assert all(o["statut"] != "nouveau" for o in opportunites)  # aucun laissé en plan


def test_secteur_citation_verifiee_bout_en_bout_quand_le_scout_la_confirme(engine_test, monkeypatch):
    """Sous-étape 2.2, point 2 : la proposition du Scout (secteur + citation)
    est vérifiée par `inferer_secteur` (2.1) et, quand la citation est
    retrouvée telle quelle dans le texte, décide du secteur PERSISTÉ sur
    l'opportunité — jamais un secteur posé sans preuve textuelle (§3.7)."""
    import re

    from app import config as cfg
    from app.adapters.model_client import AccesModeleIndisponible
    from app.models_schemas import ScoutSortie
    from app.pipeline.orchestrator import OptionsRun, executer_run

    class FauxModelClientCitationVerifiee:
        def __init__(self, *a, **kw):
            pass

        def appeler_structure(self, **kwargs):
            if kwargs.get("role") != "scout":
                raise AccesModeleIndisponible("hors périmètre de ce test : seul le Scout répond")
            signal_id = re.search(r"id=([^)]+)\)", kwargs["prompt_utilisateur"]).group(1)
            return ScoutSortie(
                opportunity_candidate="Rapprochement bancaire automatisé",
                buyer="PME avec compta interne",
                pain="6h/semaine de rapprochement manuel",
                ai_mechanism="agent de rapprochement automatique",
                why_now="coût du temps humain",
                signal_ids=[signal_id],
                # Citation copiée mot pour mot du premier signal de démo
                # (app/adapters/demo_adapter.py) -- doit être retrouvée.
                secteur="flux_documentaires",
                secteur_citation="rapprocher manuellement emails et factures fournisseurs",
            )

    monkeypatch.setattr("app.pipeline.orchestrator.ModelClient", FauxModelClientCitationVerifiee)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "cle-de-test")
    cfg.get_settings.cache_clear()
    try:
        executer_run(engine_test, OptionsRun(mode="reel", forcer_demo=True, max_signaux=1, max_analyses=1))
    finally:
        cfg.get_settings.cache_clear()

    opportunites = repo.lister_opportunites_ouvertes(engine_test)
    assert len(opportunites) == 1
    opp = opportunites[0]
    assert opp["secteur"] == "flux_documentaires"  # secteur du Scout, pas celui du mot-clé du texte
    assert opp["secteur_provenance"] == "citation_verifiee"
    assert opp["secteur_citation"] == "rapprocher manuellement emails et factures fournisseurs"


def test_secteur_citation_inventee_ne_devient_jamais_le_secteur_persiste(engine_test, monkeypatch):
    """Une citation qui n'existe pas telle quelle dans le signal ne doit
    JAMAIS faire gagner le secteur proposé par le Scout (§3.1, §3.7) --
    retombe sur l'étage `defaut` (aucun `secteur_par_defaut` en mode démo)."""
    import re

    from app import config as cfg
    from app.adapters.model_client import AccesModeleIndisponible
    from app.models_schemas import ScoutSortie
    from app.pipeline.orchestrator import OptionsRun, executer_run

    class FauxModelClientCitationInventee:
        def __init__(self, *a, **kw):
            pass

        def appeler_structure(self, **kwargs):
            if kwargs.get("role") != "scout":
                raise AccesModeleIndisponible("hors périmètre de ce test : seul le Scout répond")
            signal_id = re.search(r"id=([^)]+)\)", kwargs["prompt_utilisateur"]).group(1)
            return ScoutSortie(
                opportunity_candidate="x", buyer="x", pain="x", ai_mechanism="x", why_now="x",
                signal_ids=[signal_id],
                secteur="agents_ia",  # secteur valide mais sans rapport avec le texte
                secteur_citation="une citation qui n'apparaît nulle part dans le signal",
            )

    monkeypatch.setattr("app.pipeline.orchestrator.ModelClient", FauxModelClientCitationInventee)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "cle-de-test")
    cfg.get_settings.cache_clear()
    try:
        executer_run(engine_test, OptionsRun(mode="reel", forcer_demo=True, max_signaux=1, max_analyses=1))
    finally:
        cfg.get_settings.cache_clear()

    opp = repo.lister_opportunites_ouvertes(engine_test)[0]
    assert opp["secteur"] != "agents_ia"
    assert opp["secteur_provenance"] == "defaut"
    assert opp["secteur_citation"] is None


def test_secteur_provenance_defaut_en_repli_heuristique_sans_modele(engine_test):
    """Sans modèle (mode démo par défaut, aucune clé), le repli heuristique
    ne propose ni secteur ni citation -- jamais `citation_verifiee`."""
    executer_run(engine_test, OptionsRun(mode="dry-run", forcer_demo=True, max_signaux=6, max_analyses=0))

    opportunites = repo.lister_opportunites_ouvertes(engine_test)
    assert len(opportunites) == 6
    assert all(o["secteur_provenance"] == "defaut" for o in opportunites)
    assert all(o["secteur_citation"] is None for o in opportunites)


# ------------------------------- sous-étape 3.4 : branchement de l'Enquêteur

def test_enquete_ne_bloque_jamais_un_dossier_meme_si_le_fournisseur_plante(engine_test, monkeypatch):
    """Sous-étape 3.4, point 3 : une panne inattendue de l'Enquêteur pour UNE
    opportunité ne doit jamais la laisser bloquée en attente d'enquête --
    elle passe quand même à `enquete_terminee` (donc reprise normalement par
    l'Analyst juste après), et le passage continue pour les autres."""
    from app.pipeline import orchestrator as orch

    def _enquete_qui_plante(*args, **kwargs):
        raise RuntimeError("panne simulée d'un fournisseur de l'Enquêteur")

    monkeypatch.setattr(orch, "enqueter_opportunite", _enquete_qui_plante)

    run_id, resume = executer_run(
        engine_test, OptionsRun(mode="dry-run", forcer_demo=True, max_signaux=6, max_analyses=6),
    )

    opportunites = repo.lister_opportunites_ouvertes(engine_test)
    assert len(opportunites) == 6
    # Aucun dossier laissé sur `nouveau` (jamais enquêté) ni sur
    # `enquete_terminee` (enquêté mais jamais analysé) : tous les 6 ont
    # atteint un statut final malgré la panne de l'Enquêteur sur chacun d'eux.
    assert all(o["statut"] not in ("nouveau", "enquete_terminee") for o in opportunites)
    assert resume.opportunites_enquetees == 6
    assert resume.sources_enquete_ajoutees == 0  # rien trouvé (panne), mais rien de bloqué non plus
    assert run_id  # le run se termine normalement, jamais "echoue"
    assert repo.get_run(engine_test, run_id)["statut"] == "termine"


def test_score_prudent_superieur_avec_plusieurs_sources_de_l_enqueteur(engine_test, monkeypatch):
    """Sous-étape 3.4, point 4 : test de bout en bout sur fixtures -- un
    signal -> N requêtes -> M sources -> dossier Analyst citant plusieurs
    sources -> score prudent supérieur à ce qu'il aurait été avec une seule
    source (celle du Scout). Deux exécutions complètes du pipeline, avec le
    même Scout et le même Analyst (déterministe, cite systématiquement TOUTES
    les preuves reçues, par paires réparties sur les critères) : seule la
    présence de fournisseurs actifs pour l'Enquêteur change entre les deux."""
    import re

    from sqlalchemy import create_engine

    from app import config as cfg
    from app.adapters.model_client import AccesModeleIndisponible
    from app.enqueteur import fetch as fetch_module
    from app.enqueteur.fournisseurs import DefinitionFournisseur, RegistreFournisseurs, ResultatRecherche
    from app.models_schemas import Affirmation, AnalystSortie, CritereAnalyst, NiveauPreuve, ScoutSortie, TypeAffirmation
    from app.pipeline import orchestrator as orch
    from app.roles.analyst import NOMS_CRITERES
    from app.storage.db import migrer

    def _extraire_source_ids_du_prompt(prompt_utilisateur: str) -> list[str]:
        return [m.group(1) for ligne in prompt_utilisateur.splitlines() if (m := re.match(r"^- ([^:]+):", ligne))]

    def _sortie_analyst_deterministe(opportunity_id: str, source_ids: list[str]) -> AnalystSortie:
        criteres = []
        i = 0
        for nom in NOMS_CRITERES:
            paire = source_ids[i:i + 2]
            i += 2
            criteres.append(CritereAnalyst(
                nom=nom,
                affirmations=[
                    Affirmation(texte=f"fait observé via {sid}", type=TypeAffirmation.OBSERVE, source_ids=[sid])
                    for sid in paire
                ],
            ))
        return AnalystSortie(
            opportunity_id=opportunity_id, criteres=criteres, prix_observes=[], marge_indicative=None,
            contradictions=[], prochain_test_moins_couteux="test", niveau_preuve_global=NiveauPreuve.MOYEN,
        )

    class FauxModelClient:
        def __init__(self, *a, **kw):
            pass

        def appeler_structure(self, **kwargs):
            role = kwargs.get("role")
            if role == "scout":
                signal_id = re.search(r"id=([^)]+)\)", kwargs["prompt_utilisateur"]).group(1)
                return ScoutSortie(
                    opportunity_candidate="Rapprochement bancaire automatisé", buyer="PME e-commerce",
                    pain="rapprochement bancaire manuel", ai_mechanism="agent de rapprochement",
                    why_now="coût du temps humain", signal_ids=[signal_id],
                )
            if role == "analyst":
                source_ids = _extraire_source_ids_du_prompt(kwargs["prompt_utilisateur"])
                return _sortie_analyst_deterministe(kwargs["opportunity_id"], source_ids)
            raise AccesModeleIndisponible("hors périmètre de ce test : seuls Scout et Analyst répondent")

    def _executer(*, registre_enqueteur: RegistreFournisseurs) -> float:
        moteur = create_engine("sqlite://", future=True, connect_args={"check_same_thread": False})
        migrer(moteur)
        monkeypatch.setattr(orch, "ModelClient", FauxModelClient)
        monkeypatch.setattr(orch, "construire_registre_fournisseurs_gratuits", lambda engine: registre_enqueteur)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "cle-de-test")
        cfg.get_settings.cache_clear()
        try:
            executer_run(moteur, OptionsRun(mode="reel", forcer_demo=True, max_signaux=1, max_analyses=1))
        finally:
            cfg.get_settings.cache_clear()
        opp = repo.lister_opportunites_ouvertes(moteur)[0]
        dernier_score = repo.historique_scores(moteur, opp["id"])[-1]
        return dernier_score["score_prudent"]

    # Sans aucun fournisseur actif : l'Enquêteur ne trouve rien, l'Analyst ne
    # dispose que de l'UNIQUE source d'origine (celle du Scout).
    score_une_seule_source = _executer(registre_enqueteur=RegistreFournisseurs())

    # Avec UN fournisseur simulé qui renvoie 5 résultats distincts : jusqu'à
    # 6 sources au total pour ce même dossier.
    resultats_simules = [
        ResultatRecherche(
            url=f"https://fournisseur-test-{i}.example/page", titre=f"Résultat {i}",
            extrait=f"Contenu distinct numéro {i} pour la preuve d'enquête.",
            horodatage_source=None, fournisseur="fournisseur_test",
        )
        for i in range(5)
    ]
    registre_avec_fournisseur = RegistreFournisseurs()
    registre_avec_fournisseur.enregistrer(
        DefinitionFournisseur(nom="fournisseur_test", fabrique=lambda: _FournisseurSimuleFixe(resultats_simules))
    )
    monkeypatch.setattr(fetch_module, "get_with_retry", lambda url, **kw: type("R", (), {"text": "", "status_code": 404})())
    monkeypatch.setattr(
        fetch_module, "get_avec_limite_taille",
        lambda url, **kw: f"<html><body><p>Page {url} avec du contenu suffisant et distinct.</p></body></html>".encode("utf-8"),
    )
    monkeypatch.setattr(fetch_module.time, "sleep", lambda *_a, **_kw: None)
    score_plusieurs_sources = _executer(registre_enqueteur=registre_avec_fournisseur)

    assert score_plusieurs_sources > score_une_seule_source


class _FournisseurSimuleFixe:
    """Renvoie toujours la MÊME liste de résultats, quelle que soit la
    requête -- utile ici pour garantir un nombre de sources déterministe
    indépendamment du nombre de requêtes générées par `app/enqueteur/gabarits.py`."""

    nom = "fournisseur_test"

    def __init__(self, resultats):
        self._resultats = resultats

    def rechercher(self, requete, limite):
        return self._resultats[:limite]
