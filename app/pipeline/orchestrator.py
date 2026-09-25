"""Orchestration déterministe du pipeline (§2, pipeline) :

Collecte -> normalisation -> déduplication -> Scout -> filtre de preuves ->
Analyst -> Critic -> calcul du score -> dossier.

Le programme orchestre les rôles ; ils ne s'accordent pas eux-mêmes de
nouveaux droits (§2). Toute décision de dépenser (appel modèle) passe par
`BudgetTracker`, qui peut arrêter un passage avant la fin — jamais après
coup.

Deux façons de faire tourner ce pipeline :
- `executer_run` : UN SEUL passage borné (tests, `--dry-run`, exécution
  manuelle). Crée son propre run, le termine à la fin.
- `executer_continu` : le worker Render (§ "jamais s'arrêter", demande de
  Mathéo le 25/09/2026) — un seul run par journée UTC, à l'intérieur duquel
  plusieurs passages s'enchaînent indéfiniment. Aucun dossier n'est jamais
  abandonné à mi-chemin : `_selectionner_pour_analyse` reprend TOUJOURS le
  retard des passages précédents (tout ce qui est encore au statut
  `nouveau`), jamais seulement ce que le passage courant vient de trouver.
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.engine import Engine

from app import config as cfg
from app.adapters.base import SignalBrut
from app.adapters.demo_adapter import AdaptateurDemo
from app.adapters.model_client import ModelClient
from app.adapters.rss_adapter import AdaptateurRSS
from app.models_schemas import DecisionCritic
from app.pipeline import dedupe
from app.pipeline.budget import BudgetDepasse, BudgetTracker
from app.pipeline.normalisation import inferer_secteur
from app.roles import analyst as role_analyst
from app.roles import critic as role_critic
from app.roles import scout as role_scout
from app.roles.critic import STATUT_PAR_DECISION
from app.scoring.engine import calculer_score
from app.storage import repo

logger = logging.getLogger(__name__)

VERSION_CODE = "radar-opportunites-v1"


class ArretPause(Exception):
    pass


@dataclass
class OptionsRun:
    mode: str  # "dry-run" | "reel"
    max_signaux: int | None = None
    max_analyses: int | None = None
    forcer_demo: bool = False


@dataclass
class ResumeRun:
    signaux_lus: int = 0
    signaux_deja_vus_ignores: int = 0
    opportunites_nouvelles: int = 0
    opportunites_fusionnees: int = 0
    suggestions_fusion_a_revoir: list[dict] = field(default_factory=list)
    analyses_terminees: int = 0
    critiques_terminees: int = 0
    erreurs: list[str] = field(default_factory=list)
    budget_atteint: bool = False
    temps_ecoule: bool = False
    sources_indisponibles: list[str] = field(default_factory=list)


def _pause_demandee(engine: Engine) -> bool:
    return cfg.get_settings().pause_all or repo.lire_pause_all(engine)


def _verifier_pause(engine: Engine) -> None:
    if _pause_demandee(engine):
        raise ArretPause("PAUSE_ALL est actif (variable d'environnement ou interface web).")


def _construire_adaptateurs(forcer_demo: bool) -> list:
    if forcer_demo:
        return [(AdaptateurDemo(), 999)]
    conf = cfg.sources_autorisees()
    adaptateurs: list = []
    for src in conf.get("rss", []):
        adaptateurs.append((AdaptateurRSS(src["id"], src["nom"], src["url"]), src.get("budget_appels_par_nuit", 5)))
    if not adaptateurs:
        for src in conf.get("demo", []):
            adaptateurs.append((AdaptateurDemo(), src.get("budget_appels_par_nuit", 20)))
    return adaptateurs


def _collecter(adaptateurs, max_signaux: int, resume: ResumeRun) -> list[SignalBrut]:
    bruts: list[SignalBrut] = []
    for adaptateur, budget_source in adaptateurs:
        if len(bruts) >= max_signaux:
            break
        restant = max_signaux - len(bruts)
        try:
            nouveaux = adaptateur.collecter(min(budget_source, restant))
            bruts.extend(nouveaux)
        except Exception as exc:  # une source en panne n'arrête pas la collecte des autres
            logger.warning("Source %s indisponible: %s", getattr(adaptateur, "id_source", adaptateur), exc)
            resume.sources_indisponibles.append(str(getattr(adaptateur, "id_source", adaptateur)))
    return bruts


def _selectionner_pour_analyse(engine: Engine, max_analyses: int, fraction_echantillon_rejetes: float) -> list[dict]:
    """Filtre de preuves : priorité au retard en attente (tout ce qui est
    encore `nouveau`, quel que soit le passage qui l'a créé — jamais
    seulement les opportunités du passage courant, sinon un dossier laissé
    de côté par manque de budget ne serait plus jamais repris), plus un
    petit échantillon de rejetés pour vérifier que le filtre n'est pas trop
    sévère (§2)."""
    toutes = repo.lister_opportunites_ouvertes(engine)
    backlog_nouveau = sorted(
        (o for o in toutes if o["statut"] == "nouveau"), key=lambda o: o["date_creation"]
    )
    deja_rejetees = [o for o in toutes if o["statut"] == "rejete"]

    n_echantillon = max(0, round(max_analyses * fraction_echantillon_rejetes)) if deja_rejetees else 0
    n_principal = max(0, max_analyses - n_echantillon)

    principal = backlog_nouveau[:n_principal]
    echantillon = random.sample(deja_rejetees, k=min(n_echantillon, len(deja_rejetees))) if deja_rejetees else []
    return principal + echantillon


def _phase_collecte_et_scout(
    engine: Engine, run_id: str, *, options: OptionsRun, quotas: dict, settings, model_client: ModelClient | None,
    resume: ResumeRun, debut: float, duree_max: float,
) -> None:
    max_signaux = min(options.max_signaux or quotas["max_signaux_par_passage"], quotas["max_signaux_par_passage"])
    adaptateurs = _construire_adaptateurs(options.forcer_demo)
    bruts = _collecter(adaptateurs, max_signaux, resume)

    opportunites_du_passage: list[dict] = []  # dédup en continu à l'intérieur de CE passage

    for brut in bruts:
        if time.monotonic() - debut > duree_max:
            resume.temps_ecoule = True
            break

        url_can = dedupe.canonicaliser_url(brut.url)
        empreinte = dedupe.empreinte_contenu(brut.texte)
        source_id, _ = repo.upsert_source(
            engine, url_canonique=url_can, domaine=brut.domaine,
            date_publication=brut.date_publication, type_source=brut.type_source,
            extrait=brut.texte, empreinte=empreinte, droits_collecte=brut.droits_collecte,
        )

        if repo.signal_deja_traite(engine, source_id):
            resume.signaux_deja_vus_ignores += 1
            continue

        secteur = inferer_secteur(brut.texte)
        if secteur in cfg.secteurs().get("exclure_secteur", []):
            continue
        if any(mot.lower() in brut.texte.lower() for mot in cfg.secteurs().get("mots_cles_negatifs", [])):
            continue

        repo.inserer_signal(
            engine, source_id=source_id, run_id=run_id, texte_court=brut.texte[:500],
            categorie=secteur, date_signal=brut.date_publication, normalisation={"secteur": secteur},
        )
        resume.signaux_lus += 1

        modele_tri = settings.model_tri
        try:
            scout_sortie, via_modele = role_scout.executer_scout(
                signal_id=source_id, texte=brut.texte, secteur=secteur,
                model_client=model_client, modele=modele_tri,
            )
        except BudgetDepasse as exc:
            resume.budget_atteint = True
            resume.erreurs.append(str(exc))
            break

        existantes_meme_secteur = [o for o in opportunites_du_passage if o["secteur"] == secteur]
        suggestion = dedupe.proposer_cluster(
            secteur=secteur, acheteur=scout_sortie.buyer, texte=scout_sortie.pain,
            existantes=existantes_meme_secteur,
        )

        if suggestion and suggestion.fusion_automatique:
            opportunity_id = suggestion.opportunity_id
            resume.opportunites_fusionnees += 1
        else:
            opportunity_id = repo.creer_opportunite(
                engine, titre=scout_sortie.opportunity_candidate, acheteur=scout_sortie.buyer,
                probleme=scout_sortie.pain, mecanisme_ia=scout_sortie.ai_mechanism,
                secteur=secteur, statut="nouveau", cluster_id=None,
            )
            opportunites_du_passage.append({
                "id": opportunity_id, "secteur": secteur, "acheteur": scout_sortie.buyer,
                "probleme": scout_sortie.pain, "missing_facts": scout_sortie.missing_facts,
            })
            resume.opportunites_nouvelles += 1
            if suggestion and not suggestion.fusion_automatique:
                resume.suggestions_fusion_a_revoir.append({
                    "nouvelle": opportunity_id, "existante_suggeree": suggestion.opportunity_id,
                    "similarite": round(suggestion.similarite, 2),
                })

        repo.inserer_assessment(
            engine, opportunity_id=opportunity_id, run_id=run_id, role="scout",
            payload=scout_sortie.model_dump(mode="json"), modele=modele_tri if via_modele else "heuristique",
            version_prompt=role_scout.VERSION_PROMPT, inconnues=scout_sortie.missing_facts,
        )
        repo.inserer_evidence(
            engine, opportunity_id=opportunity_id, source_id=source_id,
            claim=f"Scout: {scout_sortie.pain}", type_="hypothese", independant=True,
        )


def _phase_analyse_et_critique(
    engine: Engine, run_id: str, *, options: OptionsRun, quotas: dict, poids_config: dict, settings,
    model_client: ModelClient | None, resume: ResumeRun, debut: float, duree_max: float,
) -> None:
    max_analyses = min(options.max_analyses or quotas["max_analyses_par_passage"], quotas["max_analyses_par_passage"])
    a_analyser = _selectionner_pour_analyse(engine, max_analyses, quotas["echantillon_rejetes_pour_controle"])

    modele_approfondi = settings.model_approfondi
    for candidat in a_analyser:
        opportunity_id = candidat["id"]
        if time.monotonic() - debut > duree_max:
            resume.temps_ecoule = True
            break

        preuves = _charger_preuves(engine, opportunity_id)
        opportunite = _opportunite_par_id(engine, opportunity_id)

        try:
            analyst_sortie, _ = role_analyst.executer_analyst(
                opportunity_id=opportunity_id, opportunite=opportunite, preuves=preuves,
                model_client=model_client, modele=modele_approfondi,
            )
        except BudgetDepasse as exc:
            resume.budget_atteint = True
            resume.erreurs.append(str(exc))
            break

        repo.inserer_assessment(
            engine, opportunity_id=opportunity_id, run_id=run_id, role="analyst",
            payload=analyst_sortie.model_dump(mode="json"), modele=modele_approfondi,
            version_prompt=role_analyst.VERSION_PROMPT,
            inconnues=[i for c in analyst_sortie.criteres for i in c.inconnues],
        )
        empreintes_existantes = repo.empreintes_sources_citees(engine, opportunity_id)
        for critere in analyst_sortie.criteres:
            for affirmation in critere.affirmations:
                for source_id_cite in affirmation.source_ids:
                    empreinte_source = _empreinte_de_source(engine, source_id_cite)
                    independant = empreinte_source not in empreintes_existantes
                    empreintes_existantes.add(empreinte_source)
                    repo.inserer_evidence(
                        engine, opportunity_id=opportunity_id, source_id=source_id_cite,
                        claim=affirmation.texte, type_=affirmation.type.value, independant=independant,
                    )
        resume.analyses_terminees += 1
        repo.maj_statut_opportunite(engine, opportunity_id, "en_analyse")

        preuves = _charger_preuves(engine, opportunity_id)
        try:
            critic_sortie, _ = role_critic.executer_critic(
                opportunity_id=opportunity_id, opportunite=opportunite, analyst_sortie=analyst_sortie,
                preuves=preuves, model_client=model_client, modele=modele_approfondi,
            )
        except BudgetDepasse as exc:
            resume.budget_atteint = True
            resume.erreurs.append(str(exc))
            break

        repo.inserer_assessment(
            engine, opportunity_id=opportunity_id, run_id=run_id, role="critic",
            payload=critic_sortie.model_dump(mode="json"), modele=modele_approfondi,
            version_prompt=role_critic.VERSION_PROMPT, inconnues=critic_sortie.faits_contestes,
        )
        resume.critiques_terminees += 1

        resultat_score = calculer_score(analyst_sortie.criteres, poids_config)
        statut_final = STATUT_PAR_DECISION[critic_sortie.decision]
        if analyst_sortie.contradictions and critic_sortie.decision != DecisionCritic.REJETER:
            statut_final = "incertain"
        repo.maj_statut_opportunite(engine, opportunity_id, statut_final)

        repo.inserer_score(
            engine, opportunity_id=opportunity_id, run_id=run_id,
            version_poids=poids_config.get("version", "?"), valeurs=resultat_score.valeurs,
            score_brut=resultat_score.score_brut, score_prudent=resultat_score.score_prudent,
            couverture_preuves=resultat_score.couverture_preuves, flags=resultat_score.flags,
            decision_critic=critic_sortie.decision.value,
        )


def _resume_vers_dict(resume: ResumeRun) -> dict:
    return {
        "signaux_lus": resume.signaux_lus,
        "signaux_deja_vus_ignores": resume.signaux_deja_vus_ignores,
        "opportunites_nouvelles": resume.opportunites_nouvelles,
        "opportunites_fusionnees": resume.opportunites_fusionnees,
        "suggestions_fusion_a_revoir": resume.suggestions_fusion_a_revoir,
        "analyses_terminees": resume.analyses_terminees,
        "critiques_terminees": resume.critiques_terminees,
        "budget_atteint": resume.budget_atteint,
        "temps_ecoule": resume.temps_ecoule,
        "sources_indisponibles": resume.sources_indisponibles,
    }


def executer_run(engine: Engine, options: OptionsRun) -> tuple[str, ResumeRun]:
    """Un seul passage borné : tests, `--dry-run`, exécution manuelle."""
    _verifier_pause(engine)

    quotas = cfg.quotas()
    poids_config = cfg.poids_scoring()
    duree_max = quotas["duree_max_minutes"] * 60
    settings = cfg.get_settings()

    resume = ResumeRun()
    debut = time.monotonic()

    run_id = repo.creer_run(
        engine, mode=options.mode, version_code=VERSION_CODE,
        version_config=poids_config.get("version", "?"), quotas=quotas,
    )

    budget = BudgetTracker(engine, run_id, plafond_eur=quotas["budget_eur_par_jour"])
    model_client = None
    if options.mode != "dry-run" and settings.has_model_access:
        model_client = ModelClient(settings, budget)

    try:
        _phase_collecte_et_scout(
            engine, run_id, options=options, quotas=quotas, settings=settings, model_client=model_client,
            resume=resume, debut=debut, duree_max=duree_max,
        )

        if resume.budget_atteint or resume.temps_ecoule:
            _finaliser(engine, run_id, budget, resume, statut="interrompu")
            return run_id, resume

        _phase_analyse_et_critique(
            engine, run_id, options=options, quotas=quotas, poids_config=poids_config, settings=settings,
            model_client=model_client, resume=resume, debut=debut, duree_max=duree_max,
        )

        _finaliser(engine, run_id, budget, resume, statut="termine")
        return run_id, resume

    except ArretPause:
        raise
    except Exception as exc:  # filet de sécurité : le run se termine "echoue", jamais une exception qui remonte sans trace
        logger.exception("Run %s interrompu par une erreur inattendue", run_id)
        resume.erreurs.append(str(exc))
        _finaliser(engine, run_id, budget, resume, statut="echoue")
        raise


def executer_continu(engine: Engine, *, forcer_demo: bool = False) -> None:
    """Le worker (§ "jamais s'arrêter", 25/09/2026) : un run par journée UTC,
    des passages enchaînés indéfiniment à l'intérieur. Ne retourne jamais
    normalement (boucle infinie) — conçu pour être le process principal d'un
    Background Worker Render, qui redémarre le process si jamais il
    s'arrête. Toute exception d'UN passage est absorbée (journalisée, run
    marqué "echoue" si besoin) : elle n'arrête jamais la boucle elle-même,
    seulement `ArretPause` volontaire en tout début de tour."""
    settings = cfg.get_settings()
    quotas = cfg.quotas()
    poids_config = cfg.poids_scoring()
    duree_max_passage = quotas["duree_max_minutes"] * 60
    options = OptionsRun(mode="reel", forcer_demo=forcer_demo)

    run_id: str | None = None

    while True:
        if _pause_demandee(engine):
            logger.info("PAUSE_ALL actif : en attente, aucun passage tant que ce n'est pas levé.")
            time.sleep(60)
            continue

        aujourdhui = datetime.now(timezone.utc).date()
        run_existant = repo.run_en_cours_le_plus_recent(engine)
        if run_existant and run_existant["debut"].date() == aujourdhui:
            run_id = run_existant["id"]
        else:
            if run_existant:  # run d'une journée précédente jamais clôturé (redémarrage du worker)
                repo.terminer_run(
                    engine, run_existant["id"], statut="termine",
                    couts=run_existant.get("couts_json") or {}, erreurs=[],
                    resume=run_existant.get("resume_json") or {},
                )
            run_id = repo.creer_run(
                engine, mode="reel", version_code=VERSION_CODE,
                version_config=poids_config.get("version", "?"), quotas=quotas,
            )
            logger.info("Nouveau run journalier %s (%s UTC).", run_id, aujourdhui.isoformat())

        resume = ResumeRun()
        debut = time.monotonic()
        budget = BudgetTracker(engine, run_id, plafond_eur=quotas["budget_eur_par_jour"])
        model_client = ModelClient(settings, budget) if settings.has_model_access else None

        try:
            _phase_collecte_et_scout(
                engine, run_id, options=options, quotas=quotas, settings=settings, model_client=model_client,
                resume=resume, debut=debut, duree_max=duree_max_passage,
            )
            if not (resume.budget_atteint or resume.temps_ecoule):
                _phase_analyse_et_critique(
                    engine, run_id, options=options, quotas=quotas, poids_config=poids_config, settings=settings,
                    model_client=model_client, resume=resume, debut=debut, duree_max=duree_max_passage,
                )
        except ArretPause:
            continue
        except Exception as exc:  # un passage rate ne doit jamais arreter le worker
            logger.exception("Passage interrompu par une erreur inattendue (run %s)", run_id)
            resume.erreurs.append(str(exc))

        repo.mettre_a_jour_progression(
            engine, run_id, couts={"total_eur_estime": round(budget.cout_total_reel(), 4)},
            resume=_resume_vers_dict(resume),
        )

        if resume.budget_atteint:
            logger.info("Budget du jour atteint (run %s) : passage suivant demain.", run_id)
            repo.terminer_run(
                engine, run_id, statut="termine",
                couts={"total_eur_estime": round(budget.cout_total_reel(), 4)},
                erreurs=resume.erreurs, resume=_resume_vers_dict(resume),
            )
            # Attends le changement de jour UTC plutot que de boucler pour rien.
            while datetime.now(timezone.utc).date() == aujourdhui:
                if _pause_demandee(engine):
                    break
                time.sleep(300)
            continue

        if resume.signaux_lus == 0 and resume.analyses_terminees == 0:
            time.sleep(quotas["pause_apres_passage_vide_minutes"] * 60)
        else:
            time.sleep(quotas["pause_apres_passage_actif_secondes"])


def _charger_preuves(engine: Engine, opportunity_id: str) -> list[dict]:
    from sqlalchemy import select

    from app.storage.schema import opportunity_evidence, sources

    with engine.connect() as cx:
        rows = cx.execute(
            select(opportunity_evidence.c.source_id, sources.c.extrait, sources.c.url_canonique)
            .select_from(opportunity_evidence.join(sources, opportunity_evidence.c.source_id == sources.c.id))
            .where(opportunity_evidence.c.opportunity_id == opportunity_id)
        ).all()
    vus = {}
    for source_id, extrait, url in rows:
        vus[source_id] = {"source_id": source_id, "extrait": extrait, "url": url}
    return list(vus.values())


def _opportunite_par_id(engine: Engine, opportunity_id: str) -> dict:
    from sqlalchemy import select

    from app.storage.schema import opportunities

    with engine.connect() as cx:
        row = cx.execute(select(opportunities).where(opportunities.c.id == opportunity_id)).mappings().first()
        return dict(row)


def _empreinte_de_source(engine: Engine, source_id: str) -> str:
    from sqlalchemy import select

    from app.storage.schema import sources

    with engine.connect() as cx:
        row = cx.execute(select(sources.c.empreinte).where(sources.c.id == source_id)).first()
        return row[0] if row else source_id


def _finaliser(engine: Engine, run_id: str, budget: BudgetTracker, resume: ResumeRun, *, statut: str) -> None:
    repo.terminer_run(
        engine, run_id, statut=statut,
        couts={"total_eur_estime": round(budget.cout_total_reel(), 4)},
        erreurs=resume.erreurs,
        resume=_resume_vers_dict(resume),
    )
