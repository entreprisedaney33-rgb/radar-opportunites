"""Concurrence d'un couple secteur x tâche (V2.6, RADAR-V2.md §7).

Deux voies, une seule table (`concurrence_secteur_tache`), un seul critère de score (`app.scoring_v2.critere_concurrence`) :

1. **Recherche web bornée** (`evaluer_web`), derrière un drapeau, DÉSACTIVÉE (décision de Mathéo du 2026-10-01 : aucune clé de moteur
   payant pour l'instant). Les requêtes sont composées par le code, en français ; chaque requête est réservée en base AVANT l'appel
   (plafond mensuel strict) ; sans drapeau ET sans clé, rien n'est appelé et le critère reste « non évalué » (0 point).
2. **Session Claude Code** (procédure V2.6b, `PROCEDURE-V2.6b.md`) : `lister_pour_session` donne les 30 meilleures fiches, la session
   cherche avec ses propres outils, écrit un fichier JSON, et `importer_session` le VALIDE strictement avant d'écrire quoi que ce soit.

Principe commun : une concurrence « évaluée » porte toujours ses sources (URL https), et un échec ou un doute reste « non évalué » --
jamais « aucun outil » par défaut (ce serait gagner 10 points sur une panne). Aucun modèle n'intervient ici ; aucun test ne fait de réseau.
"""
from __future__ import annotations

import logging
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable
from urllib.parse import urlsplit

from sqlalchemy.engine import Engine

from app import config as cfg
from app import referentiels
from app.enqueteur.concurrents import identifier_concurrents
from app.enqueteur.fetch import PageCollectee, recuperer_page
from app.enqueteur.fournisseur_payant import FournisseurBraveSearch, VARIABLE_CLE
from app.enqueteur.fournisseurs import FournisseurRecherche, ResultatRecherche
from app.storage import repo

logger = logging.getLogger(__name__)

SOURCE_WEB = "web_brave"
SOURCE_SESSION = "session_claude"
VARIABLE_DRAPEAU = "RADAR_CONCURRENCE_WEB"
VARIABLE_PLAFOND = "RADAR_RECHERCHE_WEB_MAX_MOIS"
VERSION_FORMAT_SESSION = 1
MAX_OUTILS = 8


class FournisseurBraveFrance(FournisseurBraveSearch):
    """Brave Search, résultats français. Même classe, même clé, mêmes garde-fous que `FournisseurBraveSearch` ; seuls pays et langue changent."""

    pays = "FR"
    langue = "fr"


# ------------------------------------------------------------------ drapeau, plafond, requêtes (pur) -----

@dataclass(frozen=True)
class EtatFournisseur:
    actif: bool
    raison: str


def etat_fournisseur() -> EtatFournisseur:
    """Actif seulement si le drapeau `RADAR_CONCURRENCE_WEB` vaut 1/true/yes ET que la clé est présente. Ne lit jamais la clé autrement
    que pour savoir si elle existe, ne l'affiche ni ne la journalise."""
    drapeau = os.environ.get(VARIABLE_DRAPEAU, "").strip().lower() in {"1", "true", "yes"}
    cle = bool(os.environ.get(VARIABLE_CLE))
    if not drapeau and not cle:
        return EtatFournisseur(False, f"désactivé : ni {VARIABLE_DRAPEAU} ni clé de recherche web (décision du 2026-10-01)")
    if not drapeau:
        return EtatFournisseur(False, f"désactivé : {VARIABLE_DRAPEAU} n'est pas à 1 (la clé seule ne suffit jamais)")
    if not cle:
        return EtatFournisseur(False, f"désactivé : {VARIABLE_DRAPEAU}=1 mais aucune clé ({VARIABLE_CLE} absente)")
    return EtatFournisseur(True, "actif")


def plafond_mensuel() -> int:
    """`RADAR_RECHERCHE_WEB_MAX_MOIS` si posée, sinon `config/concurrence.yaml`. Une valeur illisible ou négative ferme tout (0) : en cas de
    doute sur un plafond de dépense, on ne cherche pas."""
    brut = os.environ.get(VARIABLE_PLAFOND)
    if brut is None or not brut.strip():
        return int(cfg.concurrence()["recherche_web"]["max_requetes_par_mois"])
    try:
        valeur = int(brut.strip())
    except ValueError:
        logger.warning("%s illisible : plafond ramené à 0 (aucune recherche).", VARIABLE_PLAFOND)
        return 0
    return max(0, valeur)


