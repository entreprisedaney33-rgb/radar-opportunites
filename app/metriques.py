"""Métriques de pilotage — lecture seule sur la base, AUCUN appel modèle.

    python -m app.metriques --jour AAAA-MM-JJ
    python -m app.metriques --jour AAAA-MM-JJ --comparer AAAA-MM-JJ

Affiche le résultat et l'écrit dans `rapports/metriques/<jour>.json`. Le
"jour" est une journée UTC (mêmes bornes que le run journalier du worker,
voir app/pipeline/orchestrator.py). Une opportunité compte pour le jour où
elle a été REPÉRÉE (`opportunities.date_creation`), même si elle n'a été
notée qu'un jour plus tard — "analysée" est calculé séparément (présence
d'un score), voir plus bas.

Avec `--comparer`, les métriques du jour comparé sont calculées et
écrites de la même façon, puis les deux jours sont affichés côte à côte
(pour vérifier ce qui a réellement bougé après un changement).

`cout_jour_eur` est la somme des coûts journalisés au tarif en vigueur au
moment de CHAQUE appel (voir app/adapters/model_client.py) ; à côté,
`cout_jour_recalcule_tarifs_courants` recalcule le même jour à partir des
tokens réellement stockés (`usage_events.tokens_in`/`tokens_out`) multipliés
par les tarifs COURANTS de `config/tarifs.yaml` — pour rester comparable
d'un jour à l'autre malgré une correction de tarif (sous-étape 3.6,
préalable, AMELIORATIONS.md).

Connexion à la base : lit `RADAR_DATABASE_URL` dans l'environnement, sinon
le fichier `~/.config/radar-opportunites/env` écrit par
`scripts/creer_acces_lecture.py` (sous-étape 0.5) — jamais `DATABASE_URL`
(celle du reste de l'application, avec des droits d'écriture) et jamais
d'autre repli. Si ni l'un ni l'autre n'existe, la commande s'arrête
proprement avec un message clair, sans jamais se connecter à une autre base.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine

from app import config as cfg
from app.adapters.model_client import (
    ISSUE_NORMALISEE,
    ISSUE_PERDUE,
    ISSUE_RELANCEE,
    ISSUE_VALIDE,
    estimer_cout_eur,
)
from app.enqueteur.fetch import ETIQUETTE_PREUVE_ENQUETE, ETIQUETTE_PREUVE_PRIX
from app.storage.schema import journal_http, opportunities, opportunity_evidence, scores, sources, usage_events

RACINE = Path(__file__).resolve().parent.parent
DOSSIER_RAPPORTS = RACINE / "rapports" / "metriques"

# Même chemin que FICHIER_ENV dans scripts/creer_acces_lecture.py (à tenir
# synchronisé) : le fichier que ce script écrit, hors de tout dépôt.
FICHIER_ENV_LECTURE_SEULE = Path.home() / ".config" / "radar-opportunites" / "env"


class ErreurConfigMetriques(Exception):
    """Configuration manquante ou invalide pour se connecter en lecture
    seule — jamais de repli silencieux vers une autre variable ou une base
    par défaut (voir sous-étapes 0.4/0.5)."""


def _lire_url_depuis_fichier_env() -> str | None:
    """Repli si RADAR_DATABASE_URL n'est pas dans l'environnement. Ne
    journalise ni n'affiche jamais le contenu lu."""
    if not FICHIER_ENV_LECTURE_SEULE.exists():
        return None
    for ligne in FICHIER_ENV_LECTURE_SEULE.read_text(encoding="utf-8").splitlines():
        if ligne.startswith("RADAR_DATABASE_URL="):
            return ligne.split("=", 1)[1].strip()
    return None


def _engine_lecture_seule() -> Engine:
    url = os.environ.get("RADAR_DATABASE_URL") or _lire_url_depuis_fichier_env()
    if not url:
        raise ErreurConfigMetriques(
            "Variable d'environnement RADAR_DATABASE_URL absente, et aucun fichier "
            f"{FICHIER_ENV_LECTURE_SEULE} trouvé. app.metriques ne se connecte qu'à "
            "cette source dédiée en lecture seule (jamais à DATABASE_URL, qui a les "
            "droits d'écriture du reste de l'application, et sans aucun autre "
            "repli). Voir les sous-étapes 0.4/0.5 d'AMELIORATIONS.md."
        )
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=connect_args, future=True)


