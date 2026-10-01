"""Cycle du worker de la version 2 (V2.8, RADAR-V2.md) : ce que fait `python -m app.cli run-forever` depuis le 2026-10-01.

Décision de Mathéo : le worker exécute le cycle v2 et plus jamais le pipeline v1 (désactivé par `config/cycle_v2.yaml`, jamais supprimé).

Au démarrage (les migrations additives des tables v2 sont faites par la commande avant d'appeler ce module) :
1. **Cartographie initiale**, seulement si `RADAR_CARTOGRAPHIE_INITIALE=1` ET qu'aucune cartographie initiale n'est déjà terminée en base.
   Elle ne traite que les secteurs de priorité 1 (`config/secteurs_tpe.yaml::priorite`), dans l'enveloppe unique
   `RADAR_ENVELOPPE_INITIALE_EUR` (étiquetage + fiches) : établissements -> offres d'emploi -> étiquetage -> agrégation -> fiches, passe après
   passe. Reprenable (un redémarrage reprend où l'on s'est arrêté : chaque étape saute ce qui est fait) et idempotente (rien n'est jamais
   refait ni repayé). Terminée quand tout est fait pour la priorité 1 ou que l'enveloppe est épuisée.
2. **Régime quotidien**, ensuite et pour toujours : une passe par jour UTC (et des reprises dans la journée tant qu'il reste du travail
   et du budget) : collecte des nouvelles offres -> mesure des établissements (rafraîchissement mensuel) -> agrégation -> fiches
   recalculées quand leurs agrégats ont bougé -> étiquetage, **dans l'ordre des priorités 1, 2, 3**, avec un plafond de dépense modèle de
   2 €/jour UTC (étiquetage + fiches, tous runs confondus).

Chaque étape écrit son résumé d'avancement dans la table `runs` (`resume_json` : état, étapes, journal, avancement chiffré) ET dans les logs.
Aucun appel n'est fait sans les garde-fous existants : disjoncteur API, budget dur, enveloppe, plafonds de requêtes.
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine

from app import config as cfg
from app import referentiels
from app.storage import repo
from app.storage.schema import runs

logger = logging.getLogger(__name__)

VERSION_CODE = "radar-opportunites-v2-cycle"
MODE_INITIALE = "cartographie_initiale_v2"
MODE_QUOTIDIEN = "cycle_v2"
VARIABLE_CARTOGRAPHIE = "RADAR_CARTOGRAPHIE_INITIALE"
JOURNAL_MAX_LIGNES = 60  # lignes gardées dans runs.resume_json (le reste est dans les logs)
MARGE_PLAFOND_EUR = 0.02  # en deçà de cette marge sous le plafond, on considère le plafond atteint (un appel ne passerait plus)
PREFIXES_ARRET_SYSTEMIQUE = ("disjoncteur", "budget", "enveloppe", "ANTHROPIC_API_KEY", "identifiants", "Identifiants", "API indisponible", "5 échecs")


# ------------------------------------------------------------------ opérations (injectables pour les tests) -----

def _op_collecter_offres(engine: Engine, **kw: Any):
    from app.offres import collecter_offres
    return collecter_offres(engine, **kw)


def _op_rafraichir_etablissements(engine: Engine, **kw: Any):
    from app.etablissements import rafraichir_etablissements
    return rafraichir_etablissements(engine, **kw)


def _op_etiqueter_offres(engine: Engine, **kw: Any):
    from app.etiquetage import etiqueter_offres
    return etiqueter_offres(engine, **kw)


def _op_calculer_agregats(engine: Engine, **kw: Any):
    from app.agregation import calculer_agregats
    return calculer_agregats(engine, **kw)


def _op_produire_fiches(engine: Engine, **kw: Any):
    from app.fiches import produire_fiches
    return produire_fiches(engine, **kw)


@dataclass
class Operations:
    """Les cinq opérations du cycle. Les tests les remplacent par des doublures (aucun réseau, aucun modèle)."""
    collecter_offres: Callable[..., Any] = _op_collecter_offres
    rafraichir_etablissements: Callable[..., Any] = _op_rafraichir_etablissements
    etiqueter_offres: Callable[..., Any] = _op_etiqueter_offres
    calculer_agregats: Callable[..., Any] = _op_calculer_agregats
    produire_fiches: Callable[..., Any] = _op_produire_fiches


def _maintenant() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ réglages -----

def cartographie_initiale_demandee(environ: dict[str, str] | None = None) -> bool:
    brut = (environ if environ is not None else os.environ).get(VARIABLE_CARTOGRAPHIE, "")
    return brut.strip().lower() in {"1", "true", "yes", "oui"}


def pause_demandee(engine: Engine) -> bool:
    return cfg.get_settings().pause_all or repo.lire_pause_all(engine)


def plafond_jour_eur() -> float:
    return float(cfg.etiquetage()["budget_eur_par_jour"])


# ------------------------------------------------------------------ suivi : runs + logs -----

class Suivi:
    """Écrit l'avancement d'un run (`runs.resume_json`) et les logs à chaque étape. Ne lève jamais : un échec d'écriture du suivi
    est journalisé, il n'arrête pas le cycle."""

    def __init__(self, engine: Engine, run_id: str, etat: dict[str, Any]):
        self.engine, self.run_id, self.etat = engine, run_id, etat
        self.etat.setdefault("journal", [])
        self.etat.setdefault("etapes", {})

    def ecrire(self, etape: str, texte: str, **donnees: Any) -> None:
        ligne = f"{_maintenant().strftime('%H:%M:%S')} [{etape}] {texte}"
        logger.info("[cycle v2] %s", ligne)
        self.etat["journal"] = (self.etat["journal"] + [ligne])[-JOURNAL_MAX_LIGNES:]
        if donnees:
            self.etat["etapes"][etape] = {**self.etat["etapes"].get(etape, {}), **donnees}
        self.sauver()

    def sauver(self, *, avancement: dict | None = None) -> None:
        if avancement is not None:
            self.etat["avancement"] = avancement
        try:
            repo.mettre_a_jour_progression(self.engine, self.run_id, couts=self.etat.get("couts", {}), resume=self.etat)
        except Exception:  # noqa: BLE001 -- le suivi ne doit jamais arrêter le cycle
            logger.exception("[cycle v2] écriture du suivi impossible (run %s)", self.run_id)