def composer_requetes(libelle_secteur: str, libelle_tache: str, reglages: dict | None = None) -> dict[str, str]:
    """{'outils': ..., 'prestataires': ...} : deux requêtes en français, composées par le code à partir des libellés des référentiels."""
    gabarits = (reglages or cfg.concurrence())["requetes"]
    valeurs = {"tache": libelle_tache.strip().lower(), "secteur": libelle_secteur.strip().lower()}
    return {cle: " ".join(gabarit.format(**valeurs).split()) for cle, gabarit in gabarits.items()}


def _normaliser(texte: str) -> str:
    sans_accents = unicodedata.normalize("NFKD", texte).encode("ascii", "ignore").decode("ascii")
    return sans_accents.lower()


def mentionne_zone(texte: str, termes: list[str] | tuple[str, ...]) -> str | None:
    """Le premier terme de zone cité en MOT ENTIER (sans tenir compte de la casse ni des accents), sinon `None`."""
    normalise = _normaliser(texte)
    for terme in termes:
        if re.search(r"(?<!\w)" + re.escape(_normaliser(terme)) + r"(?!\w)", normalise):
            return terme
    return None


def url_https(valeur: Any) -> bool:
    if not isinstance(valeur, str):
        return False
    morceaux = urlsplit(valeur.strip())
    return morceaux.scheme == "https" and bool(morceaux.netloc) and " " not in valeur.strip()


def _domaine_exclu(domaine: str, exclus: frozenset[str]) -> bool:
    return any(domaine == e or domaine.endswith("." + e) for e in exclus)


_RE_MONTANT = re.compile(r"\d[\d   ]{0,6}(?:[.,]\d{1,2})?\s?(?:€|euros?\b|EUR\b)|[€$]\s?\d+(?:[.,]\d{1,2})?", re.IGNORECASE)


def extraire_prix(texte: str, *, max_caracteres: int = 160) -> str | None:
    """Le passage de la page qui entoure le premier montant en euros, tel qu'écrit sur la page (aucune conversion, aucun calcul) ; `None`
    s'il n'y a aucun montant. Un prix n'est jamais déduit : il est cité."""
    trouve = _RE_MONTANT.search(texte)
    if trouve is None:
        return None
    debut, fin = max(0, trouve.start() - 60), min(len(texte), trouve.end() + 40)
    return " ".join(texte[debut:fin].split())[:max_caracteres]


def classer_resultats(
    resultats_outils: list[ResultatRecherche], resultats_prestataires: list[ResultatRecherche], *,
    domaines_exclus: frozenset[str], termes_zone: list[str] | tuple[str, ...], max_outils: int = MAX_OUTILS,
) -> tuple[list[dict], dict]:
    """(outils dédiés, service local). Pur, déterministe.

    - outils : résultats de la requête « logiciel » dont le TITRE porte un marqueur d'offre (`identifier_concurrents`, règle de la v1
      3.15), hors annuaires et plateformes génériques, un par domaine ;
    - service local : un résultat de la requête « prestataire », en https, hors annuaires et plateformes, dont le domaine, le titre ou
      l'extrait cite un terme de la zone ; la preuve (terme, URL) est gardée."""
    outils = []
    for c in identifier_concurrents([], resultats_outils, max_concurrents=max_outils, domaines_exclus=domaines_exclus):
        resultat = next((r for r in resultats_outils if urlsplit(r.url).netloc.lower() == c.domaine), None)
        if resultat is None or not url_https(resultat.url):
            continue
        outils.append({"nom": c.nom, "url": resultat.url, "prix": None, "prix_non_trouve": True})
    for r in resultats_prestataires:
        domaine = urlsplit(r.url).netloc.lower()
        if not url_https(r.url) or _domaine_exclu(domaine, domaines_exclus):
            continue
        terme = mentionne_zone(f"{domaine} {r.titre} {r.extrait}", termes_zone)
        if terme:
            return outils, {"present": True, "nom": r.titre.strip(), "url": r.url, "preuve": f"terme de zone « {terme} » dans le domaine, le titre ou l'extrait du résultat"}
    return outils, {"present": False}


# ------------------------------------------------------------------ voie 1 : recherche web bornée -----

