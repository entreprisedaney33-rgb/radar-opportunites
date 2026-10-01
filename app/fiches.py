"""Fiches « secteur x tâche » (V2.5, RADAR-V2.md) : Analyste, Critic, score et décision.

    python -m app.cli fiches [--max-fiches N] [--estimer]

Chaîne, pour chaque couple candidat (`app.selection_couples`) :
1. ANALYSTE (modèle approfondi) : reçoit les chiffres agrégés (jamais le texte des offres), les obligations du secteur
   et les établissements de la zone ; produit la fiche structurée. Chaque affirmation chiffrée cite un bloc de données
   et le CODE vérifie que ses nombres figurent dans ce bloc (sinon l'affirmation est jetée).
2. SCORE (`app.scoring_v2`, code) : 5 critères, brut et prudent, règle dans SCORING-V2.md.
3. CRITIC (modèle approfondi) : n'a JAMAIS accès au score ; objections typées {tache_non_automatisable, deja_equipe,
   reglementaire, cible_injoignable, concurrence_locale, chiffres_fragiles}, avec une gravité. Il ne tourne pas si la
   porte d'accessibilité est déjà fermée (rien à contester).
4. DÉCISION (code) : `eligible_prospection` / `a_verifier` (preuves manquantes) / `exclue` (motif).

Jamais de fiche par repli : sans sortie valide de l'Analyste, aucune ligne n'est écrite et le couple reste en attente.
Un échec systémique (disjoncteur) ou un budget épuisé arrête la passe.

Rafraîchissement : une fiche est recalculée au bout de `rafraichissement.jours`, ou quand un chiffre d'entrée a bougé de
plus de `variation_agregats_pct` %, ou quand la version du score ou du prompt a changé.
"""
from __future__ import annotations

import logging
import os
import re
from urllib.parse import urlparse
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy.engine import Engine

from app import config as cfg
from app import referentiels
from app.concurrence import concurrence_pour_score
from app.adapters.model_client import AccesModeleIndisponible, DisjoncteurAPIOuvert, ModelClient, estimer_cout_eur
from app.faisabilite import ResultatAccessibilite, evaluer_accessibilite
from app.models_schemas import DelaiPremierRevenu, FaisabiliteSortie
from app.pipeline.budget import BudgetDepasse, BudgetTracker
from app.roles.prompts_communs import RAPPEL_SECURITE
from app.scoring_v2 import ResultatScore, calculer_score, declencheurs_du_secteur
from app.selection_couples import Candidat, selectionner
from app.storage import repo

logger = logging.getLogger(__name__)

ROLE_ANALYSTE = "analyste_fiche"
ROLE_CRITIC = "critic_fiche"
ROLES_ENVELOPPE = ("etiqueteur", ROLE_ANALYSTE, ROLE_CRITIC)  # tout ce que la première cartographie dépense en modèle
VERSION_PROMPT = "fiches-v1"
VARIABLE_ENVELOPPE = "RADAR_ENVELOPPE_INITIALE_EUR"
DECISION_ELIGIBLE, DECISION_A_VERIFIER, DECISION_EXCLUE = "eligible_prospection", "a_verifier", "exclue"

TypeObjection = Literal["tache_non_automatisable", "deja_equipe", "reglementaire", "cible_injoignable", "concurrence_locale", "chiffres_fragiles"]


# ------------------------------------------------------------------ schémas des sorties modèle -----

class AffirmationChiffree(BaseModel):
    texte: str
    source_id: str


class HypothesePrix(BaseModel):
    type: Literal["hypothese"] = "hypothese"
    montant_min_eur: float
    montant_max_eur: float
    periodicite: Literal["par_mois", "par_an", "unique"]
    justification: str


class FicheAnalyste(BaseModel):
    service_ia_propose: str
    ce_quil_remplace: str
    affirmations: list[AffirmationChiffree] = Field(default_factory=list)
    hypothese_prix: HypothesePrix
    faisabilite: FaisabiliteSortie
    # Mots, pas des chiffres : le filet de `model_client._normaliser_sortie_outil` décode en JSON toute chaîne d'un champ qui n'est pas
    # annoté `str` -- « "2" » deviendrait l'entier 2 et ferait échouer la validation d'une réponse pourtant correcte.
    personnes_necessaires: Literal["une", "deux", "plus_de_deux"]
    prochain_test: str
    preuves_manquantes: list[str] = Field(default_factory=list)


class ObjectionTypee(BaseModel):
    type: TypeObjection
    gravite: Literal["structurelle", "a_verifier"]
    texte: str
    # Uniquement pour `concurrence_locale` : un service local NOMMÉ et sa source (URL). Sans les deux, l'objection n'est jamais structurelle.
    service_local_nomme: str | None = None
    service_local_source: str | None = None


class CritiqueFiche(BaseModel):
    objections: list[ObjectionTypee] = Field(default_factory=list)
    decision: Literal["rejeter", "a_verifier", "poursuivre"]
    motif: str


# ------------------------------------------------------------------ blocs de données (ce que les modèles voient) -----

def _pct(x: float) -> str:
    return f"{100 * x:.1f}".replace(".", ",") + " %"


