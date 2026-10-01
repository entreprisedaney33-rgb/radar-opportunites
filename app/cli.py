"""Point d'entrée : `python -m app.cli run-once [--dry-run] [--demo] ...`

`--dry-run` bascule sur une base SQLite jetable dédiée (`radar_dry_run.db`,
recréée à chaque fois) et interdit tout appel modèle payant — jamais
d'écriture dans la base de production, comme demandé au §7 (Phase 1).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys


def _construire_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="radar-opportunites")
    sous = parser.add_subparsers(dest="commande", required=True)

    p_run = sous.add_parser("run-once", help="Lance un lot fini : collecte -> Scout -> Analyst -> Critic -> score -> rapport")
    p_run.add_argument("--dry-run", action="store_true",
                        help="Aucun appel modèle payant, écrit dans une base SQLite jetable séparée")
    p_run.add_argument("--demo", action="store_true", help="Force les données de démonstration (DEMO) au lieu des sources réelles")
    p_run.add_argument("--max-signals", type=int, default=None, dest="max_signals")
    p_run.add_argument("--max-deep-dives", type=int, default=None, dest="max_deep_dives")
    p_run.add_argument("--rapport", default=None, help="Chemin du rapport HTML (défaut: rapport_<run_id>.html)")

    p_forever = sous.add_parser(
        "run-forever",
        help="Tourne en continu (Background Worker) : cycle v2 (cartographie initiale si RADAR_CARTOGRAPHIE_INITIALE=1, puis régime quotidien) ; "
             "le pipeline v1 n'est lancé que si config/cycle_v2.yaml::pipeline_v1_actif est vrai",
    )
    p_forever.add_argument("--demo", action="store_true", help="Force les données de démonstration (DEMO) au lieu des sources réelles")

    sous.add_parser("migrate", help="Crée les tables manquantes dans la base configurée (DATABASE_URL)")

    p_etab = sous.add_parser(
        "etablissements",
        help="V2.2 : mesure les entreprises actives et collecte l'échantillon de prospection (API Recherche d'entreprises, gratuite)",
    )
    p_etab.add_argument("--jours", type=int, default=30, help="Saute les paires mesurées depuis moins de N jours (défaut 30)")
    p_etab.add_argument("--max-requetes", type=int, default=None, dest="max_requetes",
                        help="Plafond de requêtes de la passe (défaut : RADAR_ETAB_MAX_REQUETES ou 2000)")
    p_etab.add_argument("--code", default=None, help="Un seul code NAF (ex. 69.20Z)")
    p_etab.add_argument("--departement", default=None, help="Un seul département de la zone, ou FR (comptage France)")

    p_offres = sous.add_parser(
        "offres",
        help="V2.3 : collecte les offres d'emploi actives de France Travail par code NAF (identifiants dans l'environnement)",
    )
    p_offres.add_argument("--jours", type=int, default=None,
                          help="Recul fixe en jours (défaut : reprend à la dernière collecte, 90 jours la première fois)")
    p_offres.add_argument("--max-requetes", type=int, default=None, dest="max_requetes",
                          help="Plafond de requêtes de la passe (défaut : RADAR_FT_MAX_REQUETES ou 3000)")
    p_offres.add_argument("--code", default=None, help="Un seul code NAF (ex. 69.20Z)")

    p_etiq = sous.add_parser(
        "etiqueter",
        help="V2.4 : étiquette les offres par tâche (lexique gratuit, puis modèle le moins cher avec citation vérifiée)",
    )
    p_etiq.add_argument("--max-offres", type=int, default=None, dest="max_offres",
                        help="Plafond d'offres de la passe (défaut : RADAR_ETIQ_MAX_OFFRES ou 2000)")
    p_etiq.add_argument("--sans-modele", action="store_true", dest="sans_modele",
                        help="Lexique seul, aucun appel modèle (gratuit) ; les offres restent à reprendre")
    p_etiq.add_argument("--estimer", action="store_true",
                        help="N'appelle rien : estime le coût du modèle pour les offres encore à étiqueter dans la base")

    p_fiches = sous.add_parser(
        "fiches",
        help="V2.5 : sélectionne les couples secteur x tâche, produit les fiches (Analyste, score, Critic) et décide par le code",
    )
    p_fiches.add_argument("--max-fiches", type=int, default=None, dest="max_fiches",
                          help="Plafond de fiches de la passe (défaut : config/fiches.yaml, 50)")
    p_fiches.add_argument("--estimer", action="store_true",
                          help="N'appelle rien : compte les couples candidats et à (re)calculer, et estime le coût")

    p_conc = sous.add_parser(
        "concurrence",
        help="V2.6 : concurrence des fiches (recherche web derrière un drapeau, désactivée ; liste et import pour la procédure V2.6b)",
    )
    groupe = p_conc.add_mutually_exclusive_group(required=True)
    groupe.add_argument("--etat", action="store_true", help="État du fournisseur web, requêtes du mois, couples évalués (aucun appel)")
    groupe.add_argument("--evaluer-web", action="store_true", dest="evaluer_web",
                        help="Évalue les meilleures fiches par recherche web ; ne fait rien tant que le drapeau et la clé ne sont pas posés")
    groupe.add_argument("--lister-session", action="store_true", dest="lister_session",
                        help="V2.6b : écrit le fichier de travail des meilleures fiches (JSON) pour la session de recherche")
    groupe.add_argument("--importer", metavar="FICHIER", default=None,
                        help="V2.6b : valide strictement puis importe le fichier rempli par la session (tout ou rien)")
    p_conc.add_argument("--top", type=int, default=30, help="Nombre de fiches (défaut 30)")
    p_conc.add_argument("--sortie", default=None, help="Fichier de sortie de --lister-session (défaut : sortie standard)")
    p_conc.add_argument("--verifier-sources", action="store_true", dest="verifier_sources",
                        help="Avec --importer : relit chaque page de prix et vérifie que la citation y figure (réseau, jamais bloquant)")

    sous.add_parser("agreger", help="V2.4 : calcule la demande par secteur x tâche (stock d'offres actives sur 90 jours)")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = _construire_parser().parse_args(argv)

    if args.commande == "migrate":
        from app.storage.db import migrer

        migrer()
        print("Tables créées/à jour.")
        return 0

    if args.commande == "etablissements":
        from app.etablissements import rafraichir_etablissements
        from app.storage.db import get_engine, migrer

        engine = get_engine()
        migrer(engine)
        resume = rafraichir_etablissements(
            engine, jours=args.jours, max_requetes=args.max_requetes, code=args.code, departement=args.departement,
        )
        print(
            f"Paires prévues {resume.paires_prevues}, mesurées {resume.paires_mesurees}, en échec {resume.paires_en_echec}, "
            f"requêtes {resume.requetes}, prospects nouveaux {resume.prospects_nouveaux} "
            f"(déjà connus {resume.prospects_deja_connus})."
        )
        if resume.arret:
            print(f"Arrêt anticipé : {resume.arret}")
        for echec in resume.echecs[:10]:
            print(f"  échec : {echec}")
        return 1 if resume.paires_en_echec and not resume.paires_mesurees else 0

    if args.commande == "offres":
        from app.offres import collecter_offres
        from app.storage.db import get_engine, migrer

        engine = get_engine()
        migrer(engine)
        resume = collecter_offres(engine, jours=args.jours, code=args.code, max_requetes=args.max_requetes)
        print(
            f"Codes prévus {resume.codes_prevus}, collectés {resume.codes_collectes}, en échec {resume.codes_en_echec}, "
            f"requêtes {resume.requetes}, offres lues {resume.offres_lues} (nouvelles {resume.offres_nouvelles}, "
            f"ignorées {resume.offres_ignorees}), fenêtres tronquées {resume.fenetres_tronquees}."
        )
        if resume.arret:
            print(f"Arrêt anticipé : {resume.arret}")
        for echec in resume.echecs[:10]:
            print(f"  échec : {echec}")
        return 1 if resume.arret and not resume.codes_collectes else 0

    if args.commande == "etiqueter":
        from app.etiquetage import estimer_pour_base, etiqueter_offres
        from app.storage.db import get_engine, migrer

        engine = get_engine()
        migrer(engine)
        if args.estimer:
            est = estimer_pour_base(engine)
            print(f"{est.nb_offres} offres à étiqueter ; ~{est.jetons_entree_par_offre:.0f} jetons en entrée et "
                  f"~{est.jetons_sortie_par_offre:.0f} en sortie par offre ({est.modele}) ; "
                  f"~{est.cout_par_offre_eur:.5f} € par offre, ~{est.cout_total_eur:.2f} € au total (estimation, rien n'a été appelé).")
            return 0
        resume = etiqueter_offres(engine, max_offres=args.max_offres, avec_modele=not args.sans_modele)
        print(
            f"Offres prévues {resume.offres_prevues}, traitées {resume.offres_traitees} {resume.statuts}, "
            f"tâches par lexique {resume.taches_lexique}, citations proposées {resume.citations_proposees} "
            f"(vérifiées {resume.citations_verifiees}), appels modèle {resume.appels_modele}, coût {resume.cout_eur:.4f} €."
        )
        if resume.arret:
            print(f"Arrêt / remarque : {resume.arret}")
        return 0

    if args.commande == "fiches":
        from datetime import datetime, timezone

        from app.fiches import estimer_cout_fiche, preparer, produire_fiches
        from app.storage.db import get_engine, migrer

        engine = get_engine()
        migrer(engine)
        if args.estimer:
            maintenant = datetime.now(timezone.utc)
            a_produire, rejets, r = preparer(engine, maintenant=maintenant, aujourdhui=maintenant.date())
            par_fiche = estimer_cout_fiche()
            print(f"{r.couples_evalues} couples évalués : {r.candidats} candidats, {r.rejets} rejetés par les seuils ; "
                  f"{r.a_produire} fiches à produire ou recalculer, ~{par_fiche:.4f} € la fiche, ~{par_fiche * r.a_produire:.2f} € au total "
                  f"(estimation, rien n'a été appelé).")
            return 0
        r = produire_fiches(engine, max_fiches=args.max_fiches)
        print(f"Couples évalués {r.couples_evalues}, candidats {r.candidats}, à produire {r.a_produire}, fiches écrites {r.fiches_ecrites} "
              f"{r.par_decision}, Analyste perdu {r.analyste_perdu}, Critic perdu {r.critic_perdu}, affirmations retenues "
              f"{r.affirmations_retenues}/{r.affirmations_proposees}, coût {r.cout_eur:.4f} €.")
        if r.arret:
            print(f"Arrêt / remarque : {r.arret}")
        return 0

    if args.commande == "concurrence":
        import json

        from app import concurrence as conc
        from app.storage.db import get_engine, migrer

        engine = get_engine()
        migrer(engine)
        if args.etat:
            print(json.dumps(conc.metriques_concurrence(engine), ensure_ascii=False, indent=2))
            return 0
        if args.evaluer_web:
            etat = conc.etat_fournisseur()
            if not etat.actif:
                print(f"Recherche web de la concurrence {etat.raison}. Rien n'a été appelé ; le critère reste « non évalué » (voir PROCEDURE-V2.6b.md).")
                return 0
            resultats = conc.evaluer_web(engine, max_couples=args.top)
            evalues = sum(1 for r in resultats if r.statut == "evalue")
            print(f"{len(resultats)} couples traités, {evalues} évalués, {len(resultats) - evalues} non évalués.")
            for r in resultats:
                if r.statut != "evalue":
                    print(f"  {r.code_naf}/{r.tache_id} : non évalué ({r.motif})")
            return 0
        if args.lister_session:
            contenu = json.dumps(conc.lister_pour_session(engine, args.top), ensure_ascii=False, indent=2)
            if args.sortie:
                with open(args.sortie, "w", encoding="utf-8") as f:
                    f.write(contenu + "\n")
                print(f"Fichier de travail écrit : {args.sortie}")
            else:
                print(contenu)
            return 0
        try:
            with open(args.importer, encoding="utf-8") as f:
                donnees = json.load(f)
        except (OSError, ValueError) as exc:
            print(f"Fichier illisible : {exc}")
            return 2
        validation, verifs = conc.importer_session(engine, donnees, verifier=args.verifier_sources)
        if validation.erreurs:
            print(f"IMPORT REFUSÉ, rien n'a été écrit ({len(validation.erreurs)} erreur(s)) :")
            for e in validation.erreurs:
                print(f"  - {e}")
            return 2
        print(f"{len(validation.lignes)} évaluations importées (source session_claude)." + (f" Sources de prix : {verifs}." if verifs else ""))
        return 0

    if args.commande == "agreger":
        from app.agregation import calculer_agregats
        from app.storage.db import get_engine, migrer

        engine = get_engine()
        migrer(engine)
        r = calculer_agregats(engine)
        print(f"Secteurs avec offres {r.secteurs_avec_offres}, couples calculés {r.couples_calcules}, "
              f"lignes ajoutées {r.lignes_ajoutees}, inchangées {r.lignes_inchangees}.")
        return 0

    if args.commande == "run-once":
        if args.dry_run:
            os.environ["DATABASE_URL"] = "sqlite:///./radar_dry_run.db"
            if os.path.exists("radar_dry_run.db"):
                os.remove("radar_dry_run.db")
            from app import config as cfg

            cfg.get_settings.cache_clear()

        from app.pipeline.orchestrator import ArretPause, OptionsRun, executer_run
        from app.reports.html_report import generer_rapport_html
        from app.storage.db import get_engine, migrer

        engine = get_engine()
        migrer(engine)

        options = OptionsRun(
            mode="dry-run" if args.dry_run else "reel",
            max_signaux=args.max_signals,
            max_analyses=args.max_deep_dives,
            forcer_demo=args.demo,
        )
        try:
            run_id, resume = executer_run(engine, options)
        except ArretPause as exc:
            print(f"ARRÊT : {exc}", file=sys.stderr)
            return 1

        chemin_rapport = args.rapport or f"rapport_{run_id[:8]}.html"
        with open(chemin_rapport, "w", encoding="utf-8") as f:
            f.write(generer_rapport_html(engine, run_id))

        print(f"Run {run_id} terminé. Rapport : {chemin_rapport}")
        print(f"Résumé : {resume}")
        return 0

    if args.commande == "run-forever":
        from app import config as cfg
        from app.storage.db import get_engine, migrer

        engine = get_engine()
        migrer(engine)  # migrations additives (tables v2 comprises) : jamais de suppression ni de réécriture
        if cfg.cycle_v2().get("pipeline_v1_actif"):
            from app.pipeline.orchestrator import executer_continu

            executer_continu(engine, forcer_demo=args.demo)  # ancien comportement, rétabli par config/cycle_v2.yaml
            return 0
        from app.cycle_v2 import executer_cycle_v2

        executer_cycle_v2(engine)  # V2.8 : cartographie initiale (si demandée) puis régime quotidien ; ne retourne jamais
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