@dataclass
class ResultatEvaluation:
    code_naf: str
    tache_id: str
    statut: str  # evalue | non_evalue
    motif: str | None = None
    nb_requetes: int = 0
    outils: int = 0
    service_local: bool | None = None
    ecrit: bool = False


def _non_evalue(engine: Engine, ctx: dict, motif: str, requetes: list[str], maintenant: datetime, nb_requetes: int) -> ResultatEvaluation:
    repo.enregistrer_concurrence(engine, dict(
        code_naf=ctx["code"], tache_id=ctx["tache_id"], naf_version=ctx["naf_version"], source=SOURCE_WEB, statut="non_evalue", motif=motif,
        outils_json=[], service_local_json=None, requetes_json=requetes, evalue_le=maintenant,
    ))
    return ResultatEvaluation(ctx["code"], ctx["tache_id"], "non_evalue", motif, nb_requetes, ecrit=True)


def evaluer_couple_web(
    engine: Engine, *, code: str, tache_id: str, fournisseur: FournisseurRecherche | None = None,
    recuperer: Callable[[ResultatRecherche], PageCollectee | None] | None = None, maintenant: datetime | None = None,
    reglages: dict | None = None, domaines_exclus: frozenset[str] | None = None,
) -> ResultatEvaluation:
    """Évalue UN couple. Ne lève pas pour une panne de fournisseur. Désactivé (drapeau ou clé absents) : rien n'est appelé NI écrit.

    Évalué seulement si LES DEUX requêtes ont répondu avec au moins un résultat : une requête vide ne prouve pas l'absence d'outil ou de
    prestataire (panne, requête trop étroite). Sinon une ligne « non évalué » est écrite (trace), et le score garde 0."""
    r = reglages or cfg.concurrence()
    etat = etat_fournisseur()
    if not etat.actif:
        return ResultatEvaluation(code, tache_id, "non_evalue", etat.raison)
    quand = maintenant or datetime.now(timezone.utc)
    secteurs, taches = referentiels.secteurs_tpe(), referentiels.taches()
    ctx = {"code": code, "tache_id": tache_id, "naf_version": secteurs.naf_version}
    requetes = composer_requetes(secteurs.par_code()[code].libelle, taches.par_id()[tache_id].libelle, r)
    fournisseur = fournisseur or FournisseurBraveFrance()
    web = r["recherche_web"]
    reponses: dict[str, list[ResultatRecherche]] = {}
    posees: list[str] = []
    for cle, requete in requetes.items():
        reservation = repo.reserver_recherche_web(
            engine, fournisseur=fournisseur.nom, mois=quand.strftime("%Y-%m"), requete=requete, code_naf=code, tache_id=tache_id,
            maximum=plafond_mensuel(), maintenant=quand,
        )
        if reservation is None:
            return _non_evalue(engine, ctx, f"plafond mensuel de {plafond_mensuel()} requêtes atteint", posees, quand, len(posees))
        posees.append(requete)
        try:
            reponses[cle] = fournisseur.rechercher(requete, int(web["resultats_par_requete"]))
        except Exception as exc:  # noqa: BLE001 -- une panne de fournisseur ne casse jamais la passe
            logger.warning("Recherche concurrence %s/%s : fournisseur en échec (%s).", code, tache_id, type(exc).__name__)
            reponses[cle] = []
        repo.clore_recherche_web(engine, reservation, len(reponses[cle]))
    vides = [cle for cle, liste in reponses.items() if not liste]
    if vides:
        return _non_evalue(engine, ctx, f"aucun résultat pour la requête « {' » et « '.join(vides)} » : panne ou requête trop étroite, "
                           "pas une preuve d'absence", posees, quand, len(posees))
    exclus = domaines_exclus if domaines_exclus is not None else cfg.domaines_exclus_concurrents()
    outils, local = classer_resultats(reponses["outils"], reponses["prestataires"], domaines_exclus=exclus, termes_zone=r["termes_zone"])
    recuperer = recuperer or recuperer_page
    for outil in outils[: int(r["max_pages_prix_par_couple"])]:
        page = recuperer(ResultatRecherche(url=outil["url"], titre=outil["nom"], extrait="", horodatage_source=None, fournisseur=fournisseur.nom))
        extrait = extraire_prix(page.texte, max_caracteres=int(r["prix_extrait_max_caracteres"])) if page is not None else None
        if extrait and page is not None:
            outil["prix"], outil["prix_non_trouve"] = {"texte": extrait, "source_url": page.url}, False
    repo.enregistrer_concurrence(engine, dict(
        code_naf=code, tache_id=tache_id, naf_version=ctx["naf_version"], source=SOURCE_WEB, statut="evalue", motif=None,
        outils_json=outils, service_local_json=local, requetes_json=posees, evalue_le=quand,
    ))
    return ResultatEvaluation(code, tache_id, "evalue", None, len(posees), len(outils), local["present"], ecrit=True)