def avancement(engine: Engine) -> dict[str, Any]:
    """Photo chiffrée de la cartographie (états cumulés) : offres, établissements, étiquetage, fiches, coûts. Lecture seule ; `{}` si
    elle échoue (jamais d'exception)."""
    try:
        from app.etiquetage import ROLES_ENVELOPPE
        from app.metriques import _metriques_etablissements, _metriques_etiquetage, _metriques_fiches, _metriques_offres

        jour = _maintenant().date()
        photo = {
            "offres": _metriques_offres(engine), "etablissements": _metriques_etablissements(engine),
            "etiquetage": _metriques_etiquetage(engine, jour), "fiches": _metriques_fiches(engine),
            "depense_modele_cumulee_eur": round(repo.cout_total_par_roles(engine, ROLES_ENVELOPPE), 4),
            "depense_modele_du_jour_eur": round(repo.cout_total_jour_utc(engine, jour), 4),
        }
        return json.loads(json.dumps(photo, default=str))  # dates et décimaux -> texte : le résumé s'écrit dans une colonne JSON
    except Exception:  # noqa: BLE001
        logger.exception("[cycle v2] avancement chiffré indisponible")
        return {}


def _jour_du_run(ligne: dict) -> date:
    """Jour UTC auquel appartient un run du régime quotidien : celui écrit dans son résumé (horloge du cycle), à défaut sa date de début."""
    jour = (ligne.get("resume_json") or {}).get("jour")
    if jour:
        return date.fromisoformat(jour)
    debut = ligne["debut"] if ligne["debut"].tzinfo else ligne["debut"].replace(tzinfo=timezone.utc)
    return debut.date()


