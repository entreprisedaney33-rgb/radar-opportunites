"""Étiquetage des offres d'emploi par tâche (V2.4, RADAR-V2.md).

    python -m app.cli etiqueter [--max-offres N] [--sans-modele] [--estimer]

Deux étages, dans cet ordre, sur chaque offre :
1. ÉTAGE CODE (gratuit) : les mots-clés de `config/taches.yaml` sont cherchés dans l'intitulé et la description
   (sans accents ni casse, en mots entiers). Provenance `lexique` : c'est le filet.
2. ÉTAGE MODÈLE (le moins cher : `model_tri`) : sortie structurée stricte `{taches: [{tache, citation}]}`. Une tâche
   n'est RETENUE que si sa citation se retrouve TEXTUELLEMENT dans l'offre (même texte, à la casse, aux accents, aux
   apostrophes et aux espaces près) ; sinon elle est ignorée -- jamais écrite, jamais devinée. Provenance
   `citation_verifiee`.

Budget : rôle `etiqueteur`, compté dans le plafond journalier (2 €/jour, `config/etiquetage.yaml`) ; la première
cartographie utilise une enveloppe unique, `RADAR_ENVELOPPE_INITIALE_EUR` (jamais plus de 50 €), CUMULÉE sur toute
la durée de l'étiquetage. Un échec systémique du modèle (disjoncteur) arrête la passe : aucune étiquette n'est jamais
fabriquée par repli. Sans accès au modèle, seul le lexique tourne (statut `lexique_seul`) et les offres restent à
reprendre.

Les offres sont des données NON FIABLES (texte écrit par des tiers) : le prompt le dit au modèle, et la sortie est de
toute façon bornée par la vérification textuelle et par la liste fermée des tâches.
"""
from __future__ import annotations

import logging
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.engine import Engine

from app import config as cfg
from app import referentiels
from app.adapters.model_client import AccesModeleIndisponible, DisjoncteurAPIOuvert, ModelClient, estimer_cout_eur
from app.pipeline.budget import BudgetDepasse, BudgetTracker
from app.roles.prompts_communs import RAPPEL_SECURITE
from app.storage import repo

logger = logging.getLogger(__name__)

ROLE_ETIQUETEUR = "etiqueteur"
# V2.5 : l'enveloppe unique de la première cartographie couvre TOUT ce que le modèle coûte (étiquetage + fiches).
ROLES_ENVELOPPE = ("etiqueteur", "analyste_fiche", "critic_fiche")
VERSION_PROMPT = "etiqueteur-v1"
VARIABLE_ENVELOPPE = "RADAR_ENVELOPPE_INITIALE_EUR"
VARIABLE_MAX_OFFRES = "RADAR_ETIQ_MAX_OFFRES"
PROVENANCE_LEXIQUE = "lexique"
PROVENANCE_CITATION = "citation_verifiee"
CARACTERES_PAR_JETON = 3.4  # français courant : estimation brute du texte envoyé
# Calibrage MESURÉ au test de fumée réel du 2026-10-01 (17 offres, Haiku 4.5) : 2 468 jetons d'entrée réellement
# facturés en moyenne contre 1 700 estimés (la définition de l'outil et l'enveloppe de l'appel ne sont pas dans le
# texte envoyé) = x 1,452 ; sortie moyenne 106 jetons. À refaire si le prompt, le schéma ou le modèle changent.
FACTEUR_CALIBRAGE_ENTREE = 1.452
JETONS_SORTIE_MOYENS = 110
JETONS_FIXES_PAR_APPEL = 150  # enveloppe de l'appel (balises, outil) -- estimation


# ------------------------------------------------------------------ schéma de sortie du modèle -----

class TacheCitee(BaseModel):
    tache: str
    citation: str


class EtiquetageOffre(BaseModel):
    taches: list[TacheCitee] = Field(default_factory=list)


# ------------------------------------------------------------------ normalisation -----

_APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "´": "'", "`": "'", "ʼ": "'", "“": '"', "”": '"', "«": '"', "»": '"',
                              " ": " ", " ": " "})


def normaliser(texte: str | None) -> str:
    """Minuscules, sans accents, apostrophes et guillemets unifiés, espaces réduits. Sert aux DEUX comparaisons du
    module (mots-clés du lexique, citations du modèle) : la même règle des deux côtés, sinon rien ne se retrouve."""
    if not texte:
        return ""
    brut = unicodedata.normalize("NFKD", texte.translate(_APOSTROPHES))
    sans_accents = "".join(c for c in brut if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", sans_accents.casefold()).strip()


@lru_cache(maxsize=1)
def _motifs_lexique() -> tuple[tuple[str, str, re.Pattern[str]], ...]:
    """(id de tâche, mot-clé normalisé, motif en mots entiers) pour chaque mot-clé du lexique. Plus long d'abord :
    « relance des impayés » doit gagner sur « impayés » pour le mot-clé retenu comme preuve."""
    motifs = []
    for tache in referentiels.taches().taches:
        for mot in sorted(tache.mots_cles, key=lambda m: -len(m)):
            n = normaliser(mot)
            motifs.append((tache.id, n, re.compile(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])")))
    return tuple(motifs)


def etiqueter_par_lexique(intitule: str | None, description: str | None) -> list[dict]:
    """Étage code : une ligne par tâche trouvée, avec le mot-clé trouvé comme `citation`. Aucun appel externe."""
    texte = normaliser(f"{intitule or ''}\n{description or ''}")
    trouvees: dict[str, str] = {}
    for tache_id, mot, motif in _motifs_lexique():
        if tache_id not in trouvees and motif.search(texte):
            trouvees[tache_id] = mot
    return [{"tache_id": t, "provenance": PROVENANCE_LEXIQUE, "citation": mot} for t, mot in trouvees.items()]


# ------------------------------------------------------------------ étage modèle -----

def construire_prompt_systeme() -> str:
    taches = "\n".join(f"- {t.id} : {t.libelle}" for t in referentiels.taches().taches)
    reglages = cfg.etiquetage()
    return (
        "Tu es l'Étiqueteur d'un radar de demande : tu lis UNE offre d'emploi française et tu relèves lesquelles des "
        "tâches ci-dessous le poste confie réellement à la personne recrutée (ce qu'elle fera au quotidien), pas ce que "
        "l'entreprise vend ni ce qu'elle est.\n"
        f"{RAPPEL_SECURITE}\n\n"
        "Pour chaque tâche retenue, renvoie son identifiant EXACT (colonne de gauche) et une `citation` : un extrait "
        "COPIÉ MOT POUR MOT de l'offre qui montre cette tâche (entre "
        f"{reglages['citation_min_caracteres']} et {reglages['citation_max_caracteres']} caractères, sans résumer, sans "
        "reformuler, sans points de suspension, sans mélanger deux passages). Si tu ne trouves pas d'extrait exact, ne "
        "retiens pas la tâche : une tâche sans citation exacte sera jetée. Au plus "
        f"{reglages['max_taches_par_offre']} tâches ; liste vide si aucune ne convient.\n\n"
        f"Tâches possibles :\n{taches}"
    )


def construire_prompt_utilisateur(intitule: str | None, description: str | None) -> str:
    maxi = int(cfg.etiquetage()["description_max_caracteres"])
    return (
        f"Intitulé : {intitule or '(sans intitulé)'}\n"
        f"Description :\n{(description or '(vide)')[:maxi]}\n\n"
        "Réponds avec l'outil `repondre`."
    )


def citation_verifiee(citation: str, intitule: str | None, description: str | None) -> bool:
    """Vrai si `citation` est un extrait TEXTUEL de l'offre (à la normalisation près, voir `normaliser`) et de taille
    raisonnable. Les points de suspension sont refusés : ils signalent un montage de deux passages."""
    reglages = cfg.etiquetage()
    if "…" in citation or "..." in citation:
        return False
    n = normaliser(citation)
    if not (int(reglages["citation_min_caracteres"]) <= len(n) <= int(reglages["citation_max_caracteres"])):
        return False
    return n in normaliser(f"{intitule or ''}\n{description or ''}")


def filtrer_sortie_modele(sortie: EtiquetageOffre, intitule: str | None, description: str | None, *, modele: str) -> tuple[list[dict], int, int]:
    """(tâches retenues, nombre proposé, nombre vérifié). Retenue = tâche connue du lexique, citation textuellement dans
    l'offre, une seule ligne par tâche (la première citation vérifiée). Le reste est ignoré."""
    connues = {t.id for t in referentiels.taches().taches}
    maxi = int(cfg.etiquetage()["max_taches_par_offre"])
    retenues: dict[str, dict] = {}
    proposees = 0
    for item in sortie.taches[:maxi]:
        proposees += 1
        if item.tache not in connues or item.tache in retenues:
            continue
        if citation_verifiee(item.citation, intitule, description):
            retenues[item.tache] = {"tache_id": item.tache, "provenance": PROVENANCE_CITATION,
                                    "citation": item.citation.strip(), "modele": modele}
    return list(retenues.values()), proposees, len(retenues)


# ------------------------------------------------------------------ coût -----

def estimer_jetons_offre(intitule: str | None, description: str | None) -> tuple[int, int]:
    """(jetons d'entrée, jetons de sortie) estimés pour UNE offre : prompt système + offre ; sortie moyenne supposée
    mesurée au test de fumée (voir `JETONS_SORTIE_MOYENS`). Estimation calibrée, pas une facture."""
    entree_caracteres = len(construire_prompt_systeme()) + len(construire_prompt_utilisateur(intitule, description))
    entree = (entree_caracteres / CARACTERES_PAR_JETON + JETONS_FIXES_PAR_APPEL) * FACTEUR_CALIBRAGE_ENTREE
    return round(entree), JETONS_SORTIE_MOYENS


@dataclass
class Estimation:
    nb_offres: int
    jetons_entree_par_offre: float
    jetons_sortie_par_offre: float
    modele: str
    cout_par_offre_eur: float
    cout_total_eur: float


def estimer_cout(nb_offres: int, *, jetons_entree: float, jetons_sortie: float, modele: str | None = None) -> Estimation:
    """Pure : coût = nombre d'offres x coût d'un appel, aux tarifs de `config/tarifs.yaml`."""
    modele = modele or cfg.get_settings().model_tri
    par_offre = estimer_cout_eur(modele, round(jetons_entree), round(jetons_sortie))
    return Estimation(nb_offres, jetons_entree, jetons_sortie, modele, par_offre, par_offre * nb_offres)


def estimer_pour_base(engine: Engine) -> Estimation:
    """Estimation pour les offres de la base qui attendent encore le modèle (jamais passées, `lexique_seul` ou
    `echec_modele`) : jetons moyens calculés sur ces offres elles-mêmes. N'appelle rien, ne dépense rien."""
    secteurs = referentiels.secteurs_tpe()
    reglages = cfg.etiquetage()
    offres = repo.offres_a_etiqueter(
        engine, secteurs.naf_version, version=str(reglages["version"]), limite=10**9,
        departements_zone=referentiels.zone().departements_zone(), avec_modele=True,
        max_par_code=reglages.get("echantillon_max_par_code"),
        depuis=datetime.now(timezone.utc) - timedelta(days=int(reglages["fenetre_stock_jours"])),
        exclure_codes=tuple(s.code for s in secteurs.secteurs if s.exclusion),
    )
    if not offres:
        return estimer_cout(0, jetons_entree=0, jetons_sortie=0)
    jetons = [estimer_jetons_offre(o["intitule"], o["description"]) for o in offres]
    return estimer_cout(len(offres), jetons_entree=sum(j[0] for j in jetons) / len(jetons),
                        jetons_sortie=sum(j[1] for j in jetons) / len(jetons))


# ------------------------------------------------------------------ budget -----

def enveloppe_initiale_eur() -> float | None:
    """`None` si RADAR_ENVELOPPE_INITIALE_EUR n'est pas posée (régime de croisière). Refuse une valeur illisible,
    négative ou au-delà du maximum autorisé : jamais de repli silencieux sur une autre valeur."""
    brut = os.environ.get(VARIABLE_ENVELOPPE)
    if not brut:
        return None
    try:
        valeur = float(brut.replace(",", "."))
    except ValueError as exc:
        raise ValueError(f"{VARIABLE_ENVELOPPE} illisible : {brut!r}") from exc
    maxi = float(cfg.etiquetage()["enveloppe_max_eur"])
    if not 0 < valeur <= maxi:
        raise ValueError(f"{VARIABLE_ENVELOPPE}={valeur} hors de ]0 ; {maxi}] : refusé")
    return valeur


def max_offres_par_passe() -> int:
    brut = os.environ.get(VARIABLE_MAX_OFFRES)
    try:
        valeur = int(brut) if brut else int(cfg.etiquetage()["max_offres_par_passe"])
    except ValueError:
        valeur = int(cfg.etiquetage()["max_offres_par_passe"])
    return max(1, valeur)


# ------------------------------------------------------------------ passe d'étiquetage -----

@dataclass
class ResumeEtiquetage:
    offres_prevues: int = 0
    offres_traitees: int = 0
    statuts: dict[str, int] = field(default_factory=dict)
    taches_lexique: int = 0
    citations_proposees: int = 0
    citations_verifiees: int = 0
    appels_modele: int = 0
    cout_eur: float = 0.0
    arret: str | None = None


def _compter(resume: ResumeEtiquetage, statut: str) -> None:
    resume.statuts[statut] = resume.statuts.get(statut, 0) + 1


ENVELOPPE_DE_L_ENVIRONNEMENT = object()  # valeur par défaut : lire RADAR_ENVELOPPE_INITIALE_EUR (comportement de V2.4 inchangé)


def etiqueter_offres(
    engine: Engine, *, max_offres: int | None = None, avec_modele: bool = True, client: ModelClient | None = None,
    maintenant: datetime | None = None, codes: tuple[str, ...] | None = None, enveloppe: Any = ENVELOPPE_DE_L_ENVIRONNEMENT,
    plafond_jour_eur: float | None = None,
) -> ResumeEtiquetage:
    """Une passe. `client` : injectable pour les tests (jamais d'appel réel dans la suite par défaut).

    V2.8 (cycle du worker) : `codes` restreint la passe à une tranche de priorité ; `enveloppe` (euros, ou `None` = régime de croisière)
    remplace la lecture de la variable d'environnement ; `plafond_jour_eur` (régime de croisière) abaisse le plafond du jour -- le
    plafond réel reste le plus bas des deux, jamais au-dessus de config/etiquetage.yaml::budget_eur_par_jour."""
    settings = cfg.get_settings()
    reglages = cfg.etiquetage()
    secteurs = referentiels.secteurs_tpe()
    zone = referentiels.zone()
    version = str(reglages["version"])
    quand = maintenant or datetime.now(timezone.utc)
    limite = max_offres if max_offres is not None else max_offres_par_passe()
    if enveloppe is ENVELOPPE_DE_L_ENVIRONNEMENT:
        enveloppe = enveloppe_initiale_eur()
    plafond_eur = enveloppe if enveloppe is not None else float(reglages["budget_eur_par_jour"])
    if enveloppe is None and plafond_jour_eur is not None:
        plafond_eur = min(plafond_eur, float(plafond_jour_eur))

    offres = repo.offres_a_etiqueter(
        engine, secteurs.naf_version, version=version, limite=limite, departements_zone=zone.departements_zone(),
        avec_modele=avec_modele, max_par_code=reglages.get("echantillon_max_par_code"),
        depuis=quand - timedelta(days=int(reglages["fenetre_stock_jours"])),
        exclure_codes=tuple(s.code for s in secteurs.secteurs if s.exclusion), seulement_codes=codes,
    )
    resume = ResumeEtiquetage(offres_prevues=len(offres))
    if not offres:
        return resume

    run_id = repo.creer_run(engine, mode="etiquetage_v2", version_code=VERSION_PROMPT, version_config=version,
                            quotas={"plafond_eur": plafond_eur, "enveloppe_initiale": enveloppe is not None, "max_offres": limite})
    modele_ok = avec_modele and settings.has_model_access
    if avec_modele and not settings.has_model_access:
        resume.arret = "ANTHROPIC_API_KEY absente : lexique seul (les offres restent à reprendre par le modèle)"
    if modele_ok and client is None:
        client = ModelClient(settings, BudgetTracker(engine, run_id, plafond_eur, plafond_appels_approfondis=0))
    elif client is not None:
        modele_ok = avec_modele
    depense_avant = repo.cout_total_par_role(engine, ROLE_ETIQUETEUR)

    for offre in offres:
        id_offre, intitule, description = offre["id_offre"], offre["intitule"], offre["description"]
        taches = etiqueter_par_lexique(intitule, description)
        statut, modele, proposees, verifiees = "lexique_seul", None, 0, 0

        if modele_ok:
            if enveloppe is not None and repo.cout_total_par_roles(engine, ROLES_ENVELOPPE) >= enveloppe:
                resume.arret = f"enveloppe de {enveloppe:.2f} € épuisée (cumul étiquetage + fiches) : arrêt"
                modele_ok = False
            else:
                try:
                    sortie = client.appeler_structure(
                        modele=settings.model_tri, prompt_systeme=construire_prompt_systeme(),
                        prompt_utilisateur=construire_prompt_utilisateur(intitule, description), schema=EtiquetageOffre,
                        version_prompt=VERSION_PROMPT, role=ROLE_ETIQUETEUR, max_tokens=int(reglages["max_tokens_sortie"]),
                    )
                    resume.appels_modele += 1
                    if sortie is None:
                        statut = "echec_modele"  # appel perdu (sortie invalide après relance, ou erreur réseau)
                    else:
                        retenues, proposees, verifiees = filtrer_sortie_modele(sortie, intitule, description, modele=settings.model_tri)
                        taches += retenues
                        statut, modele = "ok", settings.model_tri
                except BudgetDepasse as exc:
                    resume.arret = f"budget : {exc}"
                    modele_ok = False
                except DisjoncteurAPIOuvert as exc:
                    resume.arret = f"disjoncteur API ouvert : {exc}"
                    modele_ok = False
                except AccesModeleIndisponible as exc:
                    resume.arret = str(exc)
                    modele_ok = False

        if statut == "lexique_seul" and offre.get("statut_precedent") == "echec_modele":
            statut = "echec_modele"  # une reprise qui n'a pas pu rappeler le modèle ne « guérit » rien
        repo.enregistrer_etiquetage(
            engine, id_offre=id_offre, statut=statut, version=version, modele=modele, taches=taches,
            nb_citations_proposees=proposees, nb_citations_verifiees=verifiees, maintenant=quand,
        )
        resume.offres_traitees += 1
        _compter(resume, statut)
        resume.taches_lexique += sum(1 for t in taches if t["provenance"] == PROVENANCE_LEXIQUE)
        resume.citations_proposees += proposees
        resume.citations_verifiees += verifiees

    resume.cout_eur = repo.cout_total_par_role(engine, ROLE_ETIQUETEUR) - depense_avant
    arret_systemique = bool(resume.arret and resume.arret.startswith(("disjoncteur", "budget", "enveloppe")))
    repo.terminer_run(engine, run_id, statut="interrompu" if arret_systemique else "termine",
                      couts={"etiqueteur_eur": round(resume.cout_eur, 6)},
                      erreurs=[resume.arret] if resume.arret else [],
                      resume={"offres": resume.offres_traitees, "statuts": resume.statuts})
    return resume
