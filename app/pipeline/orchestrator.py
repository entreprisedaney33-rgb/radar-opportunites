"""Orchestration déterministe du pipeline (§2, pipeline) :

Collecte -> normalisation -> déduplication -> Scout -> Enquêteur (sous-étape
3.4 d'AMELIORATIONS.md) -> filtre de preuves -> Analyst -> Critic -> calcul du
score -> dossier.

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
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from sqlalchemy.engine import Engine

from app import config as cfg
from app import lexique_douleur
from app import sources as config_sources
from app.adapters.base import SignalBrut
from app.adapters.demo_adapter import AdaptateurDemo
from app.adapters.hn_recherche import TAGS_VALIDES as HN_TAGS_VALIDES
from app.adapters.hn_recherche import AdaptateurRechercheHN
from app.adapters.model_client import ModelClient
from app.adapters.reddit_recherche import AdaptateurRechercheReddit
from app.adapters.rss_adapter import AdaptateurRSS
from app.enqueteur.enqueteur import enqueter_opportunite
from app.enqueteur.fournisseurs_gratuits import construire_registre_fournisseurs_gratuits
from app.enqueteur.gabarits import HypotheseEnqueteur
from app.models_schemas import DecisionCritic
from app.pipeline import dedupe
from app.pipeline.budget import BudgetDepasse, BudgetTracker
from app.pipeline.normalisation import inferer_secteur
from app.pipeline.planificateur_recherche import FluxRecherche, choisir_flux_a_visiter
from app.roles import analyst as role_analyst
from app.roles import critic as role_critic
from app.roles import scout as role_scout
from app.roles.critic import STATUT_PAR_DECISION
from app.scoring.engine import calculer_score
from app.sources import SourceConfig
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
    opportunites_enquetees: int = 0  # sous-étape 3.4 : passées par l'Enquêteur ce passage
    sources_enquete_ajoutees: int = 0  # nouvelles preuves rattachées par l'Enquêteur ce passage
    analyses_terminees: int = 0
    critiques_terminees: int = 0
    erreurs: list[str] = field(default_factory=list)
    budget_atteint: bool = False
    temps_ecoule: bool = False
    sources_indisponibles: list[str] = field(default_factory=list)
    signaux_concurrence_stockes: int = 0  # items d'un flux `offre`, jamais transformés en opportunité (1.1)


def _pause_demandee(engine: Engine) -> bool:
    return cfg.get_settings().pause_all or repo.lire_pause_all(engine)


def _verifier_pause(engine: Engine) -> None:
    if _pause_demandee(engine):
        raise ArretPause("PAUSE_ALL est actif (variable d'environnement ou interface web).")


def _construire_adaptateurs_recherche_reddit(
    engine: Engine, quotas: dict,
) -> list[tuple[object, int, SourceConfig | None]]:
    """Sous-étape 1.2 : sub × expression du lexique de douleur, en rotation
    (le produit dépasse 200 flux — voir `app/pipeline/planificateur_recherche.py`).
    Toujours `douleur` : les subreddits interrogés le sont déjà tous dans
    `app/sources.yaml` (`config_sources.subreddits_douleur`) — `config_source`
    vaut `None` ici, comme pour la démo, parce que l'adaptateur porte
    lui-même son `type_flux` (voir `app/adapters/reddit_recherche.py`)."""
    subs = config_sources.subreddits_douleur()
    expressions_douleur = lexique_douleur.expressions()
    if not subs or not expressions_douleur:
        return []

    tous_les_flux = [
        FluxRecherche(source="reddit", parametre=sub, expression_cle=expr.cle, expression_texte=expr.expression)
        for sub in subs
        for expr in expressions_douleur
    ]
    maintenant = datetime.now(timezone.utc)
    dernieres_visites = repo.lire_dernieres_visites_recherche(engine)
    choisis = choisir_flux_a_visiter(
        tous_les_flux, dernieres_visites, maintenant=maintenant,
        intervalle_heures=quotas["intervalle_heures_recherche_reddit"],
        max_par_passage=quotas["max_flux_recherche_par_passage"],
    )
    # Marqué visité au choix, pas après coup : ce créneau de rotation est
    # "consommé" pour ce passage même si le budget de signaux du passage
    # (max_signaux_par_passage) est déjà plein avant que _collecter()
    # n'atteigne ces adaptateurs (ils sont ajoutés en tête, voir
    # _construire_adaptateurs, mais un passage peut en contenir plusieurs).
    repo.marquer_flux_recherche_visites(engine, [f.id for f in choisis], maintenant)
    if choisis:
        logger.info(
            "Recherche Reddit : %d/%d combinaisons dues visitées ce passage (%s).",
            len(choisis), len(tous_les_flux), ", ".join(f.id for f in choisis),
        )

    budget_par_flux = quotas["budget_appels_recherche_reddit_par_flux"]
    return [
        (AdaptateurRechercheReddit(f.parametre, f.expression_cle, f.expression_texte), budget_par_flux, None)
        for f in choisis
    ]


def _construire_adaptateurs_recherche_hn(
    engine: Engine, quotas: dict,
) -> list[tuple[object, int, SourceConfig | None]]:
    """Sous-étape 1.4 : le connecteur de recherche Hacker News (sous-étape
    1.3, `app/adapters/hn_recherche.py`) branché dans le même planificateur
    générique que Reddit ci-dessus (question laissée ouverte en 1.3) — un
    flux par combinaison (tag, expression) : 2 tags (`comment`, `ask_hn`) ×
    25 expressions du lexique de douleur = 50 combinaisons, avec ses propres
    quotas de rotation (bien plus petit que Reddit, voir config/quotas.yaml).
    Toujours `douleur` : `config_source` vaut `None`, comme pour Reddit —
    l'adaptateur porte lui-même son `type_flux`."""
    expressions_douleur = lexique_douleur.expressions()
    if not expressions_douleur:
        return []

    tous_les_flux = [
        FluxRecherche(source="hn", parametre=tag, expression_cle=expr.cle, expression_texte=expr.expression)
        for tag in sorted(HN_TAGS_VALIDES)
        for expr in expressions_douleur
    ]
    maintenant = datetime.now(timezone.utc)
    dernieres_visites = repo.lire_dernieres_visites_recherche(engine)
    choisis = choisir_flux_a_visiter(
        tous_les_flux, dernieres_visites, maintenant=maintenant,
        intervalle_heures=quotas["intervalle_heures_recherche_hn"],
        max_par_passage=quotas["max_flux_recherche_par_passage_hn"],
    )
    repo.marquer_flux_recherche_visites(engine, [f.id for f in choisis], maintenant)
    if choisis:
        logger.info(
            "Recherche HN : %d/%d combinaisons dues visitées ce passage (%s).",
            len(choisis), len(tous_les_flux), ", ".join(f.id for f in choisis),
        )

    budget_par_flux = quotas["budget_appels_recherche_hn_par_flux"]
    return [
        (AdaptateurRechercheHN(f.parametre, f.expression_cle, f.expression_texte), budget_par_flux, None)
        for f in choisis
    ]


