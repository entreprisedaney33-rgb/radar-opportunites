"""Test de fumée RÉEL de la sous-étape V2.8 : la cartographie initiale du worker, de bout en bout, LIMITÉE.

    PYTHONPATH=. python tests_payants/fumee_cycle.py

COÛTE quelques centimes (enveloppe plafonnée à 0,40 €), hors suite par défaut. Exerce `app.cycle_v2.cartographie_initiale` pour de vrai :
API France Travail (gratuite), API Recherche d'entreprises (gratuite), modèle le moins cher (étiquetage), modèle approfondi (Analyste et
Critic) -- mais sur UN seul secteur (69.20Z), 5 offres étiquetées au plus et 1 fiche au plus, dans une base SQLite jetable. Pour cela, trois
réglages de volume sont abaissés EN MÉMOIRE (jamais écrits dans les fichiers de config) : priorité 1 réduite à ce secteur, échantillon de
5 offres, 1 fiche par secteur, seuils de sélection et pré-criblage assouplis (5 offres ne suffisent pas pour les seuils de production).
La clé du modèle est lue dans l'environnement ou, à défaut, dans .secrets/anthropic-key.txt (jamais affichée).

Vérifie : aucune erreur, les offres sont collectées et en base, 5 offres étiquetées (appels réels, citations vérifiées par le code), les
établissements mesurés, au moins une fiche écrite, coût sous l'enveloppe, cartographie initiale « terminée » et son résumé écrit dans `runs`,
la seconde exécution n'appelle plus rien (idempotence). N'écrit aucun texte d'offre dans le rapport (conditions d'utilisation de l'API).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, func, select

from app import config as cfg
from app import cycle_v2 as cyc
from app import etiquetage as etiq
from app import referentiels
from app.storage import repo
from app.storage.db import migrer
from app.storage.schema import fiches_secteur_tache, offres_emploi, offres_etiquetage, prospection, runs, usage_events

RACINE = Path(__file__).resolve().parent.parent
CODE = "69.20Z"
ENVELOPPE_EUR = "0.40"


def _cle_modele() -> None:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return
    fichier = RACINE.parents[1] / ".secrets" / "anthropic-key.txt"
    if fichier.is_file():
        os.environ["ANTHROPIC_API_KEY"] = fichier.read_text(encoding="utf-8").strip()


def main() -> int:
    _cle_modele()
    os.environ["RADAR_ENVELOPPE_INITIALE_EUR"] = ENVELOPPE_EUR
    os.environ["RADAR_CARTOGRAPHIE_INITIALE"] = "1"
    cfg.get_settings.cache_clear()
    if not cfg.get_settings().has_model_access:
        print("Aucune clé du modèle : test impossible.")
        return 1

    # --- volumes abaissés en mémoire seulement
    reel_etiq, reel_fiches = cfg.etiquetage(), cfg.fiches()
    cfg.etiquetage = lambda: {**reel_etiq, "echantillon_max_par_code": 5}  # type: ignore[assignment]
    cfg.fiches = lambda: {**reel_fiches, "selection": {**reel_fiches["selection"], "max_fiches_par_code": 1, "offres_etiquetees_min": 1,
                          "part_ic95_bas_min": 0.0, "offres_estimees_min": 1, "offres_estimees_bas_min": 0, "etablissements_rayon_min": 1,
                          "precriblage_score_maximal": False}}  # type: ignore[assignment]
    referentiels.ReferentielSecteurs.codes_par_priorite = lambda self, priorite: (CODE,) if priorite == 1 else ()  # type: ignore[method-assign]

    chemin = Path(tempfile.mkdtemp()) / "fumee_cycle.db"
    engine = create_engine(f"sqlite:///{chemin}", future=True)
    migrer(engine)
    appels: list[str] = []

    class Espion(cyc.Operations):
        pass
    ops = cyc.Operations()
    for nom in ("collecter_offres", "rafraichir_etablissements", "etiqueter_offres", "calculer_agregats", "produire_fiches"):
        f = getattr(ops, nom)
        setattr(ops, nom, (lambda f, nom: (lambda e, **kw: (appels.append(nom), f(e, **kw))[1]))(f, nom))
    # Les fiches : une seule, et une seule passe de l'Analyste puis du Critic
    produire = ops.produire_fiches
    ops.produire_fiches = lambda e, **kw: produire(e, max_fiches=1, **kw)

    debut = datetime.now(timezone.utc)
    terminee = cyc.cartographie_initiale(engine, ops, dormir=lambda s: None)
    duree = (datetime.now(timezone.utc) - debut).total_seconds()

    def compte(table, *cond):
        with engine.connect() as cx:
            return cx.execute(select(func.count()).select_from(table).where(*cond)).scalar_one()
    with engine.connect() as cx:
        run = dict(cx.execute(select(runs).where(runs.c.mode == cyc.MODE_INITIALE)).mappings().first())
        par_role = {r[0]: {"appels": r[1], "cout_eur": round(r[2], 5)} for r in cx.execute(
            select(usage_events.c.role, func.count(), func.sum(usage_events.c.cout_declare_ou_estime)).where(usage_events.c.role.is_not(None)).group_by(usage_events.c.role))}
    cout = repo.cout_total_par_roles(engine, etiq.ROLES_ENVELOPPE)
    n_appels = len(appels)
    terminee_bis = cyc.cartographie_initiale(engine, ops, dormir=lambda s: None)  # idempotence : rien ne doit repartir
    verifs = {
        "cartographie_terminee": terminee is True and run["statut"] == "termine",
        "offres_en_base": compte(offres_emploi) > 0,
        "cinq_offres_etiquetees": compte(offres_etiquetage, offres_etiquetage.c.statut == "ok") == 5,
        "etablissements_mesures": compte(prospection) > 0,
        "au_moins_une_fiche": compte(fiches_secteur_tache) >= 1,
        "au_plus_une_fiche": compte(fiches_secteur_tache) <= 1,
        "cout_sous_enveloppe": 0 < cout <= float(ENVELOPPE_EUR),
        "resume_ecrit_dans_runs": bool((run["resume_json"] or {}).get("journal")) and "avancement" in (run["resume_json"] or {}),
        "idempotent": terminee_bis is True and len(appels) == n_appels,
    }
    rapport = {
        "date": debut.isoformat(), "secteur": CODE, "duree_s": round(duree, 1), "offres_en_base": compte(offres_emploi),
        "etiquetees_ok": compte(offres_etiquetage, offres_etiquetage.c.statut == "ok"), "prospects": compte(prospection),
        "fiches": compte(fiches_secteur_tache), "cout_par_role": par_role, "cout_total_eur": round(cout, 5), "passes": (run["resume_json"] or {}).get("passes"),
        "journal": (run["resume_json"] or {}).get("journal"), "verifications": verifs,
    }
    sortie = RACINE / "rapports" / f"FUMEE_V2_8_{debut.date().isoformat()}.json"
    sortie.write_text(json.dumps(rapport, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in rapport.items() if k != "journal"}, ensure_ascii=False, indent=1))
    print("\n".join(rapport["journal"] or []))
    print(f"\nrapport : {sortie}")
    return 0 if all(verifs.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