def construire_blocs(
    candidat: Candidat, declencheurs: list[referentiels.Declencheur], *, entreprises_zone: dict | None = None,
) -> dict[str, str]:
    """`source_id` -> texte du bloc. C'est EXACTEMENT ce que le modèle lit, et la vérification des nombres s'y réfère."""
    secteurs, taches = referentiels.secteurs_tpe().par_code(), referentiels.taches().par_id()
    a = candidat.agregat
    secteur, tache = secteurs[a["code_naf"]], taches[a["tache_id"]]
    salaire = (f"{round(a['salaire_median_annuel_eur'])} € par an (médiane de {a['nb_salaires']} offres avec salaire lisible)"
               if a.get("salaire_median_annuel_eur") is not None else "aucun salaire lisible")
    tendance = (f"{a['tendance_3_mois_pct']} %" if a["tendance_statut"] == "calculee" and a["tendance_3_mois_pct"] is not None
                else f"non calculée ({a['tendance_statut']})")
    blocs = {
        "agregat": (
            f"Secteur : {secteur.libelle} (code NAF {a['code_naf']}). Tâche : {tache.libelle}.\n"
            f"Stock d'offres d'emploi actives créées sur les {a['fenetre_jours']} derniers jours : {a['nb_offres_secteur_total']} offres dans le secteur ; "
            f"{a['nb_offres_secteur']} d'entre elles ont été étiquetées (échantillon, couverture {_pct(a['couverture_etiquetage'])}).\n"
            f"Part des offres du secteur qui mentionnent la tâche : {_pct(a['part_offres_tache'])} "
            f"(intervalle de confiance à 95 % : de {_pct(a['part_ic95_bas'])} à {_pct(a['part_ic95_haut'])}).\n"
            f"Nombre d'offres mentionnant la tâche, estimé sur le stock entier : {a['nb_offres_tache_estime']} "
            f"(au pire {candidat.offres_estimees_bas}), dont {a['nb_offres_tache_zone_estime']} dans les départements de la zone.\n"
            f"Salaire : {salaire}.\n"
            f"Tendance sur 3 mois : {tendance}. Durée réelle d'accumulation de la collecte : {a['accumulation_jours']} jours."
        ),
        "etablissements": (
            f"Établissements actifs du secteur listés à moins de {referentiels.zone().rayon_km:.0f} km de Bordeaux : {candidat.etablissements_rayon}"
            + (f". Entreprises actives du secteur dans les départements de la zone : {entreprises_zone['entreprises']}"
               + (" (au moins, comptage plafonné par la source)" if entreprises_zone["plafonne"] else "") if entreprises_zone else "")
            + "."
        ),
    }
    for d in declencheurs:
        blocs[f"declencheur:{d.id}"] = (
            f"{d.libelle}. Date : {d.date.isoformat()} (date {'ferme' if d.precision_date == 'ferme' else 'à confirmer'}). "
            f"Concerne : {d.entreprises_concernees}. Source officielle : {d.source_url}."
        )
    return blocs


_RE_NOMBRE = re.compile(r"(?<![\w])\d+(?:[   ]\d{3})*(?:[.,]\d+)?")


def nombres(texte: str) -> set[float]:
    """Tous les nombres d'un texte, en valeur (« 1 180 » -> 1180 ; « 12,5 » -> 12.5 ; une date AAAA-MM-JJ donne 3 nombres)."""
    trouves: set[float] = set()
    for brut in _RE_NOMBRE.findall(texte):
        trouves.add(float(re.sub(r"[   ]", "", brut).replace(",", ".")))
    return trouves


def verifier_affirmations(affirmations: list[AffirmationChiffree], blocs: dict[str, str]) -> tuple[list[AffirmationChiffree], list[dict]]:
    """(retenues, rejetées avec motif). Retenue = source connue ET chaque nombre du texte figure dans CE bloc."""
    retenues: list[AffirmationChiffree] = []
    rejetees: list[dict] = []
    for a in affirmations:
        bloc = blocs.get(a.source_id)
        if bloc is None:
            rejetees.append({"texte": a.texte, "source_id": a.source_id, "motif": "source inconnue"})
            continue
        etrangers = sorted(n for n in nombres(a.texte) if n not in nombres(bloc))
        if etrangers:
            rejetees.append({"texte": a.texte, "source_id": a.source_id, "motif": f"nombre(s) absent(s) du bloc cité : {etrangers}"})
        else:
            retenues.append(a)
    return retenues, rejetees


# ------------------------------------------------------------------ prompts -----