def _construire_adaptateurs(
    engine: Engine, forcer_demo: bool, quotas: dict,
) -> list[tuple[object, int, SourceConfig | None]]:
    """Renvoie (adaptateur, budget d'appels, config de la source). La config
    est `None` seulement pour le repli démo (sous-étape 1.1) et pour les
    connecteurs de recherche Reddit (sous-étape 1.2) et Hacker News
    (sous-étape 1.4, branché sur le connecteur créé en 1.3) — dans ces cas
    l'adaptateur porte lui-même son `type_flux` (toujours `douleur`).

    Les adaptateurs de recherche sont placés EN TÊTE de liste (avant les
    flux frontpage statiques) : sous-étape 1.2, priorité au but de ce plan
    (signaux de douleur ciblés par expression, but n°2 des constats
    d'AMELIORATIONS.md), pas aux flux frontpage déjà présents avant ce plan
    — sinon, avec `max_signaux_par_passage` partagé entre les deux, les
    flux frontpage (jusqu'à 130 signaux de budget cumulé) pourraient à eux
    seuls épuiser le quota douleur d'un passage avant que la recherche n'y
    goûte jamais. Signalé en §9 : si ça ne suffit pas en pratique (mesuré à
    la sous-étape 1.6), le quota lui-même devra être révisé, hors périmètre
    de cette sous-étape-ci."""
    if forcer_demo:
        return [(AdaptateurDemo(), 999, None)]
    adaptateurs: list[tuple[object, int, SourceConfig | None]] = []
    adaptateurs.extend(_construire_adaptateurs_recherche_reddit(engine, quotas))
    adaptateurs.extend(_construire_adaptateurs_recherche_hn(engine, quotas))
    actives = [src for src in config_sources.sources() if src.actif]
    adaptateurs.extend(
        (AdaptateurRSS(src.id, src.nom, src.url), src.budget_appels_par_nuit, src) for src in actives
    )
    if not adaptateurs:
        conf = cfg.sources_autorisees()
        for src in conf.get("demo", []):
            adaptateurs.append((AdaptateurDemo(), src.get("budget_appels_par_nuit", 20), None))
    return adaptateurs


