"""Import des établissements mesurés hors de Render (V2.8b, RADAR-V2.md).

Constat du 2026-10-02 : l'API Recherche d'entreprises (SIRENE) est injoignable depuis le worker Render (erreurs de connexion) alors
qu'elle répond depuis le poste de Mathéo. `scripts/compter_etablissements_local.py` mesure donc les paires (code NAF, département) sur
le poste et écrit `data/etablissements_import.json` (données publiques SIRENE, aucun secret) ; le fichier est commité et déployé, et
le worker l'importe ici.

Règles :
- **Plus récent seulement** : une paire n'est importée que si sa mesure (`mesure_le`) est plus récente que la dernière mesure de la
  même paire en base. Une mesure faite par le worker lui-même après le fichier n'est donc jamais écrasée.
- **Additif et idempotent** : une ligne `etablissements_secteur` de plus (horodatée à la date de la MESURE, pas de l'import) et un
  upsert des prospects par (SIRET, version NAF), exactement comme `app.etablissements.rafraichir_paire`. Rien n'est jamais supprimé ;
  réimporter le même fichier ne fait rien (la paire est alors « déjà à jour »).
- **Validation stricte, tout ou rien** : un fichier mal formé n'importe RIEN et le dit ; jamais une moitié d'import.
- Les prospects sont écrits AVANT le comptage de la paire : si l'import est coupé au milieu, la paire n'a pas de comptage neuf et
  sera reprise au prochain import (l'upsert des prospects ne double rien).
- Paire d'un secteur exclu, inconnu, ou d'un département hors zone : ignorée et comptée, jamais écrite.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine

from app import referentiels
from app.adapters import recherche_entreprises as api
from app.storage import repo

logger = logging.getLogger(__name__)

RACINE = Path(__file__).resolve().parents[1]
FORMAT = "radar-etablissements-import/1"
FICHIER_DEFAUT = "data/etablissements_import.json"
DEPARTEMENT_FRANCE = "FR"
# Ordre des colonnes d'une ligne de prospect dans le fichier (listes plutôt qu'objets : fichier deux fois plus léger).
COLONNES_PROSPECT = ("siret", "raison_sociale", "adresse", "code_postal", "code_commune", "commune", "latitude", "longitude",
                     "tranche_effectif_salarie", "categorie_entreprise", "est_siege")
_SIRET = re.compile(r"^\d{14}$")

# Empreinte (chemin, taille, date de modification) du dernier fichier importé EN ENTIER par ce processus : un fichier inchangé n'est
# pas relu à chaque passe (il fait plusieurs mégaoctets). Un redémarrage du worker relit le fichier une fois (et n'importe rien de neuf).
_derniere_empreinte: tuple[str, int, int] | None = None


class FichierInvalide(ValueError):
    pass


@dataclass
class ResumeImport:
    fichier: str = ""
    fichier_inchange: bool = False
    absent: bool = False
    paires_fichier: int = 0
    paires_importees: int = 0
    paires_deja_a_jour: int = 0
    paires_hors_referentiel: int = 0
    prospects_nouveaux: int = 0
    prospects_deja_connus: int = 0
    genere_le: str | None = None
    erreur: str | None = None
    importees: list[str] = field(default_factory=list)


def chemin_fichier() -> Path:
    from app import config as cfg
    try:
        relatif = (cfg.cycle_v2().get("etablissements") or {}).get("fichier_import") or FICHIER_DEFAUT
    except Exception:  # noqa: BLE001
        relatif = FICHIER_DEFAUT
    chemin = Path(relatif)
    return chemin if chemin.is_absolute() else RACINE / chemin


def reinitialiser_cache() -> None:
    global _derniere_empreinte
    _derniere_empreinte = None


def _date(valeur: Any, nom: str) -> datetime:
    if not isinstance(valeur, str):
        raise FichierInvalide(f"{nom} : date absente")
    try:
        d = datetime.fromisoformat(valeur)
    except ValueError as exc:
        raise FichierInvalide(f"{nom} : date illisible {valeur!r}") from exc
    if d.tzinfo is None:
        raise FichierInvalide(f"{nom} : date sans fuseau horaire {valeur!r}")
    return d.astimezone(timezone.utc)


def _entier(valeur: Any, nom: str, *, nul: bool = False) -> int | None:
    if valeur is None and nul:
        return None
    if isinstance(valeur, bool) or not isinstance(valeur, int) or valeur < 0:
        raise FichierInvalide(f"{nom} : entier positif attendu, reçu {valeur!r}")
    return valeur


def _booleen(valeur: Any, nom: str, *, nul: bool = False) -> bool | None:
    if valeur is None and nul:
        return None
    if not isinstance(valeur, bool):
        raise FichierInvalide(f"{nom} : booléen attendu, reçu {valeur!r}")
    return valeur


def _texte_ou_nul(valeur: Any, nom: str) -> str | None:
    if valeur is None or isinstance(valeur, str):
        return valeur
    raise FichierInvalide(f"{nom} : texte attendu, reçu {type(valeur).__name__}")


def _nombre_ou_nul(valeur: Any, nom: str) -> float | None:
    if valeur is None:
        return None
    if isinstance(valeur, bool) or not isinstance(valeur, (int, float)):
        raise FichierInvalide(f"{nom} : nombre attendu, reçu {valeur!r}")
    return float(valeur)


def lire_fichier(chemin: Path) -> dict[str, Any]:
    """Lit et valide TOUT le fichier ; lève `FichierInvalide` à la première anomalie (aucun import partiel)."""
    try:
        brut = json.loads(chemin.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FichierInvalide(f"fichier illisible : {exc}") from exc
    if not isinstance(brut, dict) or brut.get("format") != FORMAT:
        raise FichierInvalide(f"format attendu {FORMAT!r}")
    if list(brut.get("colonnes_prospect") or []) != list(COLONNES_PROSPECT):
        raise FichierInvalide("colonnes_prospect différentes de celles attendues par ce code")
    if not isinstance(brut.get("naf_version"), str):
        raise FichierInvalide("naf_version absente")
    _date(brut.get("genere_le"), "genere_le")
    paires = brut.get("paires")
    if not isinstance(paires, list):
        raise FichierInvalide("paires : liste attendue")
    vues: set[tuple[str, str]] = set()
    propres = []
    for i, p in enumerate(paires):
        nom = f"paires[{i}]"
        if not isinstance(p, dict):
            raise FichierInvalide(f"{nom} : objet attendu")
        code, dep = p.get("code_naf"), p.get("departement")
        if not isinstance(code, str) or not isinstance(dep, str):
            raise FichierInvalide(f"{nom} : code_naf et departement attendus")
        if (code, dep) in vues:
            raise FichierInvalide(f"{nom} : paire {code}/{dep} en double")
        vues.add((code, dep))
        nom = f"{code}/{dep}"
        source = p.get("source_url")
        if not isinstance(source, str) or not source.startswith(api.URL_RECHERCHE):
            raise FichierInvalide(f"{nom} : source_url doit pointer vers {api.URL_RECHERCHE}")
        prospects = p.get("prospects")
        if not isinstance(prospects, list):
            raise FichierInvalide(f"{nom} : prospects : liste attendue")
        if dep == DEPARTEMENT_FRANCE and prospects:
            raise FichierInvalide(f"{nom} : la mesure France est un comptage seul, sans prospects")
        lignes = []
        sirets: set[str] = set()
        for j, ligne in enumerate(prospects):
            if not isinstance(ligne, list) or len(ligne) != len(COLONNES_PROSPECT):
                raise FichierInvalide(f"{nom} prospect {j} : {len(COLONNES_PROSPECT)} colonnes attendues")
            d = dict(zip(COLONNES_PROSPECT, ligne))
            if not isinstance(d["siret"], str) or not _SIRET.match(d["siret"]):
                raise FichierInvalide(f"{nom} prospect {j} : SIRET invalide")
            if d["siret"] in sirets:
                raise FichierInvalide(f"{nom} prospect {j} : SIRET en double")
            sirets.add(d["siret"])
            if not isinstance(d["raison_sociale"], str) or not d["raison_sociale"].strip():
                raise FichierInvalide(f"{nom} prospect {j} : raison sociale absente")
            for col in ("adresse", "code_postal", "code_commune", "commune", "tranche_effectif_salarie", "categorie_entreprise"):
                _texte_ou_nul(d[col], f"{nom} prospect {j} {col}")
            if d["code_commune"] is not None and not d["code_commune"].startswith(dep):
                raise FichierInvalide(f"{nom} prospect {j} : commune hors du département")
            d["latitude"] = _nombre_ou_nul(d["latitude"], f"{nom} prospect {j} latitude")
            d["longitude"] = _nombre_ou_nul(d["longitude"], f"{nom} prospect {j} longitude")
            _booleen(d["est_siege"], f"{nom} prospect {j} est_siege", nul=True)
            lignes.append(d)
        propres.append({
            "code_naf": code, "departement": dep, "mesure_le": _date(p.get("mesure_le"), f"{nom} mesure_le"),
            "nb_entreprises_actives": _entier(p.get("nb_entreprises_actives"), f"{nom} nb_entreprises_actives"),
            "comptage_plafonne": _booleen(p.get("comptage_plafonne"), f"{nom} comptage_plafonne"),
            "nb_etablissements_listes": _entier(p.get("nb_etablissements_listes"), f"{nom} nb_etablissements_listes", nul=True),
            "echantillon_complet": _booleen(p.get("echantillon_complet"), f"{nom} echantillon_complet", nul=True),
            "plafond_echantillon": _entier(p.get("plafond_echantillon"), f"{nom} plafond_echantillon", nul=True),
            "requetes": _entier(p.get("requetes"), f"{nom} requetes"), "source_url": source, "prospects": lignes,
        })
        if dep != DEPARTEMENT_FRANCE and propres[-1]["nb_etablissements_listes"] != len(lignes):
            raise FichierInvalide(f"{nom} : nb_etablissements_listes ({propres[-1]['nb_etablissements_listes']}) ≠ prospects ({len(lignes)})")
    return {"naf_version": brut["naf_version"], "genere_le": brut["genere_le"], "paires": propres}


def importer(engine: Engine, chemin: Path | None = None, *, forcer: bool = False) -> ResumeImport:
    """Importe les paires du fichier plus récentes que la base. Ne lève jamais : une anomalie est rendue dans `erreur`."""
    global _derniere_empreinte
    chemin = chemin or chemin_fichier()
    resume = ResumeImport(fichier=str(chemin))
    if not chemin.is_file():
        resume.absent = True
        return resume
    stat = chemin.stat()
    empreinte = (str(chemin), stat.st_size, stat.st_mtime_ns)
    if not forcer and empreinte == _derniere_empreinte:
        resume.fichier_inchange = True
        return resume
    try:
        contenu = lire_fichier(chemin)
    except FichierInvalide as exc:
        resume.erreur = f"fichier refusé, rien importé : {exc}"
        logger.error("[import établissements] %s", resume.erreur)
        return resume
    secteurs, zone = referentiels.secteurs_tpe(), referentiels.zone()
    resume.genere_le, resume.paires_fichier = contenu["genere_le"], len(contenu["paires"])
    if contenu["naf_version"] != secteurs.naf_version:
        resume.erreur = f"fichier refusé : naf_version {contenu['naf_version']!r} ≠ référentiel {secteurs.naf_version!r}"
        logger.error("[import établissements] %s", resume.erreur)
        return resume
    codes = {s.code for s in secteurs.non_exclus()}
    departements = set(zone.departements_zone()) | {DEPARTEMENT_FRANCE}
    try:
        derniers = repo.derniers_comptages_etablissements(engine, secteurs.naf_version)
        for p in contenu["paires"]:
            cle = (p["code_naf"], p["departement"])
            if cle[0] not in codes or cle[1] not in departements:
                resume.paires_hors_referentiel += 1
                continue
            en_base = (derniers.get(cle) or {}).get("horodatage")
            if en_base is not None and en_base.tzinfo is None:
                en_base = en_base.replace(tzinfo=timezone.utc)
            if en_base is not None and en_base >= p["mesure_le"]:
                resume.paires_deja_a_jour += 1
                continue
            lignes = []
            for d in p["prospects"]:
                distance = None
                if d["latitude"] is not None and d["longitude"] is not None:
                    distance = round(api.distance_km(zone.centre.latitude, zone.centre.longitude, d["latitude"], d["longitude"]), 2)
                lignes.append({**d, "code_naf": p["code_naf"], "departement": p["departement"], "siren": d["siret"][:9],
                               "distance_centre_km": distance})
            nouveaux, connus = repo.enregistrer_prospects(engine, lignes, naf_version=secteurs.naf_version, maintenant=p["mesure_le"])
            repo.enregistrer_comptage_etablissements(
                engine, code_naf=p["code_naf"], naf_version=secteurs.naf_version, departement=p["departement"],
                nb_entreprises_actives=p["nb_entreprises_actives"], comptage_plafonne=p["comptage_plafonne"],
                nb_etablissements_listes=p["nb_etablissements_listes"], echantillon_complet=p["echantillon_complet"],
                plafond_echantillon=p["plafond_echantillon"], requetes=p["requetes"], source_url=p["source_url"],
                horodatage=p["mesure_le"],
            )
            resume.paires_importees += 1
            resume.prospects_nouveaux += nouveaux
            resume.prospects_deja_connus += connus
            resume.importees.append(f"{cle[0]}/{cle[1]}")
    except Exception as exc:  # noqa: BLE001 -- base indisponible au milieu : on le dit, le prochain import reprendra
        resume.erreur = f"import interrompu ({type(exc).__name__}) après {resume.paires_importees} paires : {exc}"
        logger.exception("[import établissements] import interrompu")
        return resume
    _derniere_empreinte = empreinte
    if resume.paires_importees:
        logger.info("[import établissements] %d paires importées (%d prospects nouveaux) depuis %s", resume.paires_importees,
                    resume.prospects_nouveaux, os.path.basename(chemin))
    return resume


def resume_texte(r: ResumeImport) -> str:
    if r.absent:
        return "aucun fichier d'import"
    if r.fichier_inchange:
        return "fichier inchangé depuis le dernier import"
    if r.erreur:
        return f"ERREUR : {r.erreur}"
    return (f"fichier du {r.genere_le} : {r.paires_fichier} paires, {r.paires_importees} importées, {r.paires_deja_a_jour} déjà à jour, "
            f"{r.paires_hors_referentiel} hors référentiel ; prospects nouveaux={r.prospects_nouveaux}, déjà connus={r.prospects_deja_connus}")