def prompt_systeme_analyste() -> str:
    return (
        "Tu es l'Analyste d'un radar de demande pour deux associés basés en Gironde, qui veulent vendre un service IA à des "
        "TPE/PME françaises. On te donne, pour UN couple (secteur, tâche), des chiffres mesurés sur les offres d'emploi "
        "françaises, des obligations réglementaires et le nombre d'établissements à proximité. Tu rédiges la fiche en français.\n"
        f"{RAPPEL_SECURITE}\n\n"
        "Règles :\n"
        "- `affirmations` : les constats chiffrés. Chacune cite `source_id` = l'identifiant d'UN bloc fourni (`agregat`, "
        "`etablissements`, ou `declencheur:...`) et ne contient QUE des nombres qui figurent dans ce bloc, recopiés tels quels. "
        "Un nombre qui n'y figure pas fait jeter l'affirmation. Pas de calcul, pas d'arrondi, pas de nombre venu d'ailleurs.\n"
        "- Les parts sont mesurées sur un échantillon : cite l'intervalle de confiance, pas seulement la valeur centrale.\n"
        "- `service_ia_propose`, `ce_quil_remplace`, `hypothese_prix`, `faisabilite`, `personnes_necessaires` sont des HYPOTHÈSES de ta part, "
        "pas des faits : sois concret et réaliste (deux personnes, compétences IA intermédiaires, quelques milliers d'euros, "
        "livraison par abonnement mensuel à des petites entreprises). Ne propose aucun service qui exige un agrément ou du matériel industriel.\n"
        "- La concurrence n'est PAS évaluée à ce stade : ne dis rien sur les outils ou prestataires existants, liste-le dans `preuves_manquantes`.\n"
        "- `prochain_test` : un test peu coûteux que les associés peuvent faire eux-mêmes, FORMULÉ COMME UNE QUESTION À POSER à un "
        "établissement de la liste (par exemple : « Qui s'occupe de X aujourd'hui, avec quel outil, et combien de temps par semaine cela "
        "représente-t-il ? »), sans promesse de résultat.\n"
        "- `preuves_manquantes` : ce qu'il faudrait savoir et que les données ne disent pas.\n"
        "- Sois CONCIS : chaque justification tient en une phrase de 25 mots au plus, `service_ia_propose` en deux phrases, "
        "6 affirmations au plus. Une réponse trop longue est coupée et perdue."
    )


def prompt_utilisateur_analyste(blocs: dict[str, str]) -> str:
    corps = "\n\n".join(f"[{cle}]\n{texte}" for cle, texte in blocs.items())
    return (
        "Voici les données mesurées pour le couple (secteur, tâche) à analyser (identifiants de blocs entre crochets). Rédige la fiche "
        "COMPLÈTE avec l'outil `repondre` : chaque champ texte doit être rédigé pour CE secteur et CETTE tâche, en phrases réelles.\n\n"
        f"{corps}"
    )


def prompt_systeme_critic() -> str:
    return (
        "Tu es le Critic d'un radar de demande : tu cherches ce qui est FAUX ou FRAGILE dans une fiche rédigée par l'Analyste. "
        "Tu n'as pas accès au score et tu n'en tiens aucun compte.\n"
        f"{RAPPEL_SECURITE}\n\n"
        "Tu reçois les blocs de données mesurés et la fiche. Relève des objections, chacune d'un type exact : "
        "`tache_non_automatisable` (la tâche ne se laisse pas confier à une IA), `deja_equipe` (les entreprises de ce secteur "
        "ont probablement déjà un outil ou un logiciel qui le fait), `reglementaire` (une barrière légale ou déontologique), "
        "`cible_injoignable` (on ne peut pas raisonnablement joindre ces établissements pour leur vendre), `concurrence_locale`, "
        "`chiffres_fragiles` (échantillon trop petit, intervalle trop large, chiffre mal interprété). "
        "`gravite` : `structurelle` si l'objection suffit à ruiner l'idée, `a_verifier` si elle demande un contrôle. Règles de gravité (le "
        "code les applique de toute façon) : seuls `tache_non_automatisable`, `reglementaire` et `cible_injoignable` peuvent être "
        "`structurelles` ; `deja_equipe` (les établissements ont peut-être déjà un outil : cela se VÉRIFIE auprès d'eux) et `chiffres_fragiles` "
        "sont toujours `a_verifier` ; `concurrence_locale` n'est `structurelle` que si tu NOMMES un service local précis dans "
        "`service_local_nomme` ET donnes sa source (adresse https) dans `service_local_source` -- n'invente jamais un concurrent : la "
        "concurrence n'est pas évaluée dans les données fournies, laisse ces deux champs à null dans le doute. "
        "Sois précis et contradictoire : une objection générique sans lien avec CE secteur et CETTE tâche ne vaut rien. "
        "`decision` : `rejeter` (idée à abandonner : seulement avec une objection structurelle), `a_verifier` (à contrôler avant de prospecter), `poursuivre` (rien de bloquant). "
        "`motif` : une phrase. Aucune objection n'est un résultat acceptable si la fiche tient. Sois CONCIS : au plus 5 objections, chacune en "
        "deux phrases au plus."
    )


def prompt_utilisateur_critic(blocs: dict[str, str], fiche: FicheAnalyste, affirmations_retenues: list[AffirmationChiffree]) -> str:
    corps = "\n\n".join(f"[{cle}]\n{texte}" for cle, texte in blocs.items())
    constats = "\n".join(f"- ({a.source_id}) {a.texte}" for a in affirmations_retenues) or "(aucun constat chiffré retenu)"
    return (
        f"Blocs de données mesurés :\n\n{corps}\n\n"
        f"Fiche de l'Analyste :\n"
        f"Service proposé : {fiche.service_ia_propose}\n"
        f"Ce qu'il remplace : {fiche.ce_quil_remplace}\n"
        f"Prix envisagé (hypothèse) : {fiche.hypothese_prix.montant_min_eur:g} à {fiche.hypothese_prix.montant_max_eur:g} € "
        f"{fiche.hypothese_prix.periodicite.replace('_', ' ')}\n"
        f"Personnes nécessaires (hypothèse) : {fiche.personnes_necessaires}\n"
        f"Prochain test : {fiche.prochain_test}\n"
        f"Constats chiffrés retenus :\n{constats}\n\n"
        "Rédige ta critique COMPLÈTE avec l'outil `repondre` : objections typées et motif rédigés pour CE secteur et CETTE tâche."
    )