def _bornes_jour_utc(jour: date) -> tuple[datetime, datetime]:
    debut = datetime(jour.year, jour.month, jour.day, tzinfo=timezone.utc)
    return debut, debut + timedelta(days=1)


def _percentile(valeurs: list[float], p: float) -> float | None:
    """Percentile par rang le plus proche (arrondi au-dessus à 0,5 pile),
    sans interpolation ni dépendance supplémentaire — reproductible, mais
    tombe toujours sur une vraie valeur observée, jamais une moyenne entre
    deux. `p` entre 0 et 1."""
    if not valeurs:
        return None
    trie = sorted(valeurs)
    rang = min(len(trie) - 1, int(p * (len(trie) - 1) + 0.5))
    return trie[rang]


def calculer_metriques(engine: Engine, jour: date) -> dict:
    """Fonction pure côté lecture : ne modifie jamais la base, ne fait
    jamais d'appel réseau. Testée sur fixtures dans tests/test_metriques.py."""
    debut, fin = _bornes_jour_utc(jour)

    with engine.connect() as cx:
        opps = cx.execute(
            select(opportunities).where(
                opportunities.c.date_creation >= debut, opportunities.c.date_creation < fin
            )
        ).mappings().all()
        opp_ids = [o["id"] for o in opps]

        # `scores` est append-only (voir SCORING.md, "Historique, jamais
        # écrasé") : on garde le DERNIER score de chaque opportunité en
        # itérant dans l'ordre croissant, jamais un score intermédiaire.
        scores_par_opp: dict[str, dict] = {}
        if opp_ids:
            lignes_scores = cx.execute(
                select(scores).where(scores.c.opportunity_id.in_(opp_ids)).order_by(scores.c.date_creation)
            ).mappings().all()
            for ligne in lignes_scores:
                scores_par_opp[ligne["opportunity_id"]] = dict(ligne)

        preuves_par_opp: dict[str, set] = {}
        if opp_ids:
            lignes_preuves = cx.execute(
                select(opportunity_evidence.c.opportunity_id, opportunity_evidence.c.source_id)
                .where(opportunity_evidence.c.opportunity_id.in_(opp_ids))
            ).all()
            for opp_id, source_id in lignes_preuves:
                preuves_par_opp.setdefault(opp_id, set()).add(source_id)

        lignes_couts = cx.execute(
            select(
                usage_events.c.role, usage_events.c.opportunity_id, usage_events.c.cout_declare_ou_estime,
                usage_events.c.fournisseur, usage_events.c.modele_ou_actor,
                usage_events.c.tokens_in, usage_events.c.tokens_out,
                usage_events.c.issue, usage_events.c.sortie_tronquee,
            ).where(
                usage_events.c.date_creation >= debut, usage_events.c.date_creation < fin
            )
        ).all()

        # Sous-étape 1.4, point 2 : d'où viennent les opportunités du jour —
        # jointure sur la preuve d'ORIGINE posée par le Scout à la création
        # (claim "Scout: ...", app/pipeline/orchestrator.py::_phase_collecte_et_scout).
        # Une opportunité fusionnée (dedup, app/pipeline/dedupe.py) peut
        # porter plusieurs de ces preuves d'origine (une par signal fusionné) :
        # chacune compte ici, donc une opportunité peut apparaître dans
        # plusieurs flux/expressions à la fois — lecture "d'où viennent les
        # signaux repérés", pas une partition stricte des opportunités.
        lignes_origine = []
        if opp_ids:
            lignes_origine = cx.execute(
                select(
                    sources.c.flux_origine, sources.c.requete_origine, sources.c.etiquette,
                )
                .select_from(opportunity_evidence.join(sources, opportunity_evidence.c.source_id == sources.c.id))
                .where(
                    opportunity_evidence.c.opportunity_id.in_(opp_ids),
                    opportunity_evidence.c.claim.like("Scout: %"),
                )
            ).all()

        # Sous-étape 3.7, point 2 : appels HTTP réels du jour (collecte RSS,
        # recherche Reddit/HN, recherche et fetch de page de l'Enquêteur),
        # journalisés par `app/adapters/http.py` dans `journal_http`
        # (jamais le contenu de la page ni l'URL complète) -- agrégés plus
        # bas par flux/fournisseur.
        lignes_http = cx.execute(
            select(journal_http.c.flux_ou_fournisseur, journal_http.c.code_http, journal_http.c.erreur).where(
                journal_http.c.horodatage >= debut, journal_http.c.horodatage < fin
            )
        ).all()

        nb_signaux_concurrence = cx.execute(
            select(func.count()).select_from(sources).where(
                sources.c.etiquette == "signal_concurrence",
                sources.c.date_collecte >= debut, sources.c.date_collecte < fin,
            )
        ).scalar_one()

        # Sous-étape 3.4, point 5 : sources par dossier (déjà prévu en 0.2)
        # ventilées par fournisseur de l'Enquêteur (algolia_hn|reddit|
        # magasin_interne|fetch_direct_pricing) -- une ligne
        # `opportunity_evidence` par preuve d'enquête rattachée à une
        # opportunité repérée CE jour (même périmètre que `preuves_par_opp`
        # ci-dessus, pas limité aux preuves elles-mêmes collectées ce
        # jour-là). Sous-étape 3.4b : `ETIQUETTE_PREUVE_PRIX` incluse ici en
        # plus de `ETIQUETTE_PREUVE_ENQUETE` -- une preuve de la famille
        # `prix` reste une preuve d'enquête au sens de cette ventilation,
        # seulement étiquetée différemment pour la distinguer côté stockage.
        lignes_preuves_enquete = []
        if opp_ids:
            lignes_preuves_enquete = cx.execute(
                select(sources.c.flux_origine)
                .select_from(opportunity_evidence.join(sources, opportunity_evidence.c.source_id == sources.c.id))
                .where(
                    opportunity_evidence.c.opportunity_id.in_(opp_ids),
                    sources.c.etiquette.in_([ETIQUETTE_PREUVE_ENQUETE, ETIQUETTE_PREUVE_PRIX]),
                )
            ).all()

    nb_reperees = len(opps)
    nb_analysees = len(scores_par_opp)
    cout_jour = round(sum(c for (_, _, c, _, _, _, _, _, _) in lignes_couts), 4)

    # Traçabilité par rôle et par opportunité (colonnes ajoutées en
    # sous-étape 0.7, NULL pour tout l'historique antérieur — regroupé sous
    # "sans_role" plutôt qu'ignoré, pour que le total reste vérifiable).
    cout_par_role: dict[str, float] = {}
    cout_par_opportunite: dict[str, float] = {}
    for role, opp_id, cout, _, _, _, _, _, _ in lignes_couts:
        cle_role = role or "sans_role"
        cout_par_role[cle_role] = round(cout_par_role.get(cle_role, 0.0) + cout, 4)
        if opp_id:
            cout_par_opportunite[opp_id] = cout_par_opportunite.get(opp_id, 0.0) + cout

    # Sous-étape 3.6 (préalable, AMELIORATIONS.md) : recalcul du coût du jour
    # à partir des tokens réellement stockés (usage_events.tokens_in/tokens_out)
    # multipliés par les tarifs COURANTS de config/tarifs.yaml — contrairement
    # à `cout_jour` ci-dessus (figé au tarif en vigueur au moment de chaque
    # appel, voir app/adapters/model_client.py::estimer_cout_eur). Objectif :
    # rendre comparable un jour d'avant une correction de tarif (ex. 25/09,
    # avant la correction Sonnet 5 3$/15$→2$/10$ de la sous-étape 0.7) et un
    # jour d'après. Portée : uniquement les appels modèle réels
    # (fournisseur="anthropic", scout|analyst|critic) — les compteurs de
    # l'Enquêteur (role enqueteur_recherche|enqueteur_fetch) ne coûtent
    # jamais rien et n'ont jamais de tokens (voir app/pipeline/budget.py).
    # Une ligne "anthropic" sans tokens stockés (appel réseau qui a échoué,
    # voir model_client.py) ne peut pas être recalculée : elle est comptée à
    # part plutôt qu'ignorée silencieusement, pour que la couverture du
    # recalcul reste vérifiable.
    cout_jour_recalcule = 0.0
    nb_evenements_anthropic_couverts = 0
    nb_evenements_anthropic_sans_tokens = 0
    for _, _, _, fournisseur, modele, tokens_in, tokens_out, _, _ in lignes_couts:
        if fournisseur != "anthropic":
            continue
        if tokens_in is None or tokens_out is None:
            nb_evenements_anthropic_sans_tokens += 1
            continue
        cout_jour_recalcule += estimer_cout_eur(modele, tokens_in, tokens_out)
        nb_evenements_anthropic_couverts += 1

    # Sous-étape 3.10, point 4 : fiabilité des sorties structurées par rôle
    # (scout|analyst|critic) -- `issue`/`sortie_tronquee` (usage_events,
    # migration additive) : NULL pour tout appel antérieur à cette sous-étape,
    # compté à part ("sans_donnee") plutôt qu'ignoré, pour que le taux reste
    # vérifiable. "valide"/"normalisee" comptent comme une sortie EXPLOITÉE ;
    # "relancee" (la tentative a déclenché une relance) et "perdue" (dernière
    # tentative invalide) comptent comme NON exploitée -- `cout_appels_perdus_eur`
    # ne somme que "perdue" (au sens strict du point 1 : le coût de la
    # tentative qui n'a produit AUCUN résultat final, ni directement ni via
    # une relance), `cout_non_exploite_eur` inclut en plus "relancee" (le
    # coût de la PREMIÈRE tentative d'une paire, elle aussi jamais utilisée).
    ISSUES_EXPLOITEES = {ISSUE_VALIDE, ISSUE_NORMALISEE}
    ISSUES_NON_EXPLOITEES = {ISSUE_RELANCEE, ISSUE_PERDUE}
    fiabilite_sorties: dict[str, dict] = {}
    for role, _opp_id, cout, _fournisseur, _modele, _tin, _tout, issue, sortie_tronquee in lignes_couts:
        if role not in ("scout", "analyst", "critic"):
            continue
        stats = fiabilite_sorties.setdefault(
            role, {
                "appels": 0, "valides": 0, "normalisees": 0, "relancees": 0, "perdues": 0,
                "tronquees": 0, "sans_donnee_fiabilite": 0,
                "cout_appels_perdus_eur": 0.0, "cout_non_exploite_eur": 0.0,
            },
        )
        stats["appels"] += 1
        if sortie_tronquee:
            stats["tronquees"] += 1
        if issue is None:
            stats["sans_donnee_fiabilite"] += 1
        elif issue == ISSUE_VALIDE:
            stats["valides"] += 1
        elif issue == ISSUE_NORMALISEE:
            stats["normalisees"] += 1
        elif issue == ISSUE_RELANCEE:
            stats["relancees"] += 1
            stats["cout_non_exploite_eur"] += cout
        elif issue == ISSUE_PERDUE:
            stats["perdues"] += 1
            stats["cout_appels_perdus_eur"] += cout
            stats["cout_non_exploite_eur"] += cout
    for stats in fiabilite_sorties.values():
        denominateur = stats["appels"] - stats["sans_donnee_fiabilite"]
        stats["taux_sorties_valides"] = (
            round((stats["valides"] + stats["normalisees"]) / denominateur, 4) if denominateur else None
        )
        stats["cout_appels_perdus_eur"] = round(stats["cout_appels_perdus_eur"], 4)
        stats["cout_non_exploite_eur"] = round(stats["cout_non_exploite_eur"], 4)

    par_statut: dict[str, int] = {}
    for o in opps:
        par_statut[o["statut"]] = par_statut.get(o["statut"], 0) + 1

    par_decision: dict[str, int] = {}
    for s in scores_par_opp.values():
        cle = s["decision_critic"] or "aucune"
        par_decision[cle] = par_decision.get(cle, 0) + 1

    scores_prudents = [s["score_prudent"] for s in scores_par_opp.values()]
    nb_hors_intersectoriel = sum(1 for o in opps if o["secteur"] != "intersectoriel")
    nb_sources = [len(preuves_par_opp.get(o["id"], set())) for o in opps]

    par_secteur: dict[str, int] = {}
    for o in opps:
        par_secteur[o["secteur"]] = par_secteur.get(o["secteur"], 0) + 1

    # Sous-étape 2.2, point 3 : répartition par provenance du secteur
    # (citation_verifiee|flux|defaut, posée en 2.1) -- NULL pour toute
    # opportunité créée avant la migration additive de 2.1, regroupée sous
    # "aucune" plutôt qu'ignorée.
    par_secteur_provenance: dict[str, int] = {}
    for o in opps:
        cle = o["secteur_provenance"] or "aucune"
        par_secteur_provenance[cle] = par_secteur_provenance.get(cle, 0) + 1

    # Sous-étape 1.4, point 2 : type de flux (douleur/offre — dérivé de
    # `etiquette`, seul endroit où cette distinction est réellement stockée,
    # voir app/storage/schema.py::sources ; un flux `offre` ne produisant
    # jamais de signal, ce classement reste par construction 100 % `douleur`
    # tant qu'aucune fuite n'existe ailleurs dans le pipeline — utile comme
    # garde-fou de cohérence, pas seulement comme mesure), par flux nommé, et
    # les 10 expressions du lexique de douleur les plus productives.
    par_type_flux: dict[str, int] = {}
    par_flux: dict[str, int] = {}
    par_expression: dict[str, int] = {}
    for flux_origine, requete_origine, etiquette in lignes_origine:
        type_flux = "offre" if etiquette == "signal_concurrence" else "douleur"
        par_type_flux[type_flux] = par_type_flux.get(type_flux, 0) + 1
        cle_flux = flux_origine or "inconnu"
        par_flux[cle_flux] = par_flux.get(cle_flux, 0) + 1
        if requete_origine:
            par_expression[requete_origine] = par_expression.get(requete_origine, 0) + 1
    top_10_expressions = dict(sorted(par_expression.items(), key=lambda kv: kv[1], reverse=True)[:10])

    par_fournisseur_enquete: dict[str, int] = {}
    for (flux_origine,) in lignes_preuves_enquete:
        cle = flux_origine or "inconnu"
        par_fournisseur_enquete[cle] = par_fournisseur_enquete.get(cle, 0) + 1

    # Sous-étape 3.1 : compteurs journaliers de l'Enquêteur, branchés dans le
    # pipeline réel depuis la sous-étape 3.4
    # (app/pipeline/orchestrator.py::_phase_enquete). Dérivés de
    # `lignes_couts` (déjà interrogé ci-dessus,
    # role="enqueteur_recherche"/"enqueteur_fetch", coût toujours 0 en V1).
    quotas_config = cfg.quotas()
    nb_requetes_recherche = sum(1 for role, *_ in lignes_couts if role == "enqueteur_recherche")
    nb_fetchs_pages = sum(1 for role, *_ in lignes_couts if role == "enqueteur_fetch")

    # Sous-étape 3.7, point 2 : par flux/fournisseur -- nombre d'appels, 429,
    # 403, autres erreurs, taux de succès (code HTTP 2xx/3xx, sans erreur).
    # Un flux/fournisseur absent de `journal_http` ce jour-là (aucun appel
    # journalisé) n'apparaît simplement pas dans ce dict, plutôt qu'à 0
    # partout -- distinction utile : "jamais appelé" n'est pas "toujours en
    # échec".
    appels_http_par_flux: dict[str, dict] = {}
    for flux, code, erreur in lignes_http:
        cle = flux or "inconnu"
        stats = appels_http_par_flux.setdefault(
            cle, {"appels": 0, "http_429": 0, "http_403": 0, "autres_erreurs": 0, "succes": 0}
        )
        stats["appels"] += 1
        if erreur is None and code is not None and 200 <= code < 400:
            stats["succes"] += 1
        elif code == 429:
            stats["http_429"] += 1
        elif code == 403:
            stats["http_403"] += 1
        else:
            stats["autres_erreurs"] += 1
    for stats in appels_http_par_flux.values():
        stats["taux_succes"] = round(stats["succes"] / stats["appels"], 4)

    return {
        "jour": jour.isoformat(),
        "opportunites_reperees": nb_reperees,
        "opportunites_analysees": nb_analysees,
        "par_statut": par_statut,
        "par_decision_critic": par_decision,
        "score_prudent": {
            "min": min(scores_prudents) if scores_prudents else None,
            "mediane": _percentile(scores_prudents, 0.5),
            "p90": _percentile(scores_prudents, 0.9),
            "max": max(scores_prudents) if scores_prudents else None,
            "nb_superieur_60": sum(1 for v in scores_prudents if v > 60),
            "nb_superieur_80": sum(1 for v in scores_prudents if v > 80),
        },
        "par_secteur": par_secteur,
        "par_secteur_provenance": par_secteur_provenance,
        "par_type_flux": par_type_flux,
        "par_flux": par_flux,
        "top_10_expressions_lexique": top_10_expressions,
        "signaux_concurrence_stockes": nb_signaux_concurrence,
        "enqueteur": {
            "requetes_recherche_jour": nb_requetes_recherche,
            "plafond_requetes_recherche_par_jour": quotas_config["max_requetes_recherche_par_jour"],
            "fetchs_pages_jour": nb_fetchs_pages,
            "plafond_fetchs_pages_par_jour": quotas_config["max_fetchs_pages_par_jour"],
        },
        "appels_http_par_flux": appels_http_par_flux,
        "part_hors_intersectoriel": round(nb_hors_intersectoriel / nb_reperees, 4) if nb_reperees else None,
        "sources_par_dossier": {
            "min": min(nb_sources) if nb_sources else None,
            "mediane": _percentile([float(n) for n in nb_sources], 0.5),
            "max": max(nb_sources) if nb_sources else None,
            "part_une_seule_source": round(sum(1 for n in nb_sources if n == 1) / nb_reperees, 4) if nb_reperees else None,
            "par_fournisseur": par_fournisseur_enquete,  # sous-étape 3.4 : preuves de l'Enquêteur uniquement
        },
        # Le type d'objection n'existe pas encore dans le modèle de données
        # (Objection = texte + source_ids seulement, voir app/models_schemas.py)
        # -- champ laissé vide plutôt qu'inventé, en attendant l'étape 5 du
        # brief pour Fable 5 (BRIEF-FABLE5-AMELIORER-RADAR.md).
        "objections_critic_par_type": None,
        "cout_jour_eur": cout_jour,
        "cout_jour_recalcule_tarifs_courants": {
            "eur": round(cout_jour_recalcule, 4),
            "ecart_vs_enregistre_eur": round(cout_jour_recalcule - cout_jour, 4),
            "evenements_anthropic_couverts": nb_evenements_anthropic_couverts,
            "evenements_anthropic_sans_tokens": nb_evenements_anthropic_sans_tokens,
        },
        "cout_moyen_par_dossier_analyse_eur": round(cout_jour / nb_analysees, 4) if nb_analysees else None,
        "cout_par_role_eur": cout_par_role,
        "fiabilite_sorties": fiabilite_sorties,
        "cout_moyen_par_opportunite_eur": (
            round(sum(cout_par_opportunite.values()) / len(cout_par_opportunite), 4)
            if cout_par_opportunite else None
        ),
    }