def _collecter(
    engine: Engine,
    adaptateurs: list[tuple[object, int, SourceConfig | None]],
    max_signaux_douleur: int,
    max_signaux_offre: int,
    resume: ResumeRun,
) -> list[SignalBrut]:
    """Deux quotas INDÉPENDANTS (sous-étape 1.2) : un flux `offre` ne coûte
    aucun appel modèle (il est seulement stocké comme preuve de concurrence,
    jamais transformé en opportunité — voir `_phase_collecte_et_scout`) et ne
    doit donc jamais réduire la place disponible pour les flux `douleur`, qui
    eux alimentent le Scout. Chaque type de flux consomme son propre
    compteur ; un flux dont le quota de son type est déjà plein est ignoré
    pour la suite du passage, mais les flux de l'AUTRE type continuent
    d'être collectés normalement (la boucle ne s'arrête jamais entièrement
    tant qu'il reste de la place pour au moins un des deux types).

    `engine` (sous-étape 3.7) : transmis à chaque adaptateur pour qu'il
    journalise ses appels HTTP dans `journal_http`
    (`app/adapters/http.py`/`app/storage/repo.py::enregistrer_appel_http`)."""
    bruts_douleur: list[SignalBrut] = []
    bruts_offre: list[SignalBrut] = []
    for adaptateur, budget_source, config_source in adaptateurs:
        type_flux = config_source.type if config_source is not None else "douleur"
        bucket = bruts_douleur if type_flux == "douleur" else bruts_offre
        plafond = max_signaux_douleur if type_flux == "douleur" else max_signaux_offre
        restant = plafond - len(bucket)
        if restant <= 0:
            continue
        try:
            nouveaux = adaptateur.collecter(min(budget_source, restant), engine=engine)
        except Exception as exc:  # une source en panne n'arrête pas la collecte des autres
            logger.warning("Source %s indisponible: %s", getattr(adaptateur, "id_source", adaptateur), exc)
            resume.sources_indisponibles.append(str(getattr(adaptateur, "id_source", adaptateur)))
            continue
        if config_source is not None:
            # Le type douleur/offre et le secteur par défaut sont des
            # concepts de configuration (1.1, 2.1), pas quelque chose que
            # l'adaptateur RSS lui-même connaît.
            nouveaux = [
                replace(
                    s, flux_origine=config_source.nom, type_flux=config_source.type,
                    secteur_par_defaut=config_source.secteur_par_defaut,
                )
                for s in nouveaux
            ]
        bucket.extend(nouveaux)
    return bruts_douleur + bruts_offre


def _selectionner_pour_enquete(engine: Engine, max_enquetes: int) -> list[dict]:
    """Sous-étape 3.4, point 3 : priorité au retard -- toute opportunité
    encore `nouveau` (trouvée par le Scout, pas encore enquêtée), quel que
    soit le passage qui l'a créée -- même logique de reprise que
    `_selectionner_pour_analyse` ci-dessous (jamais seulement les
    opportunités du passage courant)."""
    toutes = repo.lister_opportunites_ouvertes(engine)
    return sorted((o for o in toutes if o["statut"] == "nouveau"), key=lambda o: o["date_creation"])[:max_enquetes]