# ------------------------------------------------------------------ gravité retenue des objections (par le code) -----

def source_valide(source: str | None) -> bool:
    """Une source de concurrent n'est acceptée que si c'est une adresse https avec un nom de domaine (jamais « selon mes connaissances »)."""
    if not source:
        return False
    analyse = urlparse(source.strip())
    return analyse.scheme == "https" and bool(analyse.hostname) and "." in analyse.hostname


def gravite_retenue(objection: ObjectionTypee, reglages: dict | None = None) -> str:
    """Gravité que le CODE retient, quelle que soit celle écrite par le Critic (décision de Mathéo du 2026-10-01) :
    - `tache_non_automatisable`, `reglementaire`, `cible_injoignable` : celle du Critic (seuls types structurels) ;
    - `concurrence_locale` : structurelle SEULEMENT si le Critic nomme un service local ET donne sa source (https) ;
    - `deja_equipe`, `chiffres_fragiles` : jamais structurelles -- « déjà équipés » envoie la fiche à vérifier auprès du client."""
    fermes = (reglages or cfg.fiches())["decision"]["objections_fermes"]
    if objection.type in fermes:
        return objection.gravite
    if objection.type == "concurrence_locale":
        nomme = bool((objection.service_local_nomme or "").strip())
        return "structurelle" if objection.gravite == "structurelle" and nomme and source_valide(objection.service_local_source) else "a_verifier"
    return "a_verifier"


def question_client(libelle_tache: str) -> str:
    """Prochain test quand les établissements sont probablement déjà équipés : une question à leur poser, jamais une affirmation."""
    return (f"Qui s'occupe aujourd'hui de « {libelle_tache} » dans votre établissement, avec quel outil, "
            "et combien de temps par semaine cela représente-t-il ?")


# ------------------------------------------------------------------ décision (par le code) -----

def decider(
    score: ResultatScore, critique: CritiqueFiche | None, acces: ResultatAccessibilite | None, personnes: str,
    etablissements_rayon: int, reglages: dict | None = None, libelle_tache: str | None = None,
    service_local: dict | None = None,
) -> tuple[str, list[str]]:
    """(décision, motifs). Exclue : porte d'accessibilité fermée, plus de deux personnes, objection structurelle d'un type ferme
    (`tache_non_automatisable`, `reglementaire`, `cible_injoignable`), ou rejet du Critic APPUYÉ par une telle objection. Éligible : score
    prudent >= seuil, aucune objection structurelle retenue, liste de prospection assez longue, Critic répondu, et AUCUNE objection
    « déjà équipés », et AUCUN service local établi (`service_local`, V2.6 : décision de Mathéo du 2026-10-01 -- la fiche part « à vérifier »
    avec le nom du prestataire et sa source). Sinon à vérifier. Les gravités sont celles que le CODE retient (`gravite_retenue`), pas celles écrites par le Critic."""
    r = reglages or cfg.fiches()
    d = r["decision"]
    objections = critique.objections if critique else []
    structurelles = [o for o in objections if gravite_retenue(o, r) == "structurelle"]
    fermes = [o for o in structurelles if o.type in d["objections_fermes"]]
    exclusions: list[str] = []
    if acces is not None and acces.accessible_solo is False:
        exclusions.append(f"porte d'accessibilité fermée : {acces.motif_exclusion}")
    if personnes == "plus_de_deux":
        exclusions.append("plus de deux personnes nécessaires")
    for o in fermes:
        exclusions.append(f"objection structurelle du Critic ({o.type}) : {o.texte}")
    if critique is not None and critique.decision == "rejeter" and fermes:
        exclusions.append(f"le Critic recommande de rejeter : {critique.motif}")
    if exclusions:
        return DECISION_EXCLUE, exclusions

    manquants: list[str] = list(score.preuves_manquantes)
    if score.score_prudent < d["score_prudent_min"]:
        manquants.append(f"score prudent {score.score_prudent} < {d['score_prudent_min']}")
    if etablissements_rayon < d["prospection_min"]:
        manquants.append(f"liste de prospection de {etablissements_rayon} établissements < {d['prospection_min']}")
    manquants += [f"objection structurelle à lever ({o.type}) : {o.texte}" for o in structurelles]
    deja_equipes = [o for o in objections if o.type == "deja_equipe"]
    if deja_equipes:
        manquants.append(f"déjà équipés (à vérifier auprès du client) : {deja_equipes[0].texte}")
        if libelle_tache:
            manquants.append(f"prochain test, une question à poser : {question_client(libelle_tache)}")
    local = service_local if service_local and service_local.get("present") else None
    if local:
        manquants.append(f"service local identifié : {local.get('nom')} (source : {local.get('url')}) -- à vérifier avant de prospecter")
    if critique is not None and critique.decision == "rejeter":
        manquants.append(f"le Critic recommande de rejeter, sans objection structurelle retenue : {critique.motif}")
    if critique is None:
        manquants.append("Critic non passé")
    seuil_ok = score.score_prudent >= d["score_prudent_min"] and etablissements_rayon >= d["prospection_min"] and not structurelles
    if seuil_ok and critique is not None and not deja_equipes and not local and critique.decision != "rejeter":
        return DECISION_ELIGIBLE, []
    return DECISION_A_VERIFIER, manquants