def _run_du_jour(engine: Engine, mode: str, jour: date) -> dict | None:
    with engine.connect() as cx:
        lignes = cx.execute(select(runs).where(runs.c.mode == mode, runs.c.statut == "en_cours").order_by(runs.c.debut.desc())).mappings().all()
    return next((dict(ligne) for ligne in lignes if _jour_du_run(ligne) == jour), None)


def _dernier_run(engine: Engine, mode: str) -> dict | None:
    with engine.connect() as cx:
        ligne = cx.execute(select(runs).where(runs.c.mode == mode).order_by(runs.c.debut.desc()).limit(1)).mappings().first()
    return dict(ligne) if ligne else None


# ------------------------------------------------------------------ reste à faire (lecture seule) -----

def reste_a_faire(engine: Engine, codes: Sequence[str]) -> dict[str, int]:
    """Ce qu'il reste à faire pour ces codes (0 partout = rien à faire) : paires (code, département) à mesurer, codes sans collecte d'offres,
    offres à étiqueter, fiches à produire ou recalculer."""
    from app.etablissements import paires_a_rafraichir
    secteurs, zone = referentiels.secteurs_tpe(), referentiels.zone()
    reglages = cfg.etiquetage()
    maintenant = _maintenant()
    fins = repo.dernieres_fins_collecte_offres(engine, secteurs.naf_version)
    offres = repo.offres_a_etiqueter(
        engine, secteurs.naf_version, version=str(reglages["version"]), limite=1_000_000, departements_zone=zone.departements_zone(),
        avec_modele=True, max_par_code=reglages.get("echantillon_max_par_code"),
        depuis=maintenant - timedelta(days=int(reglages["fenetre_stock_jours"])),
        exclure_codes=tuple(s.code for s in secteurs.secteurs if s.exclusion), seulement_codes=tuple(codes),
    )
    from app.fiches import preparer
    a_produire, _rejets, _resume = preparer(engine, maintenant=maintenant, aujourdhui=maintenant.date())
    return {
        "paires_etablissements": len(paires_a_rafraichir(engine, codes=codes)),
        "codes_sans_collecte": sum(1 for c in codes if c not in fins),
        "offres_a_etiqueter": len(offres),
        "fiches_a_produire": sum(1 for c, _r in a_produire if c.cle[0] in set(codes)),
    }


def _arret_systemique(arret: str | None) -> bool:
    return bool(arret) and arret.startswith(PREFIXES_ARRET_SYSTEMIQUE)


def _resume_texte(resume: Any) -> str:
    """Une ligne lisible pour un résumé d'opération (dataclass d'un module existant)."""
    if hasattr(resume, "__dict__"):
        morceaux = []
        for cle, val in vars(resume).items():
            if isinstance(val, (int, float, str)) and val not in (0, "", None) or (isinstance(val, dict) and val):
                morceaux.append(f"{cle}={val}")
        return ", ".join(morceaux) or "rien à faire"
    return str(resume)


# ------------------------------------------------------------------ cartographie initiale -----

def _lire_enveloppe() -> float | None:
    from app.etiquetage import enveloppe_initiale_eur
    return enveloppe_initiale_eur()