def _parser_jour(texte: str) -> date:
    return datetime.strptime(texte, "%Y-%m-%d").date()


def _ecrire_json(resultat: dict, jour: date) -> Path:
    DOSSIER_RAPPORTS.mkdir(parents=True, exist_ok=True)
    chemin = DOSSIER_RAPPORTS / f"{jour.isoformat()}.json"
    with chemin.open("w", encoding="utf-8") as f:
        json.dump(resultat, f, ensure_ascii=False, indent=2)
    return chemin


# Indicateurs clés affichés côte à côte par --comparer, dans l'ordre de la
# liste de la sous-étape 0.2. Chaque valeur est lue dans le dict déjà produit
# par `calculer_metriques`, jamais recalculée.
_LIGNES_COMPARAISON: list[tuple[str, "callable"]] = [
    ("Opportunités repérées", lambda m: m["opportunites_reperees"]),
    ("Opportunités analysées", lambda m: m["opportunites_analysees"]),
    ("Score prudent — médiane", lambda m: m["score_prudent"]["mediane"]),
    ("Score prudent — p90", lambda m: m["score_prudent"]["p90"]),
    ("Score prudent — max", lambda m: m["score_prudent"]["max"]),
    ("Dossiers > 60", lambda m: m["score_prudent"]["nb_superieur_60"]),
    ("Dossiers > 80", lambda m: m["score_prudent"]["nb_superieur_80"]),
    ("Part hors intersectoriel", lambda m: m["part_hors_intersectoriel"]),
    ("Sources / dossier — médiane", lambda m: m["sources_par_dossier"]["mediane"]),
    ("Part à une seule source", lambda m: m["sources_par_dossier"]["part_une_seule_source"]),
    ("Coût du jour (€)", lambda m: m["cout_jour_eur"]),
    ("Coût du jour, tarifs courants (€)", lambda m: m["cout_jour_recalcule_tarifs_courants"]["eur"]),
    ("Coût moyen / dossier analysé (€)", lambda m: m["cout_moyen_par_dossier_analyse_eur"]),
    ("Coût moyen / opportunité (€)", lambda m: m["cout_moyen_par_opportunite_eur"]),
]