def _selectionner_pour_analyse(engine: Engine, max_analyses: int, fraction_echantillon_rejetes: float) -> list[dict]:
    """Filtre de preuves : priorité au retard en attente (tout ce qui est
    encore `enquete_terminee` -- trouvé par le Scout ET déjà passé par
    l'Enquêteur, sous-étape 3.4 -- quel que soit le passage qui l'a créé —
    jamais seulement les opportunités du passage courant, sinon un dossier
    laissé de côté par manque de budget ne serait plus jamais repris), plus un
    petit échantillon de rejetés pour vérifier que le filtre n'est pas trop
    sévère (§2).

    Depuis la sous-étape 0.7 : une opportunité déjà retirée une fois par cet
    échantillon (table `tirages_controle_rejetes`) n'est plus jamais
    re-proposée — voir `rapports/DIAGNOSTIC_BUDGET_2026-09-25.md`, §4."""
    toutes = repo.lister_opportunites_ouvertes(engine)
    backlog_nouveau = sorted(
        (o for o in toutes if o["statut"] == "enquete_terminee"), key=lambda o: o["date_creation"]
    )
    deja_tirees = repo.opportunites_deja_tirees_controle(engine)
    deja_rejetees = [o for o in toutes if o["statut"] == "rejete" and o["id"] not in deja_tirees]

    n_echantillon = max(0, round(max_analyses * fraction_echantillon_rejetes)) if deja_rejetees else 0
    n_principal = max(0, max_analyses - n_echantillon)

    principal = backlog_nouveau[:n_principal]
    echantillon = random.sample(deja_rejetees, k=min(n_echantillon, len(deja_rejetees))) if deja_rejetees else []
    for o in echantillon:
        o["tirage_controle"] = True
        o["decision_avant"] = o["statut"]  # toujours "rejete" ici (critère de sélection), gardé explicite
    return principal + echantillon


def _phase_collecte_et_scout(
    engine: Engine, run_id: str, *, options: OptionsRun, quotas: dict, settings, model_client: ModelClient | None,
    resume: ResumeRun, debut: float, duree_max: float,
) -> None:
    max_signaux_douleur = min(
        options.max_signaux or quotas["max_signaux_par_passage"], quotas["max_signaux_par_passage"]
    )
    max_signaux_offre = quotas["max_signaux_offre_par_passage"]
    adaptateurs = _construire_adaptateurs(engine, options.forcer_demo, quotas)
    bruts = _collecter(engine, adaptateurs, max_signaux_douleur, max_signaux_offre, resume)

    opportunites_du_passage: list[dict] = []  # dédup en continu à l'intérieur de CE passage

    for brut in bruts:
        if time.monotonic() - debut > duree_max:
            resume.temps_ecoule = True
            break

        url_can = dedupe.canonicaliser_url(brut.url)
        empreinte = dedupe.empreinte_contenu(brut.texte)

        if brut.type_flux == "offre":
            # Signal de concurrence (sous-étape 1.1) : tracé comme preuve
            # (mêmes champs que n'importe quelle source), mais jamais
            # transformé en opportunité — réutilisé par l'Analyst à partir
            # de l'étape 3 (magasin interne de l'Enquêteur).
            repo.upsert_source(
                engine, url_canonique=url_can, domaine=brut.domaine,
                date_publication=brut.date_publication, type_source=brut.type_source,
                extrait=brut.texte, empreinte=empreinte, droits_collecte=brut.droits_collecte,
                flux_origine=brut.flux_origine, requete_origine=brut.requete_origine,
                etiquette="signal_concurrence",
            )
            resume.signaux_concurrence_stockes += 1
            continue

        source_id, _ = repo.upsert_source(
            engine, url_canonique=url_can, domaine=brut.domaine,
            date_publication=brut.date_publication, type_source=brut.type_source,
            extrait=brut.texte, empreinte=empreinte, droits_collecte=brut.droits_collecte,
            flux_origine=brut.flux_origine, requete_origine=brut.requete_origine,
        )

        if repo.signal_deja_traite(engine, source_id):
            resume.signaux_deja_vus_ignores += 1
            continue

        # Avant l'appel au Scout, seuls les étages « flux » et « defaut »
        # peuvent s'appliquer (la proposition du Scout n'existe pas encore) :
        # ce secteur sert au signal, aux filtres ci-dessous, à l'indice donné
        # au Scout, et au dédoublonnage intra-passage plus bas — inchangé par
        # la sous-étape 2.2, hors périmètre écrit de 2.2.
        resultat_secteur = inferer_secteur(brut.texte, secteur_defaut_flux=brut.secteur_par_defaut)
        secteur = resultat_secteur.secteur
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

        # Sous-étape 2.2 : la proposition du Scout (secteur + citation) est
        # maintenant disponible -- ré-évaluée ici pour décider du secteur
        # PERSISTÉ sur l'opportunité (étage « citation_verifiee » possible).
        # Le secteur `secteur` utilisé au-dessus (signal, filtres, indice
        # donné au Scout) et ci-dessous (dédoublonnage intra-passage) reste
        # volontairement celui d'avant le Scout : 2.2 ne touche pas au
        # dédoublonnage, hors périmètre écrit de cette sous-étape.
        resultat_secteur_final = inferer_secteur(
            brut.texte, secteur_defaut_flux=brut.secteur_par_defaut,
            secteur_propose=scout_sortie.secteur, citation_propose=scout_sortie.secteur_citation,
        )

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
                secteur=resultat_secteur_final.secteur, statut="nouveau", cluster_id=None,
                secteur_provenance=resultat_secteur_final.provenance, secteur_citation=resultat_secteur_final.citation,
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