def meilleures_fiches(engine: Engine, n: int, *, naf_version: str | None = None) -> list[dict]:
    """Les `n` meilleures fiches (dernière ligne par couple), hors décision « exclue », par score prudent décroissant puis score brut."""
    version = naf_version or referentiels.secteurs_tpe().naf_version
    fiches = [f for f in repo.dernieres_fiches(engine, version).values() if f["decision"] != "exclue"]
    fiches.sort(key=lambda f: (-f["score_prudent"], -f["score_brut"], f["code_naf"], f["tache_id"]))
    return fiches[:n]


def evaluer_web(
    engine: Engine, *, max_couples: int = 30, fournisseur: FournisseurRecherche | None = None, recuperer: Callable | None = None,
    maintenant: datetime | None = None,
) -> list[ResultatEvaluation]:
    """Passe sur les meilleures fiches dont la concurrence n'a pas été évaluée depuis `validite_jours`. Désactivé : liste vide, aucun appel."""
    etat = etat_fournisseur()
    if not etat.actif:
        return []
    quand = maintenant or datetime.now(timezone.utc)
    version = referentiels.secteurs_tpe().naf_version
    validite = int(cfg.concurrence()["validite_jours"])
    faites = repo.dernieres_concurrences(engine, version)
    resultats: list[ResultatEvaluation] = []
    for f in meilleures_fiches(engine, 10_000, naf_version=version):
        if len(resultats) >= max_couples:
            break
        ancienne = faites.get((f["code_naf"], f["tache_id"]))
        if ancienne is not None:
            date_eval = ancienne["evalue_le"] if ancienne["evalue_le"].tzinfo else ancienne["evalue_le"].replace(tzinfo=timezone.utc)
            if (quand - date_eval).days < validite:
                continue
        res = evaluer_couple_web(engine, code=f["code_naf"], tache_id=f["tache_id"], fournisseur=fournisseur, recuperer=recuperer, maintenant=quand)
        resultats.append(res)
        if res.motif and res.motif.startswith("plafond mensuel"):
            break
    return resultats


# ------------------------------------------------------------------ lien avec le score -----

def concurrence_pour_score(ligne: dict | None) -> dict[str, Any] | None:
    """Ce que `app.scoring_v2.critere_concurrence` attend : `None` (non évalué, 0 point) ou {outils_dedies, service_local}."""
    if ligne is None or ligne["statut"] != "evalue":
        return None
    return {"outils_dedies": len(ligne["outils_json"]), "service_local": bool((ligne["service_local_json"] or {}).get("present"))}


# ------------------------------------------------------------------ voie 2 : session Claude Code (V2.6b) -----

def lister_pour_session(engine: Engine, n: int = 30, *, aujourdhui: date | None = None) -> dict[str, Any]:
    """Le fichier de travail de la session V2.6b : les `n` meilleures fiches, avec les requêtes françaises à poser et ce qui est déjà connu."""
    secteurs, taches, r = referentiels.secteurs_tpe().par_code(), referentiels.taches().par_id(), cfg.concurrence()
    deja = repo.dernieres_concurrences(engine, referentiels.secteurs_tpe().naf_version)
    couples = []
    for f in meilleures_fiches(engine, n):
        cle = (f["code_naf"], f["tache_id"])
        requetes = composer_requetes(secteurs[cle[0]].libelle, taches[cle[1]].libelle, r)
        couples.append({
            "code_naf": cle[0], "tache_id": cle[1], "secteur": secteurs[cle[0]].libelle, "tache": taches[cle[1]].libelle,
            "score_prudent": f["score_prudent"], "decision": f["decision"], "requetes_suggerees": requetes,
            "termes_zone": r["termes_zone"], "deja_evalue": cle in deja,
        })
    return {"version_format": VERSION_FORMAT_SESSION, "langue": "fr", "genere_le": (aujourdhui or date.today()).isoformat(), "couples": couples}