# ------------------------------------------------------------------ rafraîchissement -----

def instantane_agregats(candidat: Candidat) -> dict[str, Any]:
    a = candidat.agregat
    return {
        "nb_offres_tache_estime": a["nb_offres_tache_estime"], "part_offres_tache": a["part_offres_tache"],
        "part_ic95_bas": a["part_ic95_bas"], "part_ic95_haut": a["part_ic95_haut"],
        "nb_offres_tache_zone_estime": a["nb_offres_tache_zone_estime"], "nb_offres_secteur_total": a["nb_offres_secteur_total"],
        "salaire_median_annuel_eur": a.get("salaire_median_annuel_eur"), "accumulation_jours": a["accumulation_jours"],
        "tendance_3_mois_pct": a["tendance_3_mois_pct"], "tendance_statut": a["tendance_statut"],
        "etablissements_rayon": candidat.etablissements_rayon,
    }


def variation_relative(avant: float | None, apres: float | None) -> float:
    """|apres - avant| / |avant| ; 0 si les deux sont nuls ou absents, 1 si on passe de rien (0/None) à quelque chose."""
    a, b = (avant or 0.0), (apres or 0.0)
    if a == 0 and b == 0:
        return 0.0
    if a == 0:
        return 1.0
    return abs(b - a) / abs(a)


CHAMPS_SURVEILLES = ("nb_offres_tache_estime", "part_offres_tache", "nb_offres_tache_zone_estime", "etablissements_rayon", "nb_offres_secteur_total")


def raison_de_recalcul(candidat: Candidat, derniere: dict | None, maintenant: datetime, reglages: dict | None = None,
                       concurrence_evaluee_le: datetime | None = None) -> str | None:
    """`None` si la fiche est à jour ; sinon la raison (jamais calculée, trop ancienne, chiffres bougés, version changée, concurrence évaluée
    depuis le dernier calcul : V2.6, sans changer la version du score -- les fiches sans concurrence évaluée n'ont aucune raison d'être refaites)."""
    r = (reglages or cfg.fiches())
    if derniere is None:
        return "jamais calculée"
    if derniere["version_score"] != str(r["version"]) or derniere["version_prompt"] != VERSION_PROMPT:
        return "version du score ou du prompt changée"
    calcule = derniere["calcule_le"]
    calcule = calcule if calcule.tzinfo else calcule.replace(tzinfo=timezone.utc)
    if concurrence_evaluee_le is not None:
        evaluee = concurrence_evaluee_le if concurrence_evaluee_le.tzinfo else concurrence_evaluee_le.replace(tzinfo=timezone.utc)
        if evaluee > calcule:
            return "concurrence évaluée depuis le dernier calcul (V2.6)"
    if maintenant - calcule >= timedelta(days=int(r["rafraichissement"]["jours"])):
        return f"calculée il y a {(maintenant - calcule).days} jours"
    seuil = float(r["rafraichissement"]["variation_agregats_pct"]) / 100
    avant, apres = derniere["agregats_json"], instantane_agregats(candidat)
    for champ in CHAMPS_SURVEILLES:
        if variation_relative(avant.get(champ), apres.get(champ)) > seuil:
            return f"{champ} : {avant.get(champ)} -> {apres.get(champ)} (> {r['rafraichissement']['variation_agregats_pct']} %)"
    return None


# ------------------------------------------------------------------ budget et estimation -----

def enveloppe_initiale_eur() -> float | None:
    from app.etiquetage import enveloppe_initiale_eur as _env

    return _env()


def estimer_cout_fiche(modele: str | None = None) -> float:
    """Coût estimé d'UNE fiche (Analyste + Critic) : prompts de taille typique + sorties maximales raisonnables. Estimation,
    remplacée par la mesure réelle du test de fumée dans le Journal."""
    modele = modele or cfg.get_settings().model_approfondi
    r = cfg.fiches()["modeles"]
    analyste = estimer_cout_eur(modele, 1800, round(0.55 * r["max_tokens_analyste"]))
    critic = estimer_cout_eur(modele, 1800, round(0.5 * r["max_tokens_critic"]))
    return analyste + critic


# ------------------------------------------------------------------ garde-fou contre les sorties dégénérées -----

LONGUEURS_MINIMALES = {"service_ia_propose": 40, "ce_quil_remplace": 20, "prochain_test": 40}