def une_passe_initiale(engine: Engine, ops: Operations, suivi: Suivi, *, enveloppe: float, priorite: int) -> tuple[bool, dict[str, int]]:
    """Une passe de la cartographie initiale. Renvoie (terminée ?, reste à faire)."""
    from app.etiquetage import ROLES_ENVELOPPE
    reglages = cfg.cycle_v2()["initiale"]
    codes = referentiels.secteurs_tpe().codes_par_priorite(priorite)
    part = float(reglages["part_etiquetage_de_l_enveloppe"])

    r = ops.rafraichir_etablissements(engine, codes=codes)
    suivi.ecrire("etablissements", _resume_texte(r), dernier=_resume_texte(r))
    r = ops.collecter_offres(engine, codes=codes)
    suivi.ecrire("offres", _resume_texte(r), dernier=_resume_texte(r))
    r = ops.etiqueter_offres(engine, codes=codes, enveloppe=enveloppe * part)
    suivi.ecrire("etiquetage", _resume_texte(r), dernier=_resume_texte(r))
    r = ops.calculer_agregats(engine)
    suivi.ecrire("agregation", _resume_texte(r), dernier=_resume_texte(r))
    r = ops.produire_fiches(engine, seulement_codes=codes, ordre_codes=codes, enveloppe=enveloppe)
    suivi.ecrire("fiches", _resume_texte(r), dernier=_resume_texte(r))

    depense = repo.cout_total_par_roles(engine, ROLES_ENVELOPPE)
    reste = reste_a_faire(engine, codes)
    etiquetage_bloque = depense >= enveloppe * part - MARGE_PLAFOND_EUR
    enveloppe_epuisee = depense >= enveloppe - MARGE_PLAFOND_EUR
    encore = (reste["paires_etablissements"] > 0 or reste["codes_sans_collecte"] > 0
              or (reste["offres_a_etiqueter"] > 0 and not etiquetage_bloque)
              or (reste["fiches_a_produire"] > 0 and not enveloppe_epuisee))
    suivi.sauver(avancement={**avancement(engine), "reste_priorite": reste, "enveloppe_eur": enveloppe,
                             "depense_enveloppe_eur": round(depense, 4)})
    suivi.etat["couts"] = {"depense_enveloppe_eur": round(depense, 4)}
    return (not encore), reste


def cartographie_initiale(
    engine: Engine, ops: Operations, *, dormir: Callable[[float], None] = time.sleep, horloge: Callable[[], datetime] = _maintenant,
) -> bool:
    """Lance (ou reprend) la cartographie initiale. Renvoie True si elle est terminée (ou l'était déjà), False si elle n'a pas pu avoir lieu."""
    reglages = cfg.cycle_v2()["initiale"]
    priorite = int(reglages["priorite"])
    precedent = _dernier_run(engine, MODE_INITIALE)
    if precedent and precedent["statut"] == "termine":
        logger.info("[cycle v2] cartographie initiale déjà terminée le %s : rien à refaire.", precedent["fin"])
        return True
    try:
        enveloppe = _lire_enveloppe()
    except ValueError as exc:
        enveloppe = None
        erreur = str(exc)
    else:
        erreur = "RADAR_ENVELOPPE_INITIALE_EUR absente" if enveloppe is None else ""
    if enveloppe is None:
        # Jamais de dépense sans enveloppe : on ne lance pas la cartographie initiale, on le dit fort, et le régime quotidien (2 €/jour) continue.
        logger.error("[cycle v2] cartographie initiale demandée mais impossible : %s. Régime quotidien seulement.", erreur)
        run_id = repo.creer_run(engine, mode=MODE_INITIALE, version_code=VERSION_CODE, version_config="?", quotas={})
        repo.terminer_run(engine, run_id, statut="echoue", couts={}, erreurs=[erreur], resume={"refusee": erreur})
        return False

    if precedent and precedent["statut"] == "en_cours":
        run_id, etat = precedent["id"], dict(precedent.get("resume_json") or {})
        logger.info("[cycle v2] reprise de la cartographie initiale (run %s).", run_id)
    else:
        run_id = repo.creer_run(engine, mode=MODE_INITIALE, version_code=VERSION_CODE, version_config="initiale",
                                quotas={"enveloppe_eur": enveloppe, "priorite": priorite})
        etat = {"phase": "initiale", "priorite": priorite, "enveloppe_eur": enveloppe, "passes": 0}
    suivi = Suivi(engine, run_id, etat)
    codes = referentiels.secteurs_tpe().codes_par_priorite(priorite)
    suivi.ecrire("demarrage", f"cartographie initiale : priorité {priorite} ({len(codes)} secteurs), enveloppe {enveloppe:.2f} €", enveloppe_eur=enveloppe)

    terminee, reste = False, {}
    while int(suivi.etat.get("passes", 0)) < int(reglages["passes_max"]):
        if pause_demandee(engine):
            suivi.ecrire("pause", "PAUSE_ALL actif : en attente")
            dormir(float(cfg.cycle_v2()["attente_pause_secondes"]))
            continue
        suivi.etat["passes"] = int(suivi.etat.get("passes", 0)) + 1
        suivi.ecrire("passe", f"passe {suivi.etat['passes']}/{reglages['passes_max']}")
        try:
            terminee, reste = une_passe_initiale(engine, ops, suivi, enveloppe=enveloppe, priorite=priorite)
        except Exception as exc:  # noqa: BLE001 -- une passe ratée ne tue jamais le worker
            logger.exception("[cycle v2] passe de la cartographie initiale interrompue")
            suivi.ecrire("passe", f"passe interrompue par une erreur : {exc}")
        if terminee:
            break
        suivi.ecrire("attente", f"reste à faire {reste} : nouvelle passe dans {reglages['attente_entre_passes_minutes']} min")
        dormir(float(reglages["attente_entre_passes_minutes"]) * 60)

    bilan = {**suivi.etat, "terminee": terminee, "reste_final": reste, "avertissement": None if terminee else f"passes_max atteint : reste {reste}"}
    suivi.ecrire("fin", "cartographie initiale TERMINÉE" if terminee else f"cartographie initiale arrêtée (passes_max) : reste {reste}")
    repo.terminer_run(engine, run_id, statut="termine", couts=suivi.etat.get("couts", {}),
                      erreurs=[] if terminee else [bilan["avertissement"]], resume={**bilan, "journal": suivi.etat["journal"]})
    return True