def formater_comparaison(jour_a: date, m_a: dict, jour_b: date, m_b: dict) -> str:
    """Deux jours de métriques déjà calculées, affichés côte à côte en
    texte. Ne recalcule rien, ne touche pas à la base."""
    largeur = max(len(libelle) for libelle, _ in _LIGNES_COMPARAISON)
    entete = f"{'Indicateur':<{largeur}}  {jour_a.isoformat():>14}  {jour_b.isoformat():>14}"
    lignes = [entete, "-" * len(entete)]
    for libelle, extraire in _LIGNES_COMPARAISON:
        lignes.append(f"{libelle:<{largeur}}  {str(extraire(m_a)):>14}  {str(extraire(m_b)):>14}")
    return "\n".join(lignes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.metriques")
    parser.add_argument("--jour", required=True, help="Jour UTC au format AAAA-MM-JJ")
    parser.add_argument("--comparer", help="Jour UTC à comparer, même format, affiché côte à côte")
    args = parser.parse_args(argv)

    try:
        jour = _parser_jour(args.jour)
    except ValueError:
        print(f"Format de date invalide : {args.jour!r} (attendu AAAA-MM-JJ)", file=sys.stderr)
        return 1

    jour_compare = None
    if args.comparer:
        try:
            jour_compare = _parser_jour(args.comparer)
        except ValueError:
            print(f"Format de date invalide pour --comparer : {args.comparer!r} (attendu AAAA-MM-JJ)", file=sys.stderr)
            return 1

    try:
        engine = _engine_lecture_seule()
    except ErreurConfigMetriques as exc:
        print(str(exc), file=sys.stderr)
        return 1

    resultat = calculer_metriques(engine, jour)
    chemin = _ecrire_json(resultat, jour)
    print(json.dumps(resultat, ensure_ascii=False, indent=2))
    print(f"\nÉcrit dans {chemin}")

    if jour_compare is not None:
        resultat_compare = calculer_metriques(engine, jour_compare)
        chemin_compare = _ecrire_json(resultat_compare, jour_compare)
        print(json.dumps(resultat_compare, ensure_ascii=False, indent=2))
        print(f"\nÉcrit dans {chemin_compare}")
        print()
        print(formater_comparaison(jour, resultat, jour_compare, resultat_compare))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
