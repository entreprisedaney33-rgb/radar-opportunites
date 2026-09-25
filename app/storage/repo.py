"""Accès aux données : chaque fonction est une unité de travail (transaction
courte). Rien n'écrase silencieusement : `scores` est append-only, `runs`
n'est mis à jour que sur son propre id (idempotence d'une reprise : voir
`get_run` avant de recréer).
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Engine

from sqlalchemy.dialects.sqlite import insert as sqlite_upsert
from sqlalchemy.dialects.postgresql import insert as postgres_upsert

from app.storage.schema import (
    assessments,
    controles,
    decisions,
    etats_flux_recherche,
    journal_http,
    opportunities,
    opportunity_evidence,
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
    temps pour la même journée."""
    with engine.connect() as cx:
        row = cx.execute(
            select(runs).where(runs.c.statut == "en_cours").order_by(runs.c.debut.desc()).limit(1)
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
                       secteur_provenance: str | None = None, secteur_citation: str | None = None) -> str:
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
                   decision_critic: str | None) -> str:
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
                         opportunity_id: str | None = None, devise: str = "EUR") -> str:
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
                usage_events.c.role.in_(("analyst", "critic")),
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


# ------------------------------------------------ tirages de contrôle ----

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