# ------------------------------------------------------------------ régime quotidien -----

@dataclass
class BilanQuotidien:
    plafond_atteint: bool
    travail_restant: bool
    erreur_systemique: bool


def cycle_quotidien(engine: Engine, ops: Operations, *, horloge: Callable[[], datetime] = _maintenant) -> BilanQuotidien:
    """Une passe du régime quotidien (priorités 1, 2, 3 ; plafond modèle de 2 €/jour UTC)."""
    cycle = cfg.cycle_v2()
    secteurs = referentiels.secteurs_tpe()
    maintenant = horloge()
    jour = maintenant.date()
    tranches = [codes for codes in (secteurs.codes_par_priorite(p) for p in (1, 2, 3)) if codes]
    ordre = secteurs.codes_dans_l_ordre()
    plafond = plafond_jour_eur()

    run = _run_du_jour(engine, MODE_QUOTIDIEN, jour)
    # Un run d'un jour précédent resté « en cours » (redémarrage du worker) est clos avant d'ouvrir celui du jour.
    for ancien in _runs_en_cours_anterieurs(engine, MODE_QUOTIDIEN, jour):
        repo.terminer_run(engine, ancien["id"], statut="termine", couts=ancien.get("couts_json") or {}, erreurs=[], resume=ancien.get("resume_json") or {})
    if run is None:
        run_id = repo.creer_run(engine, mode=MODE_QUOTIDIEN, version_code=VERSION_CODE, version_config="quotidien",
                                quotas={"plafond_jour_eur": plafond, "priorites": [1, 2, 3]})
        etat: dict[str, Any] = {"phase": "quotidien", "jour": jour.isoformat(), "passes": 0}
    else:
        run_id, etat = run["id"], dict(run.get("resume_json") or {})
    etat["passes"] = int(etat.get("passes", 0)) + 1
    suivi = Suivi(engine, run_id, etat)
    suivi.ecrire("quotidien", f"passe {etat['passes']} du {jour.isoformat()} (plafond modèle {plafond:.2f} €/jour, déjà dépensé "
                              f"{repo.cout_total_jour_utc(engine, jour):.4f} €)")
    erreur_sys = False

    def passe(nom: str, action: Callable[[], Any]) -> Any:
        nonlocal erreur_sys
        try:
            r = action()
        except Exception as exc:  # noqa: BLE001 -- une étape ratée n'arrête pas les suivantes ni le worker
            logger.exception("[cycle v2] étape %s en erreur", nom)
            suivi.ecrire(nom, f"ERREUR : {exc}")
            erreur_sys = True
            return None
        suivi.ecrire(nom, _resume_texte(r))
        if _arret_systemique(getattr(r, "arret", None)):
            erreur_sys = erreur_sys or not str(getattr(r, "arret", "")).startswith(("budget", "enveloppe"))
        return r

    # 1. collecte des nouvelles offres (gratuite), une tranche de priorité après l'autre, sous un plafond de requêtes commun
    collecte = etat["etapes"].get("collecte", {}) if "etapes" in etat else {}
    incomplet = False
    if not collecte.get("terminee"):
        from app.offres import max_requetes_par_passe as _cap_offres
        reste_req = _cap_offres()
        for codes in tranches:
            r = passe("offres", lambda c=codes: ops.collecter_offres(engine, codes=c, max_requetes=max(reste_req, 1)))
            if r is None:
                incomplet = True
                continue
            reste_req -= int(getattr(r, "requetes", 0) or 0)
            incomplet = incomplet or bool(getattr(r, "arret", None)) or bool(getattr(r, "codes_en_echec", 0))
            if reste_req <= 0:
                incomplet = True
                break
        suivi.etat["etapes"].setdefault("collecte", {})["terminee"] = not incomplet
    # 2. établissements (rafraîchissement mensuel : saute les paires mesurées depuis moins de 30 jours)
    etab = suivi.etat["etapes"].get("etablissements_jour", {})
    if not etab.get("terminee"):
        from app.etablissements import max_requetes_par_passe as _cap_etab
        reste_req = _cap_etab()
        incomplet_e = False
        for codes in tranches:
            r = passe("etablissements", lambda c=codes: ops.rafraichir_etablissements(engine, codes=c, max_requetes=max(reste_req, 1)))
            if r is None:
                incomplet_e = True
                continue
            reste_req -= int(getattr(r, "requetes", 0) or 0)
            incomplet_e = incomplet_e or bool(getattr(r, "arret", None)) or bool(getattr(r, "paires_en_echec", 0))
            if reste_req <= 0:
                incomplet_e = True
                break
        suivi.etat["etapes"].setdefault("etablissements_jour", {})["terminee"] = not incomplet_e
        incomplet = incomplet or incomplet_e
    # 3. agrégation puis fiches (part du plafond du jour), dans l'ordre des priorités
    passe("agregation", lambda: ops.calculer_agregats(engine))
    depense_avant = repo.cout_total_jour_utc(engine, jour)
    part_fiches = float(cycle["part_fiches_du_plafond_jour"]) * plafond
    passe("fiches", lambda: ops.produire_fiches(engine, ordre_codes=ordre, enveloppe=None, plafond_jour_eur=min(plafond, depense_avant + part_fiches)))
    # 4. étiquetage, tranche par tranche, jusqu'au plafond du jour
    plafond_atteint = False
    etiquetage_restant = False
    for numero, codes in enumerate(tranches, start=1):
        if repo.cout_total_jour_utc(engine, jour) >= plafond - MARGE_PLAFOND_EUR:
            plafond_atteint = True
            etiquetage_restant = True  # les tranches suivantes attendent demain
            break
        r = passe(f"etiquetage_p{numero}", lambda c=codes: ops.etiqueter_offres(engine, codes=c, enveloppe=None, plafond_jour_eur=plafond))
        arret = getattr(r, "arret", None) if r is not None else None
        if r is not None and getattr(r, "offres_prevues", 0) > getattr(r, "offres_traitees", 0):
            etiquetage_restant = True  # le plafond (ou une erreur) a coupé la passe : des offres attendent encore
        if arret and str(arret).startswith(("budget", "enveloppe")):
            plafond_atteint = True
            break
    if repo.cout_total_jour_utc(engine, jour) >= plafond - MARGE_PLAFOND_EUR:
        plafond_atteint = True
    passe("agregation", lambda: ops.calculer_agregats(engine))
    suivi.etat["couts"] = {"depense_jour_eur": round(repo.cout_total_jour_utc(engine, jour), 4), "plafond_jour_eur": plafond}
    suivi.sauver(avancement=avancement(engine))
    suivi.ecrire("bilan", f"plafond du jour {'ATTEINT' if plafond_atteint else 'non atteint'} ; travail restant "
                          f"{'oui' if (incomplet or etiquetage_restant) else 'non'} ; erreur systémique {'oui' if erreur_sys else 'non'}")
    return BilanQuotidien(plafond_atteint=plafond_atteint, travail_restant=incomplet or etiquetage_restant, erreur_systemique=erreur_sys)


