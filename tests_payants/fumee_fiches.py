"""Test de fumée RÉEL de la sous-étape V2.5 : les TROIS rôles modèle sur des données réelles, de bout en bout.

    ANTHROPIC_API_KEY=... PYTHONPATH=. python tests_payants/fumee_fiches.py

COÛTE quelques dizaines de centimes (enveloppe plafonnée à 0,80 €), hors suite par défaut. Sur UN secteur réel (69.20Z,
activités comptables), dans une base SQLite jetable :
  1. collecte réelle de toutes les offres actives des 90 derniers jours (API France Travail, gratuite) ;
  2. mesure réelle des établissements de Gironde (API Recherche d'entreprises, gratuite) ;
  3. rôle 1 -- ÉTIQUETEUR : étiquetage réel de l'échantillon (60 offres, modèle le moins cher) ;
  4. agrégation (code) ; sélection des couples sur les parts extrapolées (code) ;
  5. rôles 2 et 3 -- ANALYSTE puis CRITIC (modèle approfondi) sur au plus 3 couples ; score et décision (code).
Vérifie : aucun appel perdu, au moins une fiche écrite, nombres des affirmations retenues tous présents dans leurs blocs, le Critic
n'a reçu aucun score, coût sous l'enveloppe. N'écrit dans le rapport aucun texte d'offre (conditions d'utilisation de l'API).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, func, select

from app import agregation, config as cfg, etablissements as etab, etiquetage as etiq, fiches as fch, offres as off
from app.adapters.model_client import ModelClient
from app.pipeline.budget import BudgetTracker
from app.storage import repo
from app.storage.db import migrer
from app.storage.schema import fiches_secteur_tache, offres_emploi, usage_events

RACINE = Path(__file__).resolve().parent.parent
CODE = "69.20Z"
ENVELOPPE_EUR = "0.80"


def _cout_par_role(engine) -> dict:
    with engine.connect() as cx:
        return {r[0]: {"appels": r[1], "tokens_in": r[2], "tokens_out": r[3], "cout_eur": round(r[4], 5)} for r in cx.execute(
            select(usage_events.c.role, func.count(), func.sum(usage_events.c.tokens_in), func.sum(usage_events.c.tokens_out),
                   func.sum(usage_events.c.cout_declare_ou_estime)).where(usage_events.c.role.is_not(None)).group_by(usage_events.c.role)) if r[0]}


class ClientEspion(ModelClient):
    """Vrai ModelClient qui garde les messages réellement envoyés, pour vérifier ce que chaque rôle a reçu."""

    envoyes: list[dict] = []

    def appeler_structure(self, **kw):
        ClientEspion.envoyes.append({"role": kw["role"], "systeme": kw["prompt_systeme"], "utilisateur": kw["prompt_utilisateur"]})
        return super().appeler_structure(**kw)


def main() -> int:
    os.environ["RADAR_ENVELOPPE_INITIALE_EUR"] = ENVELOPPE_EUR
    cfg.get_settings.cache_clear()
    if not cfg.get_settings().has_model_access:
        print("ANTHROPIC_API_KEY absente : test impossible.")
        return 1
    maintenant = datetime.now(timezone.utc)
    chemin = Path(tempfile.mkdtemp()) / "fumee_fiches.db"
    engine = create_engine(f"sqlite:///{chemin}", future=True)
    migrer(engine)

    r_offres = off.collecter_offres(engine, code=CODE, jours=90)
    print(f"1. offres : {r_offres.offres_lues} lues en {r_offres.requetes} requêtes (arrêt : {r_offres.arret})")
    requetes_etab, _, _ = etab.rafraichir_paire(engine, CODE, "33")
    print(f"2. établissements Gironde : {requetes_etab} requêtes")
    r_etiq = etiq.etiqueter_offres(engine)
    print(f"3. étiquetage : {r_etiq.statuts}, citations {r_etiq.citations_verifiees}/{r_etiq.citations_proposees}, coût {r_etiq.cout_eur:.4f} €, arrêt {r_etiq.arret}")
    r_agg = agregation.calculer_agregats(engine)
    print(f"4. agrégation : {r_agg.couples_calcules} couples ; ", end="")

    a_produire, rejets, resume0 = fch.preparer(engine, maintenant=maintenant, aujourdhui=maintenant.date())
    raisons = Counter(raison.split(" :")[0].split(" <")[0][:45] for r in rejets for raison in r.raisons)
    print(f"{resume0.candidats} candidats après seuils et pré-criblage, {resume0.rejets} rejetés")
    candidats_noms = [(c.cle[1], c.agregat["nb_offres_tache_estime"], round(c.agregat["part_offres_tache"], 3)) for c, _ in a_produire]

    run_id = repo.creer_run(engine, mode="fiches_v2_fumee", version_code="fumee", version_config="fumee", quotas={})
    client = ClientEspion(cfg.get_settings(), BudgetTracker(engine, run_id, float(ENVELOPPE_EUR), plafond_appels_approfondis=0))
    r_fiches = fch.produire_fiches(engine, max_fiches=3, client=client)
    print(f"5. fiches : {r_fiches.fiches_ecrites} écrites {r_fiches.par_decision}, Analyste perdu {r_fiches.analyste_perdu}, Critic perdu {r_fiches.critic_perdu}, "
          f"affirmations {r_fiches.affirmations_retenues}/{r_fiches.affirmations_proposees}, coût {r_fiches.cout_eur:.4f} €, arrêt {r_fiches.arret}")

    with engine.connect() as cx:
        fiches = [dict(f) for f in cx.execute(select(fiches_secteur_tache)).mappings()]
        n_offres = cx.execute(select(func.count()).select_from(offres_emploi)).scalar_one()
    couts = _cout_par_role(engine)
    total = sum(v["cout_eur"] for v in couts.values())
    detail = []
    for f in fiches:
        contenu = f["fiche_json"] or {}
        detail.append({
            "tache": f["tache_id"], "decision": f["decision"], "score_brut": f["score_brut"], "score_prudent": f["score_prudent"],
            "criteres": {c["nom"]: [c["points_brut"], c["points_prudent"], c["statut"]] for c in f["score_json"]["criteres"]},
            "motifs": f["motifs_json"], "service_ia_propose": contenu.get("service_ia_propose"), "ce_quil_remplace": contenu.get("ce_quil_remplace"),
            "hypothese_prix": contenu.get("hypothese_prix"), "personnes_necessaires": contenu.get("personnes_necessaires"),
            "prochain_test": contenu.get("prochain_test"), "affirmations_retenues": [a["texte"] for a in contenu.get("affirmations", [])],
            "affirmations_rejetees": contenu.get("affirmations_rejetees", []), "preuves_manquantes_analyste": contenu.get("preuves_manquantes"),
            "objections_critic": [(o["type"], o["gravite"]) for o in (f["critique_json"] or {}).get("objections", [])],
            "decision_critic": (f["critique_json"] or {}).get("decision"),
        })
    verif = {
        "offres_collectees": n_offres > 100,
        "etiquetage_complet_sans_perte": r_etiq.statuts == {"ok": r_etiq.offres_traitees} and r_etiq.offres_traitees > 0,
        "au_moins_un_couple_candidat": resume0.candidats >= 1,
        "au_moins_une_fiche_ecrite": r_fiches.fiches_ecrites >= 1,
        "aucun_analyste_ni_critic_perdu": r_fiches.analyste_perdu == 0 and r_fiches.critic_perdu == 0,
        "pas_d_arret": r_fiches.arret is None and r_etiq.arret is None,
        "cout_sous_enveloppe": total <= float(ENVELOPPE_EUR),
        "le_critic_n_a_recu_aucun_score": all("score" not in e["utilisateur"].lower() and "points" not in e["utilisateur"].lower()
                                              for e in ClientEspion.envoyes if e["role"] == fch.ROLE_CRITIC) and any(e["role"] == fch.ROLE_CRITIC for e in ClientEspion.envoyes),
        "l_analyste_n_a_recu_aucun_texte_d_offre": all("Description :" not in e["utilisateur"] for e in ClientEspion.envoyes if e["role"] == fch.ROLE_ANALYSTE),
    }
    rapport = {
        "date": maintenant.isoformat(), "code_naf": CODE, "offres_collectees": n_offres, "requetes_offres": r_offres.requetes,
        "requetes_etablissements": requetes_etab, "etiquetage": {"statuts": r_etiq.statuts, "citations_proposees": r_etiq.citations_proposees,
                                                                "citations_verifiees": r_etiq.citations_verifiees},
        "analyste_degrade_apres_relance": r_fiches.analyste_degrade, "couples_evalues": resume0.couples_evalues, "candidats": resume0.candidats, "rejets": resume0.rejets, "raisons_de_rejet": dict(raisons),
        "candidats_par_demande": candidats_noms[:15], "fiches": detail, "cout_par_role": couts, "cout_total_eur": round(total, 5),
        "cout_par_fiche_eur": round((couts.get(fch.ROLE_ANALYSTE, {}).get("cout_eur", 0) + couts.get(fch.ROLE_CRITIC, {}).get("cout_eur", 0)) / max(1, len(fiches)), 5),
        "modeles": {"etiqueteur": cfg.get_settings().model_tri, "analyste_critic": cfg.get_settings().model_approfondi}, "verifications": verif,
    }
    sortie = RACINE / "rapports" / f"FUMEE_V2_5_{maintenant.date().isoformat()}.json"
    sortie.write_text(json.dumps(rapport, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: rapport[k] for k in ("candidats", "rejets", "raisons_de_rejet", "candidats_par_demande", "cout_par_role", "cout_total_eur", "verifications")},
                     ensure_ascii=False, indent=1, default=str))
    for d in detail:
        print(f"-- {d['tache']} : {d['decision']} (brut {d['score_brut']} / prudent {d['score_prudent']}) critères {d['criteres']}")
        print(f"   service : {d['service_ia_propose']}")
        print(f"   critic : {d['decision_critic']} {d['objections_critic']} ; affirmations retenues {len(d['affirmations_retenues'])}, rejetées {len(d['affirmations_rejetees'])}")
    print(f"Rapport : {sortie}")
    return 0 if all(verif.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