@dataclass
class ValidationImport:
    lignes: list[dict] = field(default_factory=list)
    erreurs: list[str] = field(default_factory=list)


def _texte(valeur: Any, minimum: int = 2) -> bool:
    return isinstance(valeur, str) and len(valeur.strip()) >= minimum


def valider_import(donnees: Any, couples_connus: set[tuple[str, str]]) -> ValidationImport:
    """Validation STRICTE du fichier rempli par la session. Toutes les erreurs sont listées ; une seule erreur interdit tout import."""
    v = ValidationImport()
    if not isinstance(donnees, dict):
        v.erreurs.append("le fichier doit être un objet JSON")
        return v
    if donnees.get("version_format") != VERSION_FORMAT_SESSION:
        v.erreurs.append(f"version_format doit valoir {VERSION_FORMAT_SESSION}")
    if donnees.get("langue") != "fr":
        v.erreurs.append("langue doit valoir \"fr\" (recherches et notes en français)")
    evaluations = donnees.get("evaluations")
    if not isinstance(evaluations, list) or not evaluations:
        v.erreurs.append("evaluations : liste non vide attendue")
        return v
    vus: set[tuple[str, str]] = set()
    for i, e in enumerate(evaluations):
        pref = f"evaluations[{i}]"
        if not isinstance(e, dict):
            v.erreurs.append(f"{pref} : objet attendu")
            continue
        cle = (e.get("code_naf"), e.get("tache_id"))
        pref = f"{pref} ({cle[0]}/{cle[1]})"
        if cle not in couples_connus:
            v.erreurs.append(f"{pref} : couple sans fiche (ni code ni tâche inventés)")
        if cle in vus:
            v.erreurs.append(f"{pref} : couple en double dans le fichier")
        vus.add(cle)
        try:
            date.fromisoformat(str(e.get("date_recherche")))
        except ValueError:
            v.erreurs.append(f"{pref} : date_recherche au format AAAA-MM-JJ attendue")
        recherches = e.get("recherches")
        if not (isinstance(recherches, list) and len(recherches) >= 2 and all(_texte(x, 5) for x in recherches)):
            v.erreurs.append(f"{pref} : recherches : au moins 2 requêtes réellement posées (texte)")
        outils = e.get("outils")
        if not isinstance(outils, list):
            v.erreurs.append(f"{pref} : outils : liste attendue (vide si aucun outil dédié trouvé)")
            outils = []
        if len(outils) > MAX_OUTILS:
            v.erreurs.append(f"{pref} : {len(outils)} outils > {MAX_OUTILS} (garder les plus représentatifs)")
        domaines: set[str] = set()
        outils_ok = []
        for j, o in enumerate(outils):
            po = f"{pref} outils[{j}]"
            if not isinstance(o, dict) or not _texte(o.get("nom")) or not url_https(o.get("url")):
                v.erreurs.append(f"{po} : nom et url https obligatoires")
                continue
            domaine = urlsplit(o["url"]).netloc.lower()
            if domaine in domaines:
                v.erreurs.append(f"{po} : domaine {domaine} déjà listé")
            domaines.add(domaine)
            prix = o.get("prix")
            if prix is None:
                if o.get("prix_non_trouve") is not True:
                    v.erreurs.append(f"{po} : prix absent -> écrire \"prix_non_trouve\": true (jamais un prix deviné ni omis en silence)")
                outils_ok.append({"nom": o["nom"].strip(), "url": o["url"].strip(), "prix": None, "prix_non_trouve": True})
                continue
            if not (isinstance(prix, dict) and _texte(prix.get("texte"), 3) and _texte(prix.get("citation"), 3) and url_https(prix.get("source_url"))):
                v.erreurs.append(f"{po} : prix = {{texte, citation, source_url https}} obligatoires")
                continue
            outils_ok.append({"nom": o["nom"].strip(), "url": o["url"].strip(), "prix_non_trouve": False,
                              "prix": {"texte": prix["texte"].strip(), "citation": prix["citation"].strip(), "source_url": prix["source_url"].strip(), "verifie": None}})
        local = e.get("service_local")
        if not isinstance(local, dict) or not isinstance(local.get("present"), bool):
            v.erreurs.append(f"{pref} : service_local = {{\"present\": true|false, ...}} obligatoire")
            local_ok = {"present": False}
        elif local["present"]:
            if not (_texte(local.get("nom")) and url_https(local.get("url")) and _texte(local.get("preuve"), 10)):
                v.erreurs.append(f"{pref} : service_local présent -> nom, url https et preuve (10 caractères au moins) obligatoires")
            local_ok = {"present": True, "nom": str(local.get("nom", "")).strip(), "url": str(local.get("url", "")).strip(), "preuve": str(local.get("preuve", "")).strip()}
        else:
            local_ok = {"present": False}
        note = e.get("note")
        if note is not None and not isinstance(note, str):
            v.erreurs.append(f"{pref} : note : texte attendu")
        v.lignes.append({"code_naf": cle[0], "tache_id": cle[1], "outils": outils_ok, "service_local": local_ok,
                         "recherches": [str(x).strip() for x in recherches] if isinstance(recherches, list) else [], "note": (note or None),
                         "date_recherche": str(e.get("date_recherche"))})
    return v