def _runs_en_cours_anterieurs(engine: Engine, mode: str, jour: date) -> list[dict]:
    with engine.connect() as cx:
        lignes = cx.execute(select(runs).where(runs.c.mode == mode, runs.c.statut == "en_cours")).mappings().all()
    return [dict(ligne) for ligne in lignes if _jour_du_run(ligne) < jour]


def secondes_jusqu_a_minuit_utc(maintenant: datetime) -> float:
    demain = (maintenant + timedelta(days=1)).replace(hour=0, minute=1, second=0, microsecond=0)
    return max(60.0, (demain - maintenant).total_seconds())


# ------------------------------------------------------------------ le worker -----

def executer_cycle_v2(
    engine: Engine, *, ops: Operations | None = None, dormir: Callable[[float], None] = time.sleep,
    horloge: Callable[[], datetime] = _maintenant, une_iteration: bool = False, environ: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Boucle du worker. Ne retourne jamais (sauf `une_iteration=True`, pour les tests : renvoie le bilan de l'itération)."""
    ops = ops or Operations()
    cycle = cfg.cycle_v2()
    initiale_faite: bool | None = None
    logger.info("[cycle v2] démarrage du worker : cartographie initiale %s ; pipeline v1 %s.",
                "DEMANDÉE" if cartographie_initiale_demandee(environ) else "non demandée", "actif" if cycle.get("pipeline_v1_actif") else "désactivé")
    while True:
        if pause_demandee(engine):
            logger.info("[cycle v2] PAUSE_ALL actif : en attente.")
            if une_iteration:
                return {"pause": True}
            dormir(float(cycle["attente_pause_secondes"]))
            continue
        if cartographie_initiale_demandee(environ) and not initiale_faite:
            initiale_faite = cartographie_initiale(engine, ops, dormir=dormir, horloge=horloge)
        try:
            bilan = cycle_quotidien(engine, ops, horloge=horloge)
        except Exception:  # noqa: BLE001 -- jamais d'arrêt du worker
            logger.exception("[cycle v2] passe quotidienne interrompue par une erreur inattendue")
            if une_iteration:
                return {"erreur": True}
            dormir(float(cycle["attente_apres_erreur_minutes"]) * 60)
            continue
        resultat = {"plafond_atteint": bilan.plafond_atteint, "travail_restant": bilan.travail_restant, "erreur_systemique": bilan.erreur_systemique,
                    "initiale_faite": initiale_faite}
        if une_iteration:
            return resultat
        if bilan.erreur_systemique:
            attente = float(cycle["attente_apres_erreur_minutes"]) * 60
        elif bilan.travail_restant and not bilan.plafond_atteint:
            attente = float(cycle["attente_si_reste_minutes"]) * 60
        else:
            attente = secondes_jusqu_a_minuit_utc(horloge())
        logger.info("[cycle v2] prochaine passe dans %.0f min.", attente / 60)
        fin = horloge() + timedelta(seconds=attente)
        while horloge() < fin:  # attente découpée : une pause demandée est vue en moins d'une minute
            dormir(min(float(cycle["attente_pause_secondes"]), max(1.0, (fin - horloge()).total_seconds())))
            if pause_demandee(engine):
                break