def _phase_enquete(
    engine: Engine, *, options: OptionsRun, quotas: dict, budget: BudgetTracker, resume: ResumeRun,
    debut: float, duree_max: float,
) -> None:
    """Sous-étape 3.4 : Scout -> Enquêteur -> Analyst -> Critic. Chaque
    opportunité `nouveau` (retard repris en priorité, voir
    `_selectionner_pour_enquete`) est enquêtée puis marquée
    `enquete_terminee` -- TOUJOURS, même sans nouvelle source trouvée ou en
    cas d'erreur inattendue d'un fournisseur : jamais bloquée en attente
    d'enquête (point 3 du texte de 3.4). `enqueter_opportunite` ne lève
    jamais `BudgetDepasse` (chaque appel est protégé individuellement) ; le
    filet `except Exception` ci-dessous ne couvre qu'une panne totalement
    inattendue, même esprit que `_collecter` pour une source en panne."""
    max_enquetes = min(options.max_analyses or quotas["max_analyses_par_passage"], quotas["max_analyses_par_passage"])
    a_enqueter = _selectionner_pour_enquete(engine, max_enquetes)
    if not a_enqueter:
        return

    registre = construire_registre_fournisseurs_gratuits(engine)
    for opportunite in a_enqueter:
        if time.monotonic() - debut > duree_max:
            resume.temps_ecoule = True
            break

        hypothese = HypotheseEnqueteur(
            acheteur=opportunite["acheteur"], douleur=opportunite["probleme"], mecanisme=opportunite["mecanisme_ia"],
        )
        try:
            nouvelles_sources = enqueter_opportunite(
                engine, opportunite["id"], hypothese, registre=registre, budget=budget, quotas=quotas,
            )
        except Exception as exc:
            logger.warning("Enquête de l'opportunité %s interrompue par une erreur inattendue : %s", opportunite["id"], exc)
            nouvelles_sources = []

        resume.opportunites_enquetees += 1
        resume.sources_enquete_ajoutees += len(nouvelles_sources)
        repo.maj_statut_opportunite(engine, opportunite["id"], "enquete_terminee")


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

        if candidat.get("tirage_controle"):
            repo.inserer_tirage_controle_rejete(
                engine, opportunity_id=opportunity_id, run_id=run_id,
                decision_avant=candidat["decision_avant"], decision_apres=statut_final,
            )

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
        "opportunites_enquetees": resume.opportunites_enquetees,
        "sources_enquete_ajoutees": resume.sources_enquete_ajoutees,
        "analyses_terminees": resume.analyses_terminees,
        "critiques_terminees": resume.critiques_terminees,
        "budget_atteint": resume.budget_atteint,
        "temps_ecoule": resume.temps_ecoule,
        "sources_indisponibles": resume.sources_indisponibles,
        "signaux_concurrence_stockes": resume.signaux_concurrence_stockes,
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

    budget = BudgetTracker(
        engine, run_id,
        plafond_eur=quotas["budget_eur_par_jour"],
        plafond_appels_approfondis=quotas["max_appels_approfondis_par_jour"],
        plafond_requetes_recherche_par_jour=quotas["max_requetes_recherche_par_jour"],
        plafond_fetchs_pages_par_jour=quotas["max_fetchs_pages_par_jour"],
    )
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

        _phase_enquete(
            engine, options=options, quotas=quotas, budget=budget, resume=resume, debut=debut, duree_max=duree_max,
        )

        if resume.temps_ecoule:
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
        plafond_jour = quotas["budget_eur_par_jour"]
        depense_jour = repo.cout_total_jour_utc(engine, aujourdhui)
        if depense_jour >= plafond_jour:
            # Sous-étape 0.7 : vérifié AVANT toute décision de run, y compris
            # au redémarrage du worker — sinon un `run_id` différent (le
            # précédent étant `termine`) repartirait avec un compteur à zéro
            # alors que la dépense du jour est déjà au plafond (voir
            # rapports/DIAGNOSTIC_BUDGET_2026-09-25.md).
            #
            # Sous-étape 3.7, point 3 : si un run du jour est encore
            # `en_cours` (le worker a été redémarré sans que ce run n'ait été
            # clôturé), ce chemin le marque désormais `termine`
            # (`resume_json.budget_atteint = True`) au lieu de le laisser
            # `en_cours` pendant toute l'attente de minuit UTC -- avant cette
            # correction, seul le chemin symétrique plus bas (budget atteint
            # EN COURS d'un passage) clôturait le run ; celui-ci, lui, ne
            # touchait jamais son statut. Bug réel observé le 25/09/2026 (run
            # resté `en_cours`, coût du jour à 35,37 € au-dessus du plafond,
            # voir §9 d'AMELIORATIONS.md, sous-étape 7.1).
            run_existant = repo.run_en_cours_le_plus_recent(engine)
            if run_existant:
                repo.terminer_run(
                    engine, run_existant["id"], statut="termine",
                    couts=run_existant.get("couts_json") or {},
                    erreurs=run_existant.get("erreurs_json") or [],
                    resume={**(run_existant.get("resume_json") or {}), "budget_atteint": True},
                )
            logger.info(
                "Budget du jour atteint : %.2f € / %.2f €, reprise à minuit UTC.",
                depense_jour, plafond_jour,
            )
            while datetime.now(timezone.utc).date() == aujourdhui:
                if _pause_demandee(engine):
                    break
                time.sleep(300)
            continue

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
        budget = BudgetTracker(
            engine, run_id,
            plafond_eur=plafond_jour,
            plafond_appels_approfondis=quotas["max_appels_approfondis_par_jour"],
            plafond_requetes_recherche_par_jour=quotas["max_requetes_recherche_par_jour"],
            plafond_fetchs_pages_par_jour=quotas["max_fetchs_pages_par_jour"],
        )
        model_client = ModelClient(settings, budget) if settings.has_model_access else None

        try:
            _phase_collecte_et_scout(
                engine, run_id, options=options, quotas=quotas, settings=settings, model_client=model_client,
                resume=resume, debut=debut, duree_max=duree_max_passage,
            )
            if not (resume.budget_atteint or resume.temps_ecoule):
                _phase_enquete(
                    engine, options=options, quotas=quotas, budget=budget, resume=resume,
                    debut=debut, duree_max=duree_max_passage,
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
            logger.info(
                "Budget du jour atteint : %.2f € / %.2f €, reprise à minuit UTC.",
                budget.depense_jour_engagee(), plafond_jour,
            )
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