def verifier_sources_prix(lignes: list[dict], recuperer: Callable[[ResultatRecherche], PageCollectee | None] | None = None) -> dict[str, int]:
    """Option : relit chaque `source_url` de prix et vérifie que la `citation` y figure textuellement (même idée que l'étiqueteur : une
    citation doit se retrouver). `verifie` = True / False ; jamais bloquant (une page de prix chargée par JavaScript ne se relit pas).
    Réseau : appelée seulement par la CLI avec --verifier-sources, jamais par les tests (fonction `recuperer` injectée)."""
    recuperer = recuperer or recuperer_page
    compte = {"verifie": 0, "non_verifie": 0}
    for ligne in lignes:
        for o in ligne["outils"]:
            if not o["prix"]:
                continue
            page = recuperer(ResultatRecherche(url=o["prix"]["source_url"], titre=o["nom"], extrait="", horodatage_source=None, fournisseur=SOURCE_SESSION))
            ok = page is not None and " ".join(o["prix"]["citation"].split()).lower() in " ".join(page.texte.split()).lower()
            o["prix"]["verifie"] = bool(ok)
            compte["verifie" if ok else "non_verifie"] += 1
    return compte


def importer_session(engine: Engine, donnees: Any, *, maintenant: datetime | None = None, verifier: bool = False,
                     recuperer: Callable | None = None) -> tuple[ValidationImport, dict[str, int]]:
    """Valide puis écrit (tout ou rien). Renvoie (validation, compte des vérifications de source). Rien n'est écrit s'il y a une erreur."""
    version = referentiels.secteurs_tpe().naf_version
    connus = set(repo.dernieres_fiches(engine, version))
    validation = valider_import(donnees, connus)
    if validation.erreurs:
        return validation, {}
    verifs = verifier_sources_prix(validation.lignes, recuperer) if verifier else {}
    quand = maintenant or datetime.now(timezone.utc)
    for ligne in validation.lignes:
        repo.enregistrer_concurrence(engine, dict(
            code_naf=ligne["code_naf"], tache_id=ligne["tache_id"], naf_version=version, source=SOURCE_SESSION, statut="evalue", motif=ligne["note"],
            outils_json=ligne["outils"], service_local_json=ligne["service_local"], requetes_json=ligne["recherches"], evalue_le=quand,
        ))
    return validation, verifs


def metriques_concurrence(engine: Engine, maintenant: datetime | None = None) -> dict[str, Any]:
    """Lecture seule pour `app.metriques` : état du fournisseur, requêtes du mois contre le plafond, couples évalués par source."""
    quand = maintenant or datetime.now(timezone.utc)
    mois = quand.strftime("%Y-%m")
    evaluees = repo.dernieres_concurrences(engine, referentiels.secteurs_tpe().naf_version)
    par_source: dict[str, int] = {}
    for ligne in evaluees.values():
        par_source[ligne["source"]] = par_source.get(ligne["source"], 0) + 1
    nb = repo.nombre_recherches_web(engine, mois)
    return {
        "fournisseur_web": etat_fournisseur().raison, "mois": mois, "recherches_web_ce_mois": nb, "plafond_mensuel": plafond_mensuel(),
        "cout_estime_eur_ce_mois": round(nb * float(cfg.concurrence()["recherche_web"]["cout_estime_eur_par_requete"]), 4),
        "couples_evalues": len(evaluees), "par_source": par_source,
        "avec_service_local": sum(1 for x in evaluees.values() if (x["service_local_json"] or {}).get("present")),
    }