def fiche_exploitable(fiche: FicheAnalyste, retenues: list[AffirmationChiffree]) -> str | None:
    """`None` si la fiche est exploitable ; sinon le motif. Constaté au test de fumée du 2026-10-01 : le modèle a renvoyé une fois
    « placeholder » dans tous les champs texte (3 appels de suite) -- une sortie conforme au schéma mais vide de sens. Une fiche doit
    avoir un service, un remplacement et un test réellement rédigés, et au moins UN constat chiffré dont les nombres sont vérifiés."""
    for champ, mini in LONGUEURS_MINIMALES.items():
        valeur = getattr(fiche, champ).strip()
        if len(valeur) < mini or valeur.lower() in {"placeholder", "n/a", "non précisé"}:
            return f"champ `{champ}` vide ou factice ({valeur[:30]!r})"
    if len(fiche.hypothese_prix.justification.strip()) < 20:
        return "justification du prix vide ou factice"
    if not retenues:
        return "aucun constat chiffré vérifié"
    return None


# ------------------------------------------------------------------ passe de production -----

@dataclass
class ResumeFiches:
    couples_evalues: int = 0
    candidats: int = 0
    rejets: int = 0
    a_produire: int = 0
    fiches_ecrites: int = 0
    par_decision: dict[str, int] = field(default_factory=dict)
    analyste_perdu: int = 0
    analyste_degrade: int = 0   # sortie conforme au schéma mais vide de sens, même après une relance : aucune fiche écrite
    critic_perdu: int = 0
    affirmations_proposees: int = 0
    affirmations_retenues: int = 0
    cout_eur: float = 0.0
    arret: str | None = None


def precribler(candidats: list[Candidat], aujourdhui: date, reglages: dict | None = None,
               concurrences: dict[tuple[str, str], dict] | None = None) -> tuple[list[Candidat], list]:
    """Écarte, AVANT tout appel au modèle, les couples qui ne pourraient pas être éligibles même avec une accessibilité parfaite :
    score prudent maximal atteignable (demande + proximité + déclencheur prudents + accessibilité au maximum, concurrence non évaluée
    = 0) < seuil de la décision. Pur code, aucun coût ; ces couples n'auront pas de fiche (V2.6 ajoutera la concurrence : la version
    du score sera changée pour les recalculer)."""
    from app.selection_couples import Rejet

    r = reglages or cfg.fiches()
    if not r["selection"].get("precriblage_score_maximal", True):
        return candidats, []
    declencheurs = referentiels.declencheurs().declencheurs
    retenus, ecartes = [], []
    for c in candidats:
        meilleur = calculer_score(c.agregat, etablissements_rayon=c.etablissements_rayon, declencheurs=declencheurs, aujourdhui=aujourdhui,
                                  acces=ResultatAccessibilite(True, None), delai=DelaiPremierRevenu.MOINS_DE_3_MOIS, reglages=r,
                                  concurrence=concurrence_pour_score((concurrences or {}).get(c.cle)))
        if meilleur.score_prudent < r["decision"]["score_prudent_min"]:
            ecartes.append(Rejet(c.agregat["code_naf"], c.agregat["tache_id"],
                                 [f"score prudent maximal atteignable {meilleur.score_prudent} < {r['decision']['score_prudent_min']} "
                                  "(même avec une accessibilité parfaite)"]))
        else:
            retenus.append(c)
    return retenus, ecartes


def plafonner_par_code(candidats: list[Candidat], reglages: dict | None = None) -> tuple[list[Candidat], list]:
    """Garde, pour chaque secteur, les `max_fiches_par_code` couples de plus forte demande (offres estimées) ; les autres sont écartés
    AVANT tout appel au modèle, avec leur raison. `None` : aucun plafond."""
    from app.selection_couples import Rejet

    maxi = (reglages or cfg.fiches())["selection"].get("max_fiches_par_code")
    if maxi is None:
        return candidats, []
    par_code: dict[str, int] = {}
    retenus, ecartes = [], []
    for c in sorted(candidats, key=lambda c: (-c.agregat["nb_offres_tache_estime"], c.cle)):
        n = par_code.get(c.agregat["code_naf"], 0)
        if n < maxi:
            par_code[c.agregat["code_naf"]] = n + 1
            retenus.append(c)
        else:
            ecartes.append(Rejet(c.agregat["code_naf"], c.agregat["tache_id"], [f"au-delà des {maxi} meilleurs couples du secteur (plafond de fiches par secteur)"]))
    return retenus, ecartes


def preparer(engine: Engine, *, maintenant: datetime, aujourdhui: date) -> tuple[list[tuple[Candidat, str]], list, ResumeFiches]:
    """(couples à produire avec leur raison, rejets, résumé initial) : lecture seule."""
    secteurs, zone = referentiels.secteurs_tpe(), referentiels.zone()
    agregats = repo.derniers_agregats(engine, secteurs.naf_version)
    rayon = repo.etablissements_dans_le_rayon_par_code(engine, secteurs.naf_version, rayon_km=zone.rayon_km)
    candidats, rejets = selectionner(agregats, rayon)
    concurrences = repo.dernieres_concurrences(engine, secteurs.naf_version)
    candidats, rejets_precriblage = precribler(candidats, aujourdhui, concurrences=concurrences)
    candidats, rejets_plafond = plafonner_par_code(candidats)
    rejets = rejets + rejets_precriblage + rejets_plafond
    existantes = repo.dernieres_fiches(engine, secteurs.naf_version)
    a_produire = [(c, raison) for c in candidats
                  if (raison := raison_de_recalcul(c, existantes.get(c.cle), maintenant,
                                                   concurrence_evaluee_le=(concurrences.get(c.cle) or {}).get("evalue_le")))]
    resume = ResumeFiches(couples_evalues=len(agregats), candidats=len(candidats), rejets=len(rejets), a_produire=len(a_produire))
    return a_produire, rejets, resume


