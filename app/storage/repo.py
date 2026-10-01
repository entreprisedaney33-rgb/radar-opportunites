"""Accès aux données : chaque fonction est une unité de travail (transaction
courte). Rien n'écrase silencieusement : `scores` est append-only, `runs`
n'est mis à jour que sur son propre id (idempotence d'une reprise : voir
`get_run` avant de recréer).
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, inspect, insert, select, update
from sqlalchemy.engine import Engine

from sqlalchemy.dialects.sqlite import insert as sqlite_upsert
from sqlalchemy.dialects.postgresql import insert as postgres_upsert

from app.storage.schema import (
    concurrence_secteur_tache,
    recherches_web,
    assessments,
    faisabilites,
    controles,
    decisions,
    demande_secteur_tache,
    etats_disjoncteur_api,
    etats_disjoncteur_enqueteur,
    etablissements_secteur,
    collectes_offres,
    etats_flux_recherche,
    fiches_secteur_tache,
    journal_http,
    opportunities,
    offres_emploi,
    offres_etiquetage,
    offres_taches,
    opportunity_evidence,
    prospection,
    runs,
    scores,
    signals,
    source_requetes,
    sources,
    tirages_controle_rejetes,
    usage_events,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uid() -> str:
    return uuid.uuid4().hex


def _bornes_jour_utc(jour: date) -> tuple[datetime, datetime]:
    debut = datetime(jour.year, jour.month, jour.day, tzinfo=timezone.utc)
    return debut, debut + timedelta(days=1)


# ---------------------------------------------------------------- runs ----

def creer_run(engine: Engine, *, mode: str, version_code: str, version_config: str, quotas: dict) -> str:
    run_id = _uid()
    with engine.begin() as cx:
        cx.execute(
            insert(runs).values(
                id=run_id,
                mode=mode,
                debut=_now(),
                fin=None,
                statut="en_cours",
                version_code=version_code,
                version_config=version_config,
                quotas_json=quotas,
                couts_json={},
                erreurs_json=[],
                resume_json=None,
            )
        )
    return run_id


def terminer_run(engine: Engine, run_id: str, *, statut: str, couts: dict, erreurs: list, resume: dict) -> None:
    with engine.begin() as cx:
        cx.execute(
            update(runs)
            .where(runs.c.id == run_id)
            .values(fin=_now(), statut=statut, couts_json=couts, erreurs_json=erreurs, resume_json=resume)
        )


def get_run(engine: Engine, run_id: str) -> dict | None:
    with engine.connect() as cx:
        row = cx.execute(select(runs).where(runs.c.id == run_id)).mappings().first()
        return dict(row) if row else None


def run_en_cours_le_plus_recent(engine: Engine) -> dict | None:
    """Le worker continu reprend ce run s'il existe déjà (redémarrage du
    process) au lieu d'en recréer un — jamais deux runs 'en_cours' en même
    temps pour la même journée.

    Sous-étape 3.13 : "api_en_erreur" (voir `marquer_disjoncteur_sur_run`
    ci-dessous) est un état du run EN COURS, pas une fin de run -- inclus ici
    au même titre que "en_cours", sinon le worker recréerait par erreur un
    second run pour la même journée dès qu'un incident du disjoncteur API est
    en cours."""
    with engine.connect() as cx:
        row = cx.execute(
            select(runs)
            .where(runs.c.statut.in_(("en_cours", "api_en_erreur")))
            .order_by(runs.c.debut.desc()).limit(1)
        ).mappings().first()
        return dict(row) if row else None


def mettre_a_jour_progression(engine: Engine, run_id: str, *, couts: dict, resume: dict) -> None:
    """Met à jour un run TOUJOURS 'en_cours' (jamais `fin`/`statut`) pour que
    le tableau de bord reflète la progression pendant qu'un passage tourne —
    distinct de `terminer_run`, qui clôt le run pour de bon."""
    with engine.begin() as cx:
        cx.execute(
            update(runs).where(runs.c.id == run_id).values(couts_json=couts, resume_json=resume)
        )


def marquer_disjoncteur_sur_run(
    engine: Engine, run_id: str, *, en_erreur: bool, depuis: datetime | None, message: str | None,
    messages_permanents: list[str] | None = None,
) -> None:
    """Sous-étape 3.13, point 2 : rend l'état du disjoncteur de l'appel au
    modèle visible sur le run EN COURS (`runs.statut`/`erreurs_json`), pour
    que le workflow Jarvis (`jarvis-radar-recap`) puisse l'afficher sans
    avoir à connaître `etats_disjoncteur_api` -- jamais appelée sur un run
    déjà terminé. `statut` passe à "api_en_erreur" (toujours traité comme
    "en cours" par `run_en_cours_le_plus_recent` ci-dessus) le temps de
    l'incident, revient à "en_cours" une fois résolu -- ne touche jamais
    `fin`. `erreurs_json` (sinon toujours `[]` tant qu'un run est en cours --
    seul `terminer_run` l'alimente normalement, à la fin) porte l'unique
    message décrivant l'incident courant, remplacé à chaque appel plutôt
    qu'accumulé (ce n'est pas un journal, c'est un état présent).

    `messages_permanents` (sous-étape 3.17) : lignes ajoutées APRÈS le message
    d'incident (jamais devant : Jarvis relit le premier), qui doivent survivre
    à ce remplacement -- le résumé de la reprise au démarrage du worker."""
    statut = "api_en_erreur" if en_erreur else "en_cours"
    erreurs = [f"Disjoncteur API en erreur depuis {depuis.isoformat() if depuis else '?'} : {message}"] if en_erreur else []
    erreurs = erreurs + list(messages_permanents or [])
    with engine.begin() as cx:
        cx.execute(update(runs).where(runs.c.id == run_id).values(statut=statut, erreurs_json=erreurs))


# ------------------------------------------------------------- sources ----

def upsert_source(
    engine: Engine,
    *,
    url_canonique: str,
    domaine: str,
    date_publication: datetime | None,
    type_source: str,
    extrait: str,
    empreinte: str,
    droits_collecte: str,
    flux_origine: str | None = None,
    requete_origine: str | None = None,
    etiquette: str | None = None,
) -> tuple[str, bool]:
    """Idempotence : la même URL canonique + la même empreinte de contenu ne
    créent jamais deux lignes (contrainte unique + relecture ici). Renvoie
    (id, cree). `flux_origine`/`requete_origine`/`etiquette` (sous-étape 1.1)
    ne sont renseignés qu'à la création — une source déjà vue garde ses
    valeurs d'origine, jamais réécrites.

    Sous-étape 1.4 : si `requete_origine` est fourni (connecteurs de
    recherche Reddit/HN), la requête est en plus tracée dans
    `source_requetes` — que la source soit neuve ou déjà vue. Un même post
    retrouvé par 3 requêtes différentes ne crée toujours qu'UNE SEULE source
    (et donc jamais plus d'une opportunité), mais les 3 requêtes qui l'ont
    retrouvé restent toutes visibles."""
    with engine.begin() as cx:
        existante = cx.execute(
            select(sources.c.id).where(
                sources.c.url_canonique == url_canonique,
                sources.c.empreinte == empreinte,
            )
        ).first()
        if existante:
            source_id, cree = existante[0], False
        else:
            source_id = _uid()
            cx.execute(
                insert(sources).values(
                    id=source_id,
                    url_canonique=url_canonique,
                    domaine=domaine,
                    date_publication=date_publication,
                    date_collecte=_now(),
                    type=type_source,
                    extrait=extrait,
                    empreinte=empreinte,
                    droits_collecte=droits_collecte,
                    flux_origine=flux_origine,
                    requete_origine=requete_origine,
                    etiquette=etiquette,
                )
            )
            cree = True

        if requete_origine is not None:
            upsert = sqlite_upsert if engine.dialect.name == "sqlite" else postgres_upsert
            stmt = upsert(source_requetes).values(
                id=_uid(), source_id=source_id, flux_origine=flux_origine,
                requete_origine=requete_origine, date_creation=_now(),
            )
            stmt = stmt.on_conflict_do_nothing(index_elements=["source_id", "flux_origine", "requete_origine"])
            cx.execute(stmt)

        return source_id, cree


def requetes_pour_source(engine: Engine, source_id: str) -> list[dict]:
    """Sous-étape 1.4 : toutes les requêtes de recherche distinctes qui ont
    retrouvé cette source (voir `upsert_source` ci-dessus). Observabilité et
    tests — le pipeline lui-même n'en a pas besoin pour fonctionner."""
    with engine.connect() as cx:
        rows = cx.execute(
            select(source_requetes).where(source_requetes.c.source_id == source_id)
        ).mappings().all()
        return [dict(r) for r in rows]


def lister_signaux_concurrence(engine: Engine) -> list[dict]:
    """Sous-étape 3.2 : tous les items du magasin de preuves étiquetés
    `signal_concurrence` (flux `offre`, stockés depuis la sous-étape 1.1,
    jamais transformés en opportunité — voir
    `app.pipeline.orchestrator._phase_collecte_et_scout`). Sert de bassin de
    candidats, gratuit et sans réseau, au fournisseur « magasin interne » de
    l'Enquêteur (`app.enqueteur.fournisseurs_gratuits.FournisseurMagasinInterne`)."""
    with engine.connect() as cx:
        rows = cx.execute(
            select(sources).where(sources.c.etiquette == "signal_concurrence")
        ).mappings().all()
        return [dict(r) for r in rows]


# ------------------------------------------------------------- signals ---

def inserer_signal(engine: Engine, *, source_id: str, run_id: str, texte_court: str, categorie: str,
                    date_signal: datetime | None, normalisation: dict) -> str:
    signal_id = _uid()
    with engine.begin() as cx:
        cx.execute(
            insert(signals).values(
                id=signal_id,
                source_id=source_id,
                run_id=run_id,
                texte_court=texte_court,
                categorie=categorie,
                date_signal=date_signal,
                normalisation_json=normalisation,
            )
        )
    return signal_id


def signaux_deja_lus(engine: Engine, source_ids: list[str]) -> set[str]:
    if not source_ids:
        return set()
    with engine.connect() as cx:
        rows = cx.execute(select(signals.c.source_id).where(signals.c.source_id.in_(source_ids))).all()
        return {r[0] for r in rows}


def signal_deja_traite(engine: Engine, source_id: str) -> bool:
    """Vrai si ce source_id a déjà donné lieu à un signal (une run
    précédente, ou plus tôt dans la run en cours) — sert à la reprise après
    panne : on ne retraite jamais un signal déjà vu."""
    with engine.connect() as cx:
        return cx.execute(select(signals.c.id).where(signals.c.source_id == source_id).limit(1)).first() is not None


# -------------------------------------------------------- opportunities --

def creer_opportunite(engine: Engine, *, titre: str, acheteur: str, probleme: str, mecanisme_ia: str,
                       secteur: str, statut: str, cluster_id: str | None,
                       secteur_provenance: str | None = None, secteur_citation: str | None = None,
                       mots_cles_en: str | None = None, mots_cles_fr: str | None = None) -> str:
    opp_id = _uid()
    with engine.begin() as cx:
        cx.execute(
            insert(opportunities).values(
                id=opp_id,
                titre=titre,
                acheteur=acheteur,
                probleme=probleme,
                mecanisme_ia=mecanisme_ia,
                secteur=secteur,
                secteur_provenance=secteur_provenance,
                secteur_citation=secteur_citation,
                mots_cles_en=mots_cles_en,
                mots_cles_fr=mots_cles_fr,
                statut=statut,
                cluster_id=cluster_id,
                date_creation=_now(),
                date_maj=_now(),
            )
        )
    return opp_id


def maj_statut_opportunite(engine: Engine, opportunity_id: str, statut: str) -> None:
    with engine.begin() as cx:
        cx.execute(
            update(opportunities).where(opportunities.c.id == opportunity_id).values(statut=statut, date_maj=_now())
        )


def maj_opportunite_depuis_reprise(
    engine: Engine, opportunity_id: str, *, titre: str, acheteur: str, probleme: str, mecanisme_ia: str,
    secteur: str, secteur_provenance: str | None, secteur_citation: str | None,
    mots_cles_en: str | None, mots_cles_fr: str | None, statut: str,
) -> None:
    """Sous-étape 3.13 (`app.reprise`) : une reprise réussie (le Scout a pu
    être rappelé avec un vrai modèle, voir
    `app.pipeline.orchestrator._phase_reprise`) MET À JOUR le dossier
    existant plutôt que d'en créer un nouveau -- même identifiant, donc
    aucune duplication pour Jarvis/les métriques. Champs non listés ici
    (`id`, `cluster_id`, `date_creation`) restent inchangés, jamais réécrits
    par cette fonction."""
    with engine.begin() as cx:
        cx.execute(
            update(opportunities).where(opportunities.c.id == opportunity_id).values(
                titre=titre, acheteur=acheteur, probleme=probleme, mecanisme_ia=mecanisme_ia,
                secteur=secteur, secteur_provenance=secteur_provenance, secteur_citation=secteur_citation,
                mots_cles_en=mots_cles_en, mots_cles_fr=mots_cles_fr, statut=statut, date_maj=_now(),
            )
        )


def opportunites_creees_par_repli_scout(engine: Engine, depuis: datetime) -> list[dict]:
    """Sous-étape 3.13 (`app.reprise`) : opportunités À MARQUER `a_reprendre`
    -- créées à partir de `depuis`, dont TOUTES les évaluations Scout connues
    sont un repli sans modèle (`assessments.modele == "heuristique"`), pas
    déjà marquées. Deux façons de sortir de cette liste, la commande reste
    idempotente sans y penser : le statut passe à `a_reprendre` (ce tour-ci),
    ou une reprise réussie ajoute une nouvelle évaluation Scout avec un vrai
    nom de modèle (plus tard, voir `app.pipeline.orchestrator._phase_reprise`)."""
    scout_reel = select(assessments.c.opportunity_id).where(
        assessments.c.role == "scout", assessments.c.modele != "heuristique",
    )
    with engine.connect() as cx:
        rows = cx.execute(
            select(opportunities)
            .where(
                opportunities.c.date_creation >= depuis,
                opportunities.c.statut != "a_reprendre",
                opportunities.c.id.not_in(scout_reel),
            )
            .order_by(opportunities.c.date_creation)
        ).mappings().all()
        return [dict(r) for r in rows]


def signal_origine_scout(engine: Engine, opportunity_id: str) -> dict | None:
    """Retrouve le signal d'origine (texte collecté, id de source) qui a
    produit cette opportunité, via la preuve posée à la création par
    `app/pipeline/orchestrator.py::_phase_collecte_et_scout`
    (`claim` commençant par « Scout: »). La PLUS ANCIENNE si plusieurs (une
    opportunité fusionnée, `app/pipeline/dedupe.py`, peut en porter
    plusieurs) -- c'est le signal réellement à l'origine du dossier."""
    with engine.connect() as cx:
        row = cx.execute(
            select(opportunity_evidence.c.source_id, sources.c.extrait)
            .select_from(opportunity_evidence.join(sources, opportunity_evidence.c.source_id == sources.c.id))
            .where(
                opportunity_evidence.c.opportunity_id == opportunity_id,
                opportunity_evidence.c.claim.like("Scout: %"),
            )
            .order_by(opportunity_evidence.c.date_creation)
            .limit(1)
        ).mappings().first()
        return dict(row) if row else None


def lister_opportunites_ouvertes(engine: Engine, secteur: str | None = None) -> list[dict]:
    with engine.connect() as cx:
        q = select(opportunities)
        if secteur:
            q = q.where(opportunities.c.secteur == secteur)
        return [dict(r) for r in cx.execute(q).mappings().all()]


def lister_opportunites_par_run(engine: Engine, run_id: str) -> list[dict]:
    """Top du matin : dernier score de chaque opportunité touchée par ce run."""
    with engine.connect() as cx:
        opp_ids = [r[0] for r in cx.execute(
            select(assessments.c.opportunity_id).where(assessments.c.run_id == run_id).distinct()
        ).all()]
        if not opp_ids:
            return []
        out = []
        for opp_id in opp_ids:
            opp = cx.execute(select(opportunities).where(opportunities.c.id == opp_id)).mappings().first()
            dernier_score = cx.execute(
                select(scores).where(scores.c.opportunity_id == opp_id).order_by(scores.c.date_creation.desc())
            ).mappings().first()
            out.append({"opportunite": dict(opp) if opp else None, "dernier_score": dict(dernier_score) if dernier_score else None})
        out.sort(key=lambda r: (r["dernier_score"] or {}).get("score_prudent", 0), reverse=True)
        return out


# ----------------------------------------------------------- evidence ----

def inserer_evidence(engine: Engine, *, opportunity_id: str, source_id: str, claim: str, type_: str,
                      independant: bool) -> str:
    ev_id = _uid()
    with engine.begin() as cx:
        cx.execute(
            insert(opportunity_evidence).values(
                id=ev_id,
                opportunity_id=opportunity_id,
                source_id=source_id,
                claim=claim,
                type=type_,
                independant=independant,
                date_creation=_now(),
            )
        )
    return ev_id


def sources_deja_citees(engine: Engine, opportunity_id: str) -> set[str]:
    with engine.connect() as cx:
        rows = cx.execute(
            select(opportunity_evidence.c.source_id).where(opportunity_evidence.c.opportunity_id == opportunity_id)
        ).all()
        return {r[0] for r in rows}


def dossiers_rattaches_a_url(engine: Engine, url_canonique: str) -> set[str]:
    """Sous-étape 3.17 : les dossiers auxquels cette URL est déjà rattachée
    comme preuve, toutes versions de la page confondues (une même URL peut
    avoir plusieurs lignes `sources`, une par empreinte de contenu), SIGNAL
    D'ORIGINE EXCLU (preuve « Scout: … », posée à la création du dossier) --
    c'est le nombre que plafonne `max_dossiers_par_source`."""
    with engine.connect() as cx:
        rows = cx.execute(
            select(opportunity_evidence.c.opportunity_id)
            .select_from(opportunity_evidence.join(sources, opportunity_evidence.c.source_id == sources.c.id))
            .where(sources.c.url_canonique == url_canonique, ~opportunity_evidence.c.claim.like("Scout: %"))
            .distinct()
        ).all()
        return {r[0] for r in rows}


def infos_sources_du_dossier(engine: Engine, opportunity_id: str) -> dict[str, dict]:
    """Sous-étape 3.17 : pour chaque source rattachée à ce dossier,
    `{"domaine": ..., "origine": bool}` -- ce dont le moteur de score a besoin
    pour exiger deux sources distinctes (`app.scoring.engine.InfoSource`).
    `origine` : la source porte une preuve « Scout: … » pour ce dossier (le
    signal que le Scout a lu, voir `signal_origine_scout`)."""
    with engine.connect() as cx:
        rows = cx.execute(
            select(opportunity_evidence.c.source_id, sources.c.domaine, opportunity_evidence.c.claim)
            .select_from(opportunity_evidence.join(sources, opportunity_evidence.c.source_id == sources.c.id))
            .where(opportunity_evidence.c.opportunity_id == opportunity_id)
        ).all()
    infos: dict[str, dict] = {}
    for source_id, domaine, claim in rows:
        info = infos.setdefault(source_id, {"domaine": domaine, "origine": False})
        if claim.startswith("Scout: "):
            info["origine"] = True
    return infos


def opportunite_pour_source(engine: Engine, source_id: str) -> str | None:
    """Si ce source_id est déjà cité comme preuve d'une opportunité,
    renvoie son id — sert à la reprise après panne (ne pas recréer un
    dossier pour un signal déjà rattaché)."""
    with engine.connect() as cx:
        row = cx.execute(
            select(opportunity_evidence.c.opportunity_id).where(opportunity_evidence.c.source_id == source_id).limit(1)
        ).first()
        return row[0] if row else None


def empreintes_sources_citees(engine: Engine, opportunity_id: str) -> set[str]:
    """Empreintes de contenu déjà citées pour cette opportunité — sert à
    repérer une même source recopiée sous deux URLs (§2 : ne pas compter
    deux fois la même preuve comme indépendante)."""
    with engine.connect() as cx:
        rows = cx.execute(
            select(sources.c.empreinte)
            .select_from(opportunity_evidence.join(sources, opportunity_evidence.c.source_id == sources.c.id))
            .where(opportunity_evidence.c.opportunity_id == opportunity_id)
        ).all()
        return {r[0] for r in rows}


# --------------------------------------------------------- assessments ---

def inserer_assessment(engine: Engine, *, opportunity_id: str, run_id: str, role: str, payload: dict,
                        modele: str, version_prompt: str, inconnues: list) -> str:
    a_id = _uid()
    with engine.begin() as cx:
        cx.execute(
            insert(assessments).values(
                id=a_id,
                opportunity_id=opportunity_id,
                run_id=run_id,
                role=role,
                payload_json=payload,
                modele=modele,
                version_prompt=version_prompt,
                inconnues_json=inconnues,
                date_creation=_now(),
            )
        )
    return a_id


# -------------------------------------------------------------- scores ---

def inserer_score(engine: Engine, *, opportunity_id: str, run_id: str, version_poids: str, valeurs: dict,
                   score_brut: float, score_prudent: float, couverture_preuves: float, flags: list,
                   decision_critic: str | None, origine: str | None = None) -> str:
    """Toujours un INSERT : jamais d'UPDATE sur cette table (historique
    conservé, §5 « pas d'écrasement silencieux »)."""
    s_id = _uid()
    with engine.begin() as cx:
        cx.execute(
            insert(scores).values(
                id=s_id,
                opportunity_id=opportunity_id,
                run_id=run_id,
                version_poids=version_poids,
                valeurs_json=valeurs,
                score_brut=score_brut,
                score_prudent=score_prudent,
                couverture_preuves=couverture_preuves,
                flags_json=flags,
                decision_critic=decision_critic,
                origine=origine,
                date_creation=_now(),
            )
        )
    return s_id


def historique_scores(engine: Engine, opportunity_id: str) -> list[dict]:
    with engine.connect() as cx:
        rows = cx.execute(
            select(scores).where(scores.c.opportunity_id == opportunity_id).order_by(scores.c.date_creation)
        ).mappings().all()
        return [dict(r) for r in rows]


# ----------------------------------------------------------- decisions ---

def inserer_decision(engine: Engine, *, opportunity_id: str, auteur: str, action: str, justification: str | None) -> str:
    d_id = _uid()
    with engine.begin() as cx:
        cx.execute(
            insert(decisions).values(
                id=d_id,
                opportunity_id=opportunity_id,
                auteur=auteur,
                action=action,
                date_creation=_now(),
                justification=justification,
            )
        )
    return d_id


# ------------------------------------------------------------- usage -----

def inserer_usage_event(engine: Engine, *, run_id: str, fournisseur: str, modele_ou_actor: str, appels: int,
                         tokens_in: int | None, tokens_out: int | None, cout: float, role: str | None = None,
                         opportunity_id: str | None = None, devise: str = "EUR",
                         issue: str | None = None, sortie_tronquee: bool | None = None) -> str:
    """`issue`/`sortie_tronquee` (sous-étape 3.10) : `None` par défaut --
    comportement inchangé pour tout appelant existant (compteurs de
    l'Enquêteur compris, qui ne sont pas des appels modèle et ne renseignent
    jamais ces deux champs). Renseignés uniquement par
    `app.pipeline.budget.BudgetTracker.enregistrer_reel`, depuis
    `app.adapters.model_client.ModelClient.appeler_structure`."""
    u_id = _uid()
    with engine.begin() as cx:
        cx.execute(
            insert(usage_events).values(
                id=u_id,
                run_id=run_id,
                fournisseur=fournisseur,
                modele_ou_actor=modele_ou_actor,
                appels=appels,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cout_declare_ou_estime=cout,
                devise=devise,
                date_creation=_now(),
                role=role,
                opportunity_id=opportunity_id,
                issue=issue,
                sortie_tronquee=sortie_tronquee,
            )
        )
    return u_id


def cout_total_run(engine: Engine, run_id: str) -> float:
    with engine.connect() as cx:
        rows = cx.execute(
            select(usage_events.c.cout_declare_ou_estime).where(usage_events.c.run_id == run_id)
        ).all()
        return sum(r[0] for r in rows)


def cout_total_jour_utc(engine: Engine, jour: date) -> float:
    """Dépense du jour calendaire UTC, tous runs confondus — la clé du
    plafond dur (§3.4), par opposition à `cout_total_run` (clé du run,
    reporting uniquement). Voir `rapports/DIAGNOSTIC_BUDGET_2026-09-25.md`."""
    debut, fin = _bornes_jour_utc(jour)
    with engine.connect() as cx:
        rows = cx.execute(
            select(usage_events.c.cout_declare_ou_estime).where(
                usage_events.c.date_creation >= debut, usage_events.c.date_creation < fin
            )
        ).all()
        return sum(r[0] for r in rows)


def nombre_appels_approfondis_jour_utc(engine: Engine, jour: date) -> int:
    """Nombre d'appels déjà journalisés aujourd'hui (UTC) pour les rôles
    Analyst/Critic — second garde-fou, indépendant de toute estimation de
    prix (sous-étape 0.7). Les lignes antérieures à la migration (`role`
    NULL) ne comptent jamais ici : elles datent d'un autre jour de toute
    façon."""
    debut, fin = _bornes_jour_utc(jour)
    with engine.connect() as cx:
        return cx.execute(
            select(func.count()).select_from(usage_events).where(
                usage_events.c.date_creation >= debut,
                usage_events.c.date_creation < fin,
                usage_events.c.role.in_(("analyst", "critic", "faisabilite")),
            )
        ).scalar_one()


def nombre_evenements_role_jour_utc(engine: Engine, jour: date, *, role: str) -> int:
    """Nombre d'évènements `usage_events` d'UN rôle donné pour le jour UTC.
    Sous-étape 3.1 (AMELIORATIONS.md) : sert les deux compteurs de
    l'Enquêteur (requêtes de recherche, fetchs de page), indépendants du
    plafond en euros et du plafond d'appels approfondis (0.7, ci-dessus)."""
    debut, fin = _bornes_jour_utc(jour)
    with engine.connect() as cx:
        return cx.execute(
            select(func.count()).select_from(usage_events).where(
                usage_events.c.date_creation >= debut,
                usage_events.c.date_creation < fin,
                usage_events.c.role == role,
            )
        ).scalar_one()


def nombre_evenements_role_fournisseur_jour_utc(
    engine: Engine, jour: date, *, role: str, fournisseur: str
) -> int:
    """Sous-étape 3.9 (AMELIORATIONS.md) : variante de la fonction ci-dessus,
    filtrée en plus par `fournisseur` -- sert le plafond dédié à Reddit
    (`app.pipeline.budget.BudgetTracker.requetes_recherche_reddit_jour_engagees`),
    une part du plafond global de requêtes de recherche."""
    debut, fin = _bornes_jour_utc(jour)
    with engine.connect() as cx:
        return cx.execute(
            select(func.count()).select_from(usage_events).where(
                usage_events.c.date_creation >= debut,
                usage_events.c.date_creation < fin,
                usage_events.c.role == role,
                usage_events.c.fournisseur == fournisseur,
            )
        ).scalar_one()


# ------------------------------------------------ tirages de contrôle ----

def nombre_tirages_controle_jour_utc(engine: Engine, jour: date) -> int:
    """Sous-étape 3.17 : tirages de contrôle déjà effectués pendant cette
    journée UTC, tous runs confondus -- même clé que le plafond en euros
    (`nombre_appels_approfondis_jour_utc`), pour qu'un redémarrage du worker
    ne remette jamais le compteur à zéro."""
    debut, fin = _bornes_jour_utc(jour)
    with engine.connect() as cx:
        return cx.execute(
            select(func.count()).select_from(tirages_controle_rejetes).where(
                tirages_controle_rejetes.c.date_creation >= debut,
                tirages_controle_rejetes.c.date_creation < fin,
            )
        ).scalar_one()


def opportunites_deja_tirees_controle(engine: Engine) -> set[str]:
    """Opportunités déjà retirées au moins une fois par l'échantillon de
    contrôle des rejetés — jamais deux fois la même (§2, pipeline)."""
    with engine.connect() as cx:
        rows = cx.execute(select(tirages_controle_rejetes.c.opportunity_id)).all()
        return {r[0] for r in rows}


def inserer_tirage_controle_rejete(engine: Engine, *, opportunity_id: str, run_id: str, decision_avant: str,
                                    decision_apres: str) -> str:
    t_id = _uid()
    with engine.begin() as cx:
        cx.execute(
            insert(tirages_controle_rejetes).values(
                id=t_id,
                opportunity_id=opportunity_id,
                run_id=run_id,
                date_creation=_now(),
                decision_avant=decision_avant,
                decision_apres=decision_apres,
            )
        )
    return t_id


# ---------------------------------------------------------- controles ----

def lire_pause_all(engine: Engine) -> bool:
    with engine.connect() as cx:
        row = cx.execute(select(controles.c.valeur).where(controles.c.cle == "pause_all")).first()
        return bool(row[0]) if row else False


def definir_pause_all(engine: Engine, valeur: bool) -> None:
    upsert = sqlite_upsert if engine.dialect.name == "sqlite" else postgres_upsert
    stmt = upsert(controles).values(cle="pause_all", valeur=valeur, date_maj=_now())
    stmt = stmt.on_conflict_do_update(index_elements=["cle"], set_={"valeur": valeur, "date_maj": _now()})
    with engine.begin() as cx:
        cx.execute(stmt)


# ------------------------------------------------------ journal HTTP -----

def enregistrer_appel_http(engine: Engine, *, hote: str, flux_ou_fournisseur: str, code_http: int | None,
                            erreur: str | None, duree_ms: float) -> str:
    """Sous-étape 3.7 (AMELIORATIONS.md) : une ligne par appel HTTP réel --
    voir `app/storage/schema.py::journal_http` pour ce qui est (et n'est
    jamais) enregistré. Appelée uniquement depuis `app/adapters/http.py`."""
    j_id = _uid()
    with engine.begin() as cx:
        cx.execute(
            insert(journal_http).values(
                id=j_id, horodatage=_now(), hote=hote, flux_ou_fournisseur=flux_ou_fournisseur,
                code_http=code_http, erreur=erreur, duree_ms=duree_ms,
            )
        )
    return j_id


def lister_appels_http_jour_utc(engine: Engine, jour: date) -> list[dict]:
    """Sous-étape 3.7, point 2 : lecture brute pour `app.metriques`, qui
    agrège par flux/fournisseur (nombre d'appels, 429/403/autres erreurs,
    taux de succès)."""
    debut, fin = _bornes_jour_utc(jour)
    with engine.connect() as cx:
        rows = cx.execute(
            select(journal_http.c.flux_ou_fournisseur, journal_http.c.code_http, journal_http.c.erreur).where(
                journal_http.c.horodatage >= debut, journal_http.c.horodatage < fin
            )
        ).all()
        return [{"flux_ou_fournisseur": f, "code_http": c, "erreur": e} for f, c, e in rows]


# ---------------------------------------- planificateur de recherche -----

def lire_dernieres_visites_recherche(engine: Engine) -> dict[str, datetime]:
    """Sous-étape 1.2 : mémoire du planificateur (`app/pipeline/planificateur_recherche.py`)
    — un flux absent de ce dictionnaire n'a jamais été visité.

    SQLite (tests, dev local) ne conserve pas le fuseau horaire d'une
    `DateTime(timezone=True)` : une valeur relue redevient naïve, alors que
    tout ce qu'on y écrit ici passe par `_now()` (toujours UTC) — on la
    force donc explicitement en UTC pour que la comparaison avec `maintenant`
    (toujours "aware" côté appelant) ne plante jamais. Sans effet sur
    PostgreSQL (production), qui renvoie déjà une valeur "aware"."""
    with engine.connect() as cx:
        rows = cx.execute(select(etats_flux_recherche)).all()
        return {
            r.cle: (r.derniere_visite if r.derniere_visite.tzinfo else r.derniere_visite.replace(tzinfo=timezone.utc))
            for r in rows
        }


def marquer_flux_recherche_visites(engine: Engine, cles: list[str], quand: datetime) -> None:
    """Idempotent (upsert) : rejouer le même passage ne duplique rien."""
    if not cles:
        return
    upsert = sqlite_upsert if engine.dialect.name == "sqlite" else postgres_upsert
    with engine.begin() as cx:
        for cle in cles:
            stmt = upsert(etats_flux_recherche).values(cle=cle, derniere_visite=quand)
            stmt = stmt.on_conflict_do_update(index_elements=["cle"], set_={"derniere_visite": quand})
            cx.execute(stmt)


# ------------------------------------------------- disjoncteur Enquêteur --

def lire_disjoncteur_enqueteur(engine: Engine, cle: str) -> dict | None:
    """Sous-étape 3.11. `None` si `cle` n'a jamais enregistré d'échec —
    l'appelant (`app.enqueteur.fournisseurs_gratuits`) le traite alors comme
    `app.enqueteur.disjoncteur.ETAT_INITIAL`.

    Même remarque que `lire_dernieres_visites_recherche` : SQLite ne
    conserve pas le fuseau horaire d'une `DateTime(timezone=True)` — une
    valeur relue redevient naïve, on la force donc en UTC pour rester
    comparable à `datetime.now(timezone.utc)` côté appelant. Sans effet sur
    PostgreSQL (production), déjà "aware"."""
    with engine.connect() as cx:
        row = cx.execute(
            select(etats_disjoncteur_enqueteur).where(etats_disjoncteur_enqueteur.c.cle == cle)
        ).mappings().first()
    if row is None:
        return None
    pause_jusqu_a = row["pause_jusqu_a"]
    if pause_jusqu_a is not None and pause_jusqu_a.tzinfo is None:
        pause_jusqu_a = pause_jusqu_a.replace(tzinfo=timezone.utc)
    return {"echecs_consecutifs": row["echecs_consecutifs"], "pause_jusqu_a": pause_jusqu_a}


def ecrire_disjoncteur_enqueteur(
    engine: Engine, cle: str, *, echecs_consecutifs: int, pause_jusqu_a: datetime | None,
) -> None:
    """Idempotent (upsert), même mécanisme que `marquer_flux_recherche_visites`."""
    upsert = sqlite_upsert if engine.dialect.name == "sqlite" else postgres_upsert
    with engine.begin() as cx:
        stmt = upsert(etats_disjoncteur_enqueteur).values(
            cle=cle, echecs_consecutifs=echecs_consecutifs, pause_jusqu_a=pause_jusqu_a, date_maj=_now(),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["cle"],
            set_={"echecs_consecutifs": echecs_consecutifs, "pause_jusqu_a": pause_jusqu_a, "date_maj": _now()},
        )
        cx.execute(stmt)


# Sous-étape 3.13 : une seule clé -- Scout/Analyst/Critic partagent le même
# `ModelClient`, donc le même disjoncteur (contrairement à Reddit ci-dessus,
# qui a une clé par fournisseur, extensible). Constante ici plutôt que dans
# `app.pipeline.disjoncteur_api` : c'est un détail de STOCKAGE, la ligne d'une
# table à clé primaire fixe, pas une notion que le module pur a besoin de
# connaître.
CLE_DISJONCTEUR_API = "modele"


def lire_disjoncteur_api(engine: Engine) -> dict | None:
    """`None` si aucun échec n'a jamais été enregistré -- l'appelant
    (`app.adapters.model_client.ModelClient`) le traite alors comme
    `app.pipeline.disjoncteur_api.ETAT_INITIAL`, même convention que
    `lire_disjoncteur_enqueteur` ci-dessus (SQLite renvoie un datetime naïf,
    forcé en UTC pour rester comparable à `datetime.now(timezone.utc))`."""
    with engine.connect() as cx:
        row = cx.execute(
            select(etats_disjoncteur_api).where(etats_disjoncteur_api.c.cle == CLE_DISJONCTEUR_API)
        ).mappings().first()
    if row is None:
        return None
    depuis = row["depuis"]
    if depuis is not None and depuis.tzinfo is None:
        depuis = depuis.replace(tzinfo=timezone.utc)
    pause_jusqu_a = row["pause_jusqu_a"]
    if pause_jusqu_a is not None and pause_jusqu_a.tzinfo is None:
        pause_jusqu_a = pause_jusqu_a.replace(tzinfo=timezone.utc)
    return {
        "echecs_consecutifs": row["echecs_consecutifs"],
        "en_erreur": row["en_erreur"],
        "depuis": depuis,
        "pause_jusqu_a": pause_jusqu_a,
        "dernier_message": row["dernier_message"],
    }


def ecrire_disjoncteur_api(
    engine: Engine, *, echecs_consecutifs: int, en_erreur: bool, depuis: datetime | None,
    pause_jusqu_a: datetime | None, dernier_message: str | None,
) -> None:
    """Idempotent (upsert), même mécanisme que `ecrire_disjoncteur_enqueteur`."""
    upsert = sqlite_upsert if engine.dialect.name == "sqlite" else postgres_upsert
    valeurs = {
        "echecs_consecutifs": echecs_consecutifs, "en_erreur": en_erreur, "depuis": depuis,
        "pause_jusqu_a": pause_jusqu_a, "dernier_message": dernier_message, "date_maj": _now(),
    }
    with engine.begin() as cx:
        stmt = upsert(etats_disjoncteur_api).values(cle=CLE_DISJONCTEUR_API, **valeurs)
        stmt = stmt.on_conflict_do_update(index_elements=["cle"], set_=valeurs)
        cx.execute(stmt)


# --------------------------------------------- sous-étape 4.1 (faisabilité) ---

def inserer_faisabilite(engine: Engine, *, opportunity_id: str, run_id: str | None, origine: str, payload: dict,
                        accessible_solo: bool | None, motif_exclusion: str | None, modele: str | None) -> str:
    """Append-only, comme `scores` : la dernière ligne par dossier fait foi."""
    f_id = _uid()
    with engine.begin() as cx:
        cx.execute(
            insert(faisabilites).values(
                id=f_id, opportunity_id=opportunity_id, run_id=run_id, origine=origine, payload_json=payload,
                accessible_solo=accessible_solo, motif_exclusion=motif_exclusion, modele=modele,
                date_creation=_now(),
            )
        )
    return f_id


def dossiers_avec_faisabilite(engine: Engine) -> set[str]:
    """Vide si la table n'existe pas encore (base lue en lecture seule AVANT
    la migration de déploiement -- simulation de `app.recalcul`)."""
    if "faisabilites" not in inspect(engine).get_table_names():
        return set()
    with engine.connect() as cx:
        return {r[0] for r in cx.execute(select(faisabilites.c.opportunity_id).distinct()).all()}


def dernier_score_par_dossier(engine: Engine) -> dict[str, dict]:
    """Dernière ligne de `scores` de chaque dossier (par `date_creation`).
    Ne sélectionne que les colonnes réellement présentes (la colonne
    `origine` de 4.1 manque tant que la migration n'a pas tourné -- base lue
    en lecture seule avant le déploiement)."""
    presentes = {c["name"] for c in inspect(engine).get_columns("scores")}
    colonnes = [c for c in scores.c if c.name in presentes]
    with engine.connect() as cx:
        lignes = cx.execute(select(*colonnes).order_by(scores.c.date_creation)).mappings().all()
    derniers: dict[str, dict] = {}
    for ligne in lignes:  # ordre croissant : la dernière écrase
        derniers[ligne["opportunity_id"]] = {"origine": None, **dict(ligne)}
    return derniers


def dernier_assessment(engine: Engine, opportunity_id: str, role: str) -> dict | None:
    with engine.connect() as cx:
        row = cx.execute(
            select(assessments).where(assessments.c.opportunity_id == opportunity_id, assessments.c.role == role)
            .order_by(assessments.c.date_creation.desc()).limit(1)
        ).mappings().first()
    return dict(row) if row else None


def derniers_assessments_par_dossier(engine: Engine, role: str) -> dict[str, dict]:
    """Version en une seule requête de `dernier_assessment` pour tous les
    dossiers (recalcul 4.1 : une requête distante par dossier était trop lente)."""
    with engine.connect() as cx:
        lignes = cx.execute(
            select(assessments).where(assessments.c.role == role).order_by(assessments.c.date_creation)
        ).mappings().all()
    derniers: dict[str, dict] = {}
    for ligne in lignes:  # ordre croissant : la dernière écrase
        derniers[ligne["opportunity_id"]] = dict(ligne)
    return derniers


def infos_sources_tous_dossiers(engine: Engine) -> dict[str, dict[str, dict]]:
    """Version en une seule requête de `infos_sources_du_dossier` :
    `{opportunity_id: {source_id: {"domaine", "origine"}}}`."""
    with engine.connect() as cx:
        rows = cx.execute(
            select(opportunity_evidence.c.opportunity_id, opportunity_evidence.c.source_id, sources.c.domaine,
                   opportunity_evidence.c.claim)
            .select_from(opportunity_evidence.join(sources, opportunity_evidence.c.source_id == sources.c.id))
        ).all()
    tous: dict[str, dict[str, dict]] = {}
    for opp_id, source_id, domaine, claim in rows:
        info = tous.setdefault(opp_id, {}).setdefault(source_id, {"domaine": domaine, "origine": False})
        if claim.startswith("Scout: "):
            info["origine"] = True
    return tous


# ------------------------------------- sous-étape V2.2 (établissements) -----

def enregistrer_comptage_etablissements(
    engine: Engine, *, code_naf: str, naf_version: str, departement: str, nb_entreprises_actives: int,
    comptage_plafonne: bool, nb_etablissements_listes: int | None, echantillon_complet: bool | None, plafond_echantillon: int | None,
    requetes: int, source_url: str, horodatage: datetime | None = None,
) -> str:
    """Append-only : une ligne par mesure, la plus récente fait foi (`derniers_comptages_etablissements`)."""
    e_id = _uid()
    with engine.begin() as cx:
        cx.execute(
            insert(etablissements_secteur).values(
                id=e_id, code_naf=code_naf, naf_version=naf_version, departement=departement,
                nb_entreprises_actives=nb_entreprises_actives, comptage_plafonne=comptage_plafonne,
                nb_etablissements_listes=nb_etablissements_listes,
                echantillon_complet=echantillon_complet, plafond_echantillon=plafond_echantillon,
                requetes=requetes, source_url=source_url, horodatage=horodatage or _now(),
            )
        )
    return e_id


def derniers_comptages_etablissements(engine: Engine, naf_version: str) -> dict[tuple[str, str], dict]:
    """Dernière mesure de chaque paire (code NAF, département) -> ligne complète."""
    with engine.connect() as cx:
        lignes = cx.execute(
            select(etablissements_secteur)
            .where(etablissements_secteur.c.naf_version == naf_version)
            .order_by(etablissements_secteur.c.horodatage.asc())
        ).mappings().all()
    derniers: dict[tuple[str, str], dict] = {}
    for ligne in lignes:  # ordre croissant : la dernière écrase
        derniers[(ligne["code_naf"], ligne["departement"])] = dict(ligne)
    return derniers


def enregistrer_prospects(
    engine: Engine, prospects: list[dict[str, Any]], *, naf_version: str, maintenant: datetime | None = None,
) -> tuple[int, int]:
    """Upsert par (siret, naf_version) ; renvoie (nouveaux, déjà connus). Un déjà connu voit ses champs
    rafraîchis et sa `derniere_vue` avancer ; `premiere_collecte` ne bouge jamais ; rien n'est jamais supprimé."""
    if not prospects:
        return 0, 0
    quand = maintenant or _now()
    upsert = sqlite_upsert if engine.dialect.name == "sqlite" else postgres_upsert
    sirets = [p["siret"] for p in prospects]
    connus: set[str] = set()
    with engine.begin() as cx:
        for i in range(0, len(sirets), 300):
            lot = sirets[i:i + 300]
            connus.update(
                r[0] for r in cx.execute(
                    select(prospection.c.siret).where(prospection.c.naf_version == naf_version, prospection.c.siret.in_(lot))
                )
            )
        for p in prospects:
            valeurs = {k: v for k, v in p.items() if k != "siret"}
            valeurs["derniere_vue"] = quand
            stmt = upsert(prospection).values(
                id=_uid(), siret=p["siret"], naf_version=naf_version, premiere_collecte=quand, **valeurs,
            )
            stmt = stmt.on_conflict_do_update(index_elements=["siret", "naf_version"], set_=valeurs)
            cx.execute(stmt)
    nouveaux = len(set(sirets) - connus)
    return nouveaux, len(set(sirets)) - nouveaux


def resume_etablissements(engine: Engine, naf_version: str, *, rayon_km: float, departements_zone: tuple[str, ...]) -> dict:
    """Lecture seule pour `app.metriques` : secteurs couverts, entreprises et prospects en zone."""
    derniers = derniers_comptages_etablissements(engine, naf_version)
    secteurs_couverts = {code for (code, dep) in derniers if dep != "FR"}
    entreprises_zone = sum(
        l["nb_entreprises_actives"] for (code, dep), l in derniers.items() if dep in departements_zone
    )
    with engine.connect() as cx:
        total = cx.execute(
            select(func.count()).select_from(prospection).where(prospection.c.naf_version == naf_version)
        ).scalar_one()
        en_rayon = cx.execute(
            select(func.count()).select_from(prospection).where(
                prospection.c.naf_version == naf_version, prospection.c.distance_centre_km <= rayon_km)
        ).scalar_one()
    dernieres = [l["horodatage"] for l in derniers.values()]
    return {
        "naf_version": naf_version,
        "secteurs_couverts": len(secteurs_couverts),
        "mesures_secteur_departement": len(derniers),
        "mesures_plafonnees_api": sum(1 for l in derniers.values() if l["comptage_plafonne"]),
        "entreprises_actives_departements_zone": entreprises_zone,
        "prospects_total": total,
        "prospects_dans_le_rayon": en_rayon,
        "derniere_mesure": max(dernieres).isoformat() if dernieres else None,
    }


# ------------------------------------------ sous-étape V2.3 (offres d'emploi) -----

def enregistrer_offres(
    engine: Engine, offres: list[dict[str, Any]], *, naf_version: str, maintenant: datetime | None = None,
) -> tuple[int, int]:
    """Upsert par `id_offre` ; renvoie (nouvelles, déjà connues). Une offre déjà connue voit ses champs
    rafraîchis (actualisation, salaire...) et sa `derniere_vue` avancer ; `premiere_collecte` ne bouge jamais."""
    if not offres:
        return 0, 0
    quand = maintenant or _now()
    upsert = sqlite_upsert if engine.dialect.name == "sqlite" else postgres_upsert
    ids = list({o["id_offre"] for o in offres})
    connues: set[str] = set()
    with engine.begin() as cx:
        for i in range(0, len(ids), 300):
            connues.update(
                r[0] for r in cx.execute(select(offres_emploi.c.id_offre).where(offres_emploi.c.id_offre.in_(ids[i:i + 300])))
            )
        for o in offres:
            valeurs = {k: v for k, v in o.items() if k != "id_offre"}
            valeurs["derniere_vue"] = quand
            valeurs["naf_version"] = naf_version
            stmt = upsert(offres_emploi).values(id=_uid(), id_offre=o["id_offre"], premiere_collecte=quand, **valeurs)
            cx.execute(stmt.on_conflict_do_update(index_elements=["id_offre"], set_=valeurs))
    nouvelles = len(set(ids) - connues)
    return nouvelles, len(ids) - nouvelles


def enregistrer_collecte_offres(
    engine: Engine, *, code_naf: str, naf_version: str, debut: datetime, fin: datetime, nb_offres: int,
    nb_nouvelles: int, requetes: int, fenetres_tronquees: int, horodatage: datetime | None = None,
) -> str:
    c_id = _uid()
    with engine.begin() as cx:
        cx.execute(insert(collectes_offres).values(
            id=c_id, code_naf=code_naf, naf_version=naf_version, debut=debut, fin=fin, nb_offres=nb_offres,
            nb_nouvelles=nb_nouvelles, requetes=requetes, fenetres_tronquees=fenetres_tronquees,
            horodatage=horodatage or _now(),
        ))
    return c_id


def _aware(valeur: datetime | None) -> datetime | None:
    if valeur is not None and valeur.tzinfo is None:
        return valeur.replace(tzinfo=timezone.utc)
    return valeur


def dernieres_fins_collecte_offres(engine: Engine, naf_version: str) -> dict[str, datetime]:
    """Pour chaque code NAF : la `fin` de sa collecte la plus récente."""
    with engine.connect() as cx:
        lignes = cx.execute(
            select(collectes_offres.c.code_naf, func.max(collectes_offres.c.fin))
            .where(collectes_offres.c.naf_version == naf_version).group_by(collectes_offres.c.code_naf)
        ).all()
    return {code: _aware(fin) for code, fin in lignes}


def resume_offres(
    engine: Engine, naf_version: str, *, departements_zone: tuple[str, ...], maintenant: datetime | None = None,
) -> dict:
    """Lecture seule pour `app.metriques`."""
    quand = maintenant or _now()
    with engine.connect() as cx:
        def _compte(*conditions) -> int:
            return cx.execute(select(func.count()).select_from(offres_emploi).where(
                offres_emploi.c.naf_version == naf_version, *conditions)).scalar_one()

        total = _compte()
        derniers_90_jours = _compte(offres_emploi.c.date_creation >= quand - timedelta(days=90))
        en_zone = _compte(offres_emploi.c.departement.in_(departements_zone))
        avec_salaire = _compte(offres_emploi.c.salaire_annuel_min_eur.is_not(None))
        codes = cx.execute(select(func.count(func.distinct(offres_emploi.c.code_naf))).where(
            offres_emploi.c.naf_version == naf_version)).scalar_one()
        tronquees = cx.execute(select(func.coalesce(func.sum(collectes_offres.c.fenetres_tronquees), 0)).where(
            collectes_offres.c.naf_version == naf_version)).scalar_one()
        derniere = cx.execute(select(func.max(collectes_offres.c.horodatage)).where(
            collectes_offres.c.naf_version == naf_version)).scalar_one()
    return {
        "naf_version": naf_version, "offres_total": total, "offres_creees_90_jours": derniers_90_jours,
        "offres_dans_les_departements_de_la_zone": en_zone, "offres_avec_salaire_lisible": avec_salaire,
        "codes_naf_avec_offres": codes, "fenetres_tronquees_cumulees": int(tronquees),
        "derniere_collecte": _aware(derniere).isoformat() if derniere else None,
    }


# ------------------------------------------ sous-étape V2.4 (étiquetage) -----

def _rang_stable(id_offre: str) -> str:
    """Clé de tirage STABLE (hachage de l'identifiant) : le même échantillon d'un jour à l'autre, sans biais de date."""
    import hashlib

    return hashlib.md5(id_offre.encode("utf-8")).hexdigest()


def offres_a_etiqueter(engine: Engine, naf_version: str, *, version: str, limite: int, departements_zone: tuple[str, ...],
                       avec_modele: bool, max_par_code: int | None = None, depuis: datetime | None = None,
                       exclure_codes: tuple[str, ...] = (), seulement_codes: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    """Offres à passer : jamais étiquetées, ou `lexique_seul`/`echec_modele` (reprise si `avec_modele`).

    Sans `max_par_code` : celles de la zone d'abord, puis les plus récentes. Avec `max_par_code` (échantillon) : au plus
    N offres par code NAF parmi celles créées depuis `depuis` (étiquetées ou en cours comprises), tirées par hachage
    stable ; les reprises (déjà dans l'échantillon) passent toujours en premier, puis les nouvelles à tour de rôle
    entre les codes, pour qu'une passe courte ne vide pas un seul secteur."""
    jamais = offres_etiquetage.c.id_offre.is_(None)
    a_reprendre = offres_etiquetage.c.statut.in_(("lexique_seul", "echec_modele"))
    filtre = (jamais | a_reprendre) if avec_modele else jamais
    if exclure_codes:  # secteurs exclus du référentiel : jamais de dépense d'étiquetage dessus
        filtre = filtre & (offres_emploi.c.code_naf.is_(None) | offres_emploi.c.code_naf.not_in(exclure_codes))
    if seulement_codes is not None:  # V2.8 : une tranche de priorité à la fois (cartographie initiale, régime quotidien)
        filtre = filtre & offres_emploi.c.code_naf.in_(seulement_codes)
    jointure = offres_emploi.outerjoin(offres_etiquetage, offres_emploi.c.id_offre == offres_etiquetage.c.id_offre)
    colonnes = (offres_emploi.c.id_offre, offres_emploi.c.intitule, offres_emploi.c.description, offres_emploi.c.code_naf,
                offres_etiquetage.c.statut.label("statut_precedent"))
    if max_par_code is None:
        en_zone = (offres_emploi.c.departement.in_(departements_zone)).desc()
        requete = (select(*colonnes).select_from(jointure).where(offres_emploi.c.naf_version == naf_version, filtre)
                   .order_by(en_zone, offres_emploi.c.date_creation.desc(), offres_emploi.c.id_offre).limit(limite))
        with engine.connect() as cx:
            return [dict(r) for r in cx.execute(requete).mappings()]

    fenetre = (offres_emploi.c.date_creation >= depuis) if depuis is not None else True
    with engine.connect() as cx:
        en_cours = cx.execute(
            select(offres_emploi.c.code_naf, func.count()).select_from(jointure)
            .where(offres_emploi.c.naf_version == naf_version, offres_etiquetage.c.id_offre.is_not(None), fenetre)
            .group_by(offres_emploi.c.code_naf)
        ).all()
        deja = {code: n for code, n in en_cours}
        candidats = cx.execute(
            select(offres_emploi.c.id_offre, offres_emploi.c.code_naf, offres_etiquetage.c.statut)
            .select_from(jointure).where(offres_emploi.c.naf_version == naf_version, filtre, fenetre)
        ).all()
    reprises = sorted((i for i, _c, st in candidats if st is not None), key=_rang_stable)
    nouvelles_par_code: dict[str, list[str]] = {}
    for i, code, st in candidats:
        if st is None:
            nouvelles_par_code.setdefault(code or "", []).append(i)
    tour: list[tuple[int, str, str]] = []
    for code, ids in nouvelles_par_code.items():
        place = max(0, max_par_code - deja.get(code, 0))
        for rang, i in enumerate(sorted(ids, key=_rang_stable)[:place]):
            tour.append((rang, code, i))
    choisies = (reprises + [i for _r, _c, i in sorted(tour)])[:limite]
    lignes: dict[str, dict[str, Any]] = {}
    with engine.connect() as cx:
        for k in range(0, len(choisies), 400):
            lot = choisies[k:k + 400]
            for r in cx.execute(select(*colonnes).select_from(jointure).where(offres_emploi.c.id_offre.in_(lot))).mappings():
                lignes[r["id_offre"]] = dict(r)
    return [lignes[i] for i in choisies if i in lignes]


def enregistrer_etiquetage(
    engine: Engine, *, id_offre: str, statut: str, version: str, modele: str | None, taches: list[dict[str, Any]],
    nb_citations_proposees: int, nb_citations_verifiees: int, maintenant: datetime | None = None,
) -> None:
    """Une seule transaction par offre : les lignes `offres_taches` (déjà présentes : ignorées, jamais dupliquées) et
    l'état `offres_etiquetage` (mis à jour si l'offre est reprise). Rien n'est jamais supprimé."""
    quand = maintenant or _now()
    upsert = sqlite_upsert if engine.dialect.name == "sqlite" else postgres_upsert
    nb_lexique = sum(1 for t in taches if t["provenance"] == "lexique")
    with engine.begin() as cx:
        for t in taches:
            stmt = upsert(offres_taches).values(
                id=_uid(), id_offre=id_offre, tache_id=t["tache_id"], provenance=t["provenance"], citation=t["citation"],
                modele=t.get("modele"), version=version, date_creation=quand,
            ).on_conflict_do_nothing(index_elements=["id_offre", "tache_id", "provenance"])
            cx.execute(stmt)
        valeurs = dict(statut=statut, version=version, modele=modele, nb_lexique=nb_lexique,
                       nb_citations_proposees=nb_citations_proposees, nb_citations_verifiees=nb_citations_verifiees,
                       etiquetee_le=quand)
        cx.execute(upsert(offres_etiquetage).values(id_offre=id_offre, **valeurs)
                   .on_conflict_do_update(index_elements=["id_offre"], set_=valeurs))


def cout_total_par_role(engine: Engine, role: str) -> float:
    """Dépense cumulée de TOUS les jours pour un rôle (enveloppe unique de la première cartographie)."""
    with engine.connect() as cx:
        return float(cx.execute(
            select(func.coalesce(func.sum(usage_events.c.cout_declare_ou_estime), 0.0)).where(usage_events.c.role == role)
        ).scalar_one())


def cout_total_par_roles(engine: Engine, roles: tuple[str, ...]) -> float:
    """Dépense cumulée de TOUS les jours pour plusieurs rôles (enveloppe unique partagée de la première cartographie)."""
    with engine.connect() as cx:
        return float(cx.execute(
            select(func.coalesce(func.sum(usage_events.c.cout_declare_ou_estime), 0.0)).where(usage_events.c.role.in_(roles))
        ).scalar_one())


def resume_etiquetage(engine: Engine, naf_version: str, *, role: str, jour: date | None = None) -> dict:
    """Lecture seule pour `app.metriques` (états CUMULÉS)."""
    with engine.connect() as cx:
        total = cx.execute(select(func.count()).select_from(offres_emploi).where(offres_emploi.c.naf_version == naf_version)).scalar_one()
        par_statut = dict(cx.execute(select(offres_etiquetage.c.statut, func.count()).group_by(offres_etiquetage.c.statut)).all())
        somme = cx.execute(select(
            func.coalesce(func.sum(offres_etiquetage.c.nb_citations_proposees), 0),
            func.coalesce(func.sum(offres_etiquetage.c.nb_citations_verifiees), 0),
            func.coalesce(func.sum(offres_etiquetage.c.nb_lexique), 0),
        ).where(offres_etiquetage.c.statut == "ok")).one()
        avec_tache = cx.execute(select(func.count(func.distinct(offres_taches.c.id_offre)))).scalar_one()
    proposees, verifiees, lexique = (int(somme[0]), int(somme[1]), int(somme[2]))
    cout_total = cout_total_par_role(engine, role)
    cout_jour = 0.0
    if jour is not None:
        debut, fin = _bornes_jour_utc(jour)
        with engine.connect() as cx:
            cout_jour = float(cx.execute(select(func.coalesce(func.sum(usage_events.c.cout_declare_ou_estime), 0.0)).where(
                usage_events.c.role == role, usage_events.c.date_creation >= debut, usage_events.c.date_creation < fin)).scalar_one())
    etiquetees_ok = int(par_statut.get("ok", 0))
    return {
        "offres_total": total, "offres_par_statut": {k: int(v) for k, v in par_statut.items()},
        "offres_non_etiquetees": total - sum(par_statut.values()),
        "offres_avec_au_moins_une_tache": int(avec_tache),
        "taux_citation_verifiee": round(verifiees / proposees, 4) if proposees else None,
        "citations_proposees": proposees, "citations_verifiees": verifiees, "taches_lexique": lexique,
        "cout_etiquetage_total_eur": round(cout_total, 4), "cout_etiquetage_jour_eur": round(cout_jour, 4),
        "cout_moyen_par_offre_eur": round(cout_total / etiquetees_ok, 5) if etiquetees_ok else None,
    }


# ------------------------------------------ sous-étape V2.4 (agrégation) -----

def dernier_agregat_par_couple(engine: Engine, naf_version: str) -> dict[tuple[str, str], dict]:
    with engine.connect() as cx:
        lignes = cx.execute(
            select(demande_secteur_tache).where(demande_secteur_tache.c.naf_version == naf_version)
            .order_by(demande_secteur_tache.c.calcule_le.asc())
        ).mappings().all()
    derniers: dict[tuple[str, str], dict] = {}
    for ligne in lignes:
        derniers[(ligne["code_naf"], ligne["tache_id"])] = dict(ligne)
    return derniers


def enregistrer_agregats(engine: Engine, lignes: list[dict[str, Any]]) -> None:
    if not lignes:
        return
    with engine.begin() as cx:
        for ligne in lignes:
            cx.execute(insert(demande_secteur_tache).values(id=_uid(), **ligne))


# ------------------------------------------ sous-étape V2.5 (fiches) -----

def derniers_agregats(engine: Engine, naf_version: str) -> list[dict]:
    """Dernière ligne `demande_secteur_tache` de chaque couple (code, tâche)."""
    return list(dernier_agregat_par_couple(engine, naf_version).values())


def etablissements_dans_le_rayon_par_code(engine: Engine, naf_version: str, *, rayon_km: float) -> dict[str, int]:
    """Nombre d'établissements de l'échantillon de prospection à moins de `rayon_km` du centre, par code NAF."""
    with engine.connect() as cx:
        lignes = cx.execute(
            select(prospection.c.code_naf, func.count()).where(
                prospection.c.naf_version == naf_version, prospection.c.distance_centre_km <= rayon_km
            ).group_by(prospection.c.code_naf)
        ).all()
    return {code: int(n) for code, n in lignes}


def entreprises_zone_par_code(engine: Engine, naf_version: str, departements: tuple[str, ...]) -> dict[str, dict]:
    """Somme, par code NAF, des dernières mesures `nb_entreprises_actives` des départements de la zone ; `plafonne` si
    l'une des mesures est une borne basse (plafond de 10 000 de l'API)."""
    resultat: dict[str, dict] = {}
    for (code, dep), ligne in derniers_comptages_etablissements(engine, naf_version).items():
        if dep not in departements:
            continue
        r = resultat.setdefault(code, {"entreprises": 0, "plafonne": False})
        r["entreprises"] += int(ligne["nb_entreprises_actives"])
        r["plafonne"] = r["plafonne"] or bool(ligne["comptage_plafonne"])
    return resultat


def enregistrer_fiche(engine: Engine, valeurs: dict[str, Any]) -> str:
    f_id = _uid()
    with engine.begin() as cx:
        cx.execute(insert(fiches_secteur_tache).values(id=f_id, **valeurs))
    return f_id


def dernieres_fiches(engine: Engine, naf_version: str) -> dict[tuple[str, str], dict]:
    with engine.connect() as cx:
        lignes = cx.execute(
            select(fiches_secteur_tache).where(fiches_secteur_tache.c.naf_version == naf_version)
            .order_by(fiches_secteur_tache.c.calcule_le.asc())
        ).mappings().all()
    derniers: dict[tuple[str, str], dict] = {}
    for ligne in lignes:
        derniers[(ligne["code_naf"], ligne["tache_id"])] = dict(ligne)
    return derniers


def resume_fiches(engine: Engine, naf_version: str, *, role_analyste: str, role_critic: str) -> dict:
    """Lecture seule pour `app.metriques` (états CUMULÉS)."""
    derniers = dernieres_fiches(engine, naf_version)
    par_decision: dict[str, int] = {}
    for f in derniers.values():
        par_decision[f["decision"]] = par_decision.get(f["decision"], 0) + 1
    scores = sorted(f["score_prudent"] for f in derniers.values())
    return {
        "fiches": len(derniers), "par_decision": par_decision,
        "score_prudent_median": scores[len(scores) // 2] if scores else None,
        "score_prudent_max": scores[-1] if scores else None,
        "cout_analyste_eur": round(cout_total_par_role(engine, role_analyste), 4),
        "cout_critic_eur": round(cout_total_par_role(engine, role_critic), 4),
    }


# ------------------------------------------------------------- concurrence (V2.6) -----

def enregistrer_concurrence(engine: Engine, valeurs: dict[str, Any]) -> str:
    c_id = _uid()
    with engine.begin() as cx:
        cx.execute(insert(concurrence_secteur_tache).values(id=c_id, **valeurs))
    return c_id


def dernieres_concurrences(engine: Engine, naf_version: str, *, seulement_evaluees: bool = True) -> dict[tuple[str, str], dict]:
    """Ligne la plus récente par (code, tâche). Par défaut parmi les lignes ÉVALUÉES seulement : un échec ultérieur n'efface rien."""
    requete = select(concurrence_secteur_tache).where(concurrence_secteur_tache.c.naf_version == naf_version)
    if seulement_evaluees:
        requete = requete.where(concurrence_secteur_tache.c.statut == "evalue")
    with engine.connect() as cx:
        lignes = cx.execute(requete.order_by(concurrence_secteur_tache.c.evalue_le.asc())).mappings().all()
    derniers: dict[tuple[str, str], dict] = {}
    for ligne in lignes:
        derniers[(ligne["code_naf"], ligne["tache_id"])] = dict(ligne)
    return derniers


def reserver_recherche_web(engine: Engine, *, fournisseur: str, mois: str, requete: str, code_naf: str | None, tache_id: str | None,
                           maximum: int, maintenant: datetime) -> str | None:
    """Plafond mensuel STRICT : compte et réserve dans la même transaction. `None` si le plafond du mois est atteint (rien n'est écrit)."""
    r_id = _uid()
    with engine.begin() as cx:
        deja = cx.execute(select(func.count()).select_from(recherches_web).where(recherches_web.c.mois == mois)).scalar_one()
        if deja >= maximum:
            return None
        cx.execute(insert(recherches_web).values(id=r_id, fournisseur=fournisseur, mois=mois, requete=requete, code_naf=code_naf,
                                                 tache_id=tache_id, nb_resultats=None, date_creation=maintenant))
    return r_id


def clore_recherche_web(engine: Engine, recherche_id: str, nb_resultats: int) -> None:
    with engine.begin() as cx:
        cx.execute(update(recherches_web).where(recherches_web.c.id == recherche_id).values(nb_resultats=nb_resultats))


def nombre_recherches_web(engine: Engine, mois: str) -> int:
    with engine.connect() as cx:
        return int(cx.execute(select(func.count()).select_from(recherches_web).where(recherches_web.c.mois == mois)).scalar_one())
