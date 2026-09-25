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

from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine

from app.storage.schema import opportunities, opportunity_evidence, scores, usage_events

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
            ).where(
                usage_events.c.date_creation >= debut, usage_events.c.date_creation < fin
            )
        ).all()

    nb_reperees = len(opps)
    nb_analysees = len(scores_par_opp)
    cout_jour = round(sum(c for (_, _, c) in lignes_couts), 4)

    # Traçabilité par rôle et par opportunité (colonnes ajoutées en
    # sous-étape 0.7, NULL pour tout l'historique antérieur — regroupé sous
    # "sans_role" plutôt qu'ignoré, pour que le total reste vérifiable).
    cout_par_role: dict[str, float] = {}
    cout_par_opportunite: dict[str, float] = {}
    for role, opp_id, cout in lignes_couts:
        cle_role = role or "sans_role"
        cout_par_role[cle_role] = round(cout_par_role.get(cle_role, 0.0) + cout, 4)
        if opp_id:
            cout_par_opportunite[opp_id] = cout_par_opportunite.get(opp_id, 0.0) + cout

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
        "part_hors_intersectoriel": round(nb_hors_intersectoriel / nb_reperees, 4) if nb_reperees else None,
        "sources_par_dossier": {
            "min": min(nb_sources) if nb_sources else None,
            "mediane": _percentile([float(n) for n in nb_sources], 0.5),
            "max": max(nb_sources) if nb_sources else None,
            "part_une_seule_source": round(sum(1 for n in nb_sources if n == 1) / nb_reperees, 4) if nb_reperees else None,
        },
        # Le type d'objection n'existe pas encore dans le modèle de données
        # (Objection = texte + source_ids seulement, voir app/models_schemas.py)
        # -- champ laissé vide plutôt qu'inventé, en attendant l'étape 5 du
        # brief pour Fable 5 (BRIEF-FABLE5-AMELIORER-RADAR.md).
        "objections_critic_par_type": None,
        "cout_jour_eur": cout_jour,
        "cout_moyen_par_dossier_analyse_eur": round(cout_jour / nb_analysees, 4) if nb_analysees else None,
        "cout_par_role_eur": cout_par_role,
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