def _critique_en_dict(critique: CritiqueFiche | None) -> dict | None:
    """La critique telle que le Critic l'a écrite, + la gravité RETENUE par le code pour chaque objection (`gravite_retenue`)."""
    if critique is None:
        return None
    contenu = critique.model_dump(mode="json")
    for brute, o in zip(contenu["objections"], critique.objections):
        brute["gravite_retenue"] = gravite_retenue(o)
    return contenu


ENVELOPPE_DE_L_ENVIRONNEMENT = object()  # valeur par défaut : lire RADAR_ENVELOPPE_INITIALE_EUR (comportement de V2.5 inchangé)


def produire_fiches(
    engine: Engine, *, max_fiches: int | None = None, client: ModelClient | None = None, maintenant: datetime | None = None,
    aujourdhui: date | None = None, ordre_codes: tuple[str, ...] | None = None, seulement_codes: tuple[str, ...] | None = None,
    enveloppe: Any = ENVELOPPE_DE_L_ENVIRONNEMENT, plafond_jour_eur: float | None = None,
) -> ResumeFiches:
    """Une passe. V2.8 (cycle du worker) : `seulement_codes` restreint aux secteurs d'une tranche de priorité ; `ordre_codes` traite les
    couples dans l'ordre de priorité des secteurs (puis dans l'ordre habituel) ; `enveloppe` (euros, ou `None` = régime de croisière)
    remplace la lecture de la variable d'environnement ; `plafond_jour_eur` abaisse le plafond du jour (régime de croisière)."""
    settings, reglages = cfg.get_settings(), cfg.fiches()
    quand = maintenant or datetime.now(timezone.utc)
    jour = aujourdhui or quand.date()
    secteurs = referentiels.secteurs_tpe()
    a_produire, _rejets, resume = preparer(engine, maintenant=quand, aujourdhui=jour)
    if seulement_codes is not None:
        a_produire = [(c, r) for c, r in a_produire if c.cle[0] in set(seulement_codes)]
    if ordre_codes is not None:
        rang = {code: i for i, code in enumerate(ordre_codes)}
        a_produire = sorted(a_produire, key=lambda cr: rang.get(cr[0].cle[0], len(rang)))  # tri stable : l'ordre habituel est conservé à rang égal
    limite = max_fiches if max_fiches is not None else int(reglages["modeles"]["max_fiches_par_passe"])
    a_produire = a_produire[:limite]
    resume.a_produire = len(a_produire)  # ce qui sera réellement traité dans CETTE passe
    if not a_produire:
        return resume
    if not settings.has_model_access and client is None:
        resume.arret = "ANTHROPIC_API_KEY absente : aucune fiche produite (jamais de fiche par repli)"
        return resume

    if enveloppe is ENVELOPPE_DE_L_ENVIRONNEMENT:
        enveloppe = enveloppe_initiale_eur()
    plafond = enveloppe if enveloppe is not None else float(reglages["modeles"]["budget_eur_par_jour"])
    if enveloppe is None and plafond_jour_eur is not None:
        plafond = min(plafond, float(plafond_jour_eur))
    run_id = repo.creer_run(engine, mode="fiches_v2", version_code=VERSION_PROMPT, version_config=str(reglages["version"]),
                            quotas={"plafond_eur": plafond, "enveloppe_initiale": enveloppe is not None, "max_fiches": limite})
    if client is None:
        client = ModelClient(settings, BudgetTracker(engine, run_id, plafond, plafond_appels_approfondis=0))
    depense_avant = sum(repo.cout_total_par_role(engine, r) for r in (ROLE_ANALYSTE, ROLE_CRITIC))
    declencheurs = referentiels.declencheurs().declencheurs
    entreprises = repo.entreprises_zone_par_code(engine, secteurs.naf_version, referentiels.zone().departements_zone())
    concurrences = repo.dernieres_concurrences(engine, secteurs.naf_version)
    modeles = reglages["modeles"]

    for candidat, raison in a_produire:
        if enveloppe is not None and sum(repo.cout_total_par_role(engine, r) for r in ROLES_ENVELOPPE) >= enveloppe:
            resume.arret = f"enveloppe de {enveloppe:.2f} € épuisée (étiquetage + fiches) : arrêt"
            break
        code, tache_id = candidat.cle
        datees = declencheurs_du_secteur(code, declencheurs, jour, reglages["score"]["declencheur"], tache_id)
        blocs = construire_blocs(candidat, datees, entreprises_zone=entreprises.get(code))
        fiche, retenues, rejetees, motif_degrade = None, [], [], None
        arret = False
        for essai in (1, 2):  # une seule relance si la sortie est dégénérée (voir `fiche_exploitable`)
            try:
                fiche = client.appeler_structure(
                    modele=settings.model_approfondi, prompt_systeme=prompt_systeme_analyste(), prompt_utilisateur=prompt_utilisateur_analyste(blocs),
                    schema=FicheAnalyste, version_prompt=VERSION_PROMPT, role=ROLE_ANALYSTE, max_tokens=int(modeles["max_tokens_analyste"]),
                    forcer_outil=False,  # un appel forcé donne parfois une réponse factice : voir ModelClient.appeler_structure
                )
            except BudgetDepasse as exc:
                resume.arret, arret = f"budget : {exc}", True
                break
            except DisjoncteurAPIOuvert as exc:
                resume.arret, arret = f"disjoncteur API ouvert : {exc}", True
                break
            except AccesModeleIndisponible as exc:
                resume.arret, arret = str(exc), True
                break
            if fiche is None:
                break  # appel perdu (déjà relancé par le client si le schéma était invalide)
            retenues, rejetees = verifier_affirmations(fiche.affirmations, blocs)
            resume.affirmations_proposees += len(fiche.affirmations)
            resume.affirmations_retenues += len(retenues)
            motif_degrade = fiche_exploitable(fiche, retenues)
            if motif_degrade is None:
                break
            logger.warning("Fiche %s/%s dégénérée (essai %d) : %s", code, tache_id, essai, motif_degrade)
        if arret:
            break
        if fiche is None:
            resume.analyste_perdu += 1  # aucune fiche par repli : le couple reste en attente
            continue
        if motif_degrade is not None:
            resume.analyste_degrade += 1  # idem : jamais de fiche vide de sens, le couple reste en attente
            continue
        acces = evaluer_accessibilite(fiche.faisabilite)
        score = calculer_score(candidat.agregat, etablissements_rayon=candidat.etablissements_rayon, declencheurs=declencheurs, aujourdhui=jour,
                               acces=acces, delai=fiche.faisabilite.delai_premier_revenu.valeur,
                               rattachement_provisoire=referentiels.declencheurs().rattachement_taches.statut == "provisoire",
                               concurrence=concurrence_pour_score(concurrences.get(candidat.cle)))
        critique, modele_critic = None, None
        porte_fermee = acces.accessible_solo is False or fiche.personnes_necessaires == "plus_de_deux"
        # Le Critic ne tourne que si la fiche PEUT devenir éligible : porte ouverte, score prudent suffisant, liste de prospection assez
        # longue. Sinon la décision sera « à vérifier » quoi qu'il dise, et ses ~0,016 € ne servent à rien.
        seuils = reglages["decision"]
        eligible_possible = score.score_prudent >= seuils["score_prudent_min"] and candidat.etablissements_rayon >= seuils["prospection_min"]
        if not porte_fermee and eligible_possible:
            try:
                critique = client.appeler_structure(
                    modele=settings.model_approfondi, prompt_systeme=prompt_systeme_critic(),
                    prompt_utilisateur=prompt_utilisateur_critic(blocs, fiche, retenues), schema=CritiqueFiche,
                    version_prompt=VERSION_PROMPT, role=ROLE_CRITIC, max_tokens=int(modeles["max_tokens_critic"]), forcer_outil=False,
                )
            except BudgetDepasse as exc:
                resume.arret = f"budget : {exc}"
                break
            except DisjoncteurAPIOuvert as exc:
                resume.arret = f"disjoncteur API ouvert : {exc}"
                break
            if critique is None:
                resume.critic_perdu += 1  # sans Critic valide la fiche n'est jamais « éligible » (voir `decider`)
            else:
                modele_critic = settings.model_approfondi
        libelle_tache = referentiels.taches().par_id()[tache_id].libelle
        decision, motifs = decider(score, critique, acces, fiche.personnes_necessaires, candidat.etablissements_rayon, libelle_tache=libelle_tache,
                                 service_local=(concurrences.get(candidat.cle) or {}).get("service_local_json"))
        contenu = fiche.model_dump(mode="json")
        if critique is not None and any(o.type == "deja_equipe" for o in critique.objections):
            contenu["prochain_test_a_poser_au_client"] = question_client(libelle_tache)
        contenu["affirmations"] = [a.model_dump() for a in retenues]
        contenu["affirmations_rejetees"] = rejetees
        repo.enregistrer_fiche(engine, dict(
            code_naf=code, tache_id=tache_id, naf_version=secteurs.naf_version, version_score=str(reglages["version"]), version_prompt=VERSION_PROMPT,
            decision=decision, motifs_json=motifs, score_brut=score.score_brut, score_prudent=score.score_prudent, score_json=score.en_dict(),
            agregats_json=instantane_agregats(candidat), fiche_json=contenu, critique_json=_critique_en_dict(critique),
            modele_analyste=settings.model_approfondi, modele_critic=modele_critic, calcule_le=quand,
        ))
        resume.fiches_ecrites += 1
        resume.par_decision[decision] = resume.par_decision.get(decision, 0) + 1

    resume.cout_eur = sum(repo.cout_total_par_role(engine, r) for r in (ROLE_ANALYSTE, ROLE_CRITIC)) - depense_avant
    systemique = bool(resume.arret and resume.arret.startswith(("disjoncteur", "budget", "enveloppe")))
    repo.terminer_run(engine, run_id, statut="interrompu" if systemique else "termine", couts={"fiches_eur": round(resume.cout_eur, 6)},
                      erreurs=[resume.arret] if resume.arret else [], resume={"fiches": resume.fiches_ecrites, "decisions": resume.par_decision})
    return resume
