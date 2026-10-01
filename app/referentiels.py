"""Référentiels de la version 2 (sous-étape V2.1 de RADAR-V2.md).

Quatre fichiers de `config/` décrivent le terrain : secteurs de TPE/PME (codes NAF),
tâches automatisables, zone de prospection, déclencheurs réglementaires. Ce module les
charge avec une validation STRICTE : un champ inconnu, un doublon, un code mal formé,
une exclusion sans source ou un déclencheur sans URL officielle fait échouer le
chargement, avec la liste complète des erreurs (jamais la première seulement).

Aucun appel réseau ni modèle ici : lecture de fichiers locaux uniquement.
"""
from __future__ import annotations

import re
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

_RE_NAF = re.compile(r"^\d{2}\.\d{2}[A-Z]$")
_RE_IDENTIFIANT = re.compile(r"^[a-z][a-z0-9_]*$")
_RE_DEPARTEMENT = re.compile(r"^(\d{2}|2[AB]|\d{3})$")
_DOMAINES_OFFICIELS = (".gouv.fr", ".europa.eu", "urssaf.fr")
TOUS_SECTEURS = "*"


class ReferentielInvalide(ValueError):
    """Un référentiel est mal formé ; le message liste toutes les erreurs trouvées."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _url_officielle(valeur: str) -> str:
    analyse = urlparse(valeur)
    hote = (analyse.hostname or "").lower()
    if analyse.scheme != "https" or not hote:
        raise ValueError(f"URL https attendue, reçu : {valeur!r}")
    if not any(hote == d.lstrip(".") or hote.endswith(d) for d in _DOMAINES_OFFICIELS):
        raise ValueError(f"domaine non officiel ({hote}) ; acceptés : {', '.join(_DOMAINES_OFFICIELS)}")
    return valeur


# --------------------------------------------------------------------------------------
# Secteurs
# --------------------------------------------------------------------------------------

class Exclusion(_Strict):
    motif: Literal["reglementaire_lourd", "materiel_industriel", "code_naf_agence"]
    source: str


class SecteurTpe(_Strict):
    code: str
    libelle: str = Field(min_length=3)
    famille: str
    # V2.8 : ordre de couverture (1 = cartographie initiale, 2 puis 3 = régime quotidien) ; explicite sur chaque entrée, jamais de défaut.
    priorite: int = Field(ge=1, le=3)
    exclusion: Exclusion | None = None

    @field_validator("code")
    @classmethod
    def _code_naf(cls, v: str) -> str:
        if not _RE_NAF.match(v):
            raise ValueError(f"code NAF mal formé : {v!r} (attendu : 00.00A)")
        return v


class ReferentielSecteurs(_Strict):
    nomenclature: str
    naf_version: Literal["2", "2.1"]
    cree_le: date
    familles: tuple[str, ...]
    motifs_exclusion: tuple[str, ...]
    sources_exclusion: dict[str, str]
    secteurs: tuple[SecteurTpe, ...]

    @model_validator(mode="after")
    def _coherence(self) -> "ReferentielSecteurs":
        erreurs: list[str] = []
        codes = [s.code for s in self.secteurs]
        for code in sorted({c for c in codes if codes.count(c) > 1}):
            erreurs.append(f"code NAF en double : {code}")
        for s in self.secteurs:
            if s.famille not in self.familles:
                erreurs.append(f"{s.code} : famille inconnue {s.famille!r}")
            if s.exclusion is not None:
                if s.exclusion.source not in self.sources_exclusion:
                    erreurs.append(f"{s.code} : source d'exclusion inconnue {s.exclusion.source!r}")
                if s.exclusion.motif not in self.motifs_exclusion:
                    erreurs.append(f"{s.code} : motif d'exclusion non déclaré {s.exclusion.motif!r}")
        for cle, texte in self.sources_exclusion.items():
            if not texte.strip():
                erreurs.append(f"source d'exclusion vide : {cle}")
        if erreurs:
            raise ValueError("; ".join(erreurs))
        return self

    def par_code(self) -> dict[str, SecteurTpe]:
        return {s.code: s for s in self.secteurs}

    def non_exclus(self) -> tuple[SecteurTpe, ...]:
        return tuple(s for s in self.secteurs if s.exclusion is None)

    def codes_par_priorite(self, priorite: int) -> tuple[str, ...]:
        """Codes NAF RETENUS (non exclus) d'une priorité, dans l'ordre du fichier."""
        return tuple(s.code for s in self.secteurs if s.exclusion is None and s.priorite == priorite)

    def codes_dans_l_ordre(self) -> tuple[str, ...]:
        """Tous les codes retenus : priorité 1, puis 2, puis 3 (l'ordre de couverture du régime quotidien)."""
        return tuple(c for p in (1, 2, 3) for c in self.codes_par_priorite(p))


# --------------------------------------------------------------------------------------
# Tâches
# --------------------------------------------------------------------------------------

class Tache(_Strict):
    id: str
    libelle: str = Field(min_length=3)
    famille: str
    donnees_sensibles_patients: bool = False
    mots_cles: tuple[str, ...] = Field(min_length=3)

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not _RE_IDENTIFIANT.match(v):
            raise ValueError(f"identifiant mal formé : {v!r} (minuscules, chiffres, tirets bas)")
        return v

    @field_validator("mots_cles")
    @classmethod
    def _mots(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for mot in v:
            if not mot.strip() or mot != mot.strip():
                raise ValueError(f"mot-clé vide ou avec espaces en bord : {mot!r}")
        normalises = [m.lower() for m in v]
        if len(set(normalises)) != len(normalises):
            raise ValueError("mots-clés en double dans une même tâche")
        return v


MOTIF_HDS = "HDS"


class RegleDonneesSensibles(_Strict):
    """Pour les secteurs de santé, les tâches `donnees_sensibles_patients` sont exclues (motif HDS)."""
    motif: Literal["HDS"]
    source: str = Field(min_length=10)
    familles_secteurs: tuple[str, ...] = Field(min_length=1)
    secteurs_hors_regle: tuple[str, ...] = ()

    @field_validator("secteurs_hors_regle")
    @classmethod
    def _codes(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for code in v:
            if not _RE_NAF.match(code):
                raise ValueError(f"code NAF mal formé : {code!r}")
        return v


class ReferentielTaches(_Strict):
    familles: tuple[str, ...]
    regle_donnees_sensibles_patients: RegleDonneesSensibles
    taches: tuple[Tache, ...]

    @model_validator(mode="after")
    def _coherence(self) -> "ReferentielTaches":
        erreurs: list[str] = []
        ids = [t.id for t in self.taches]
        for i in sorted({x for x in ids if ids.count(x) > 1}):
            erreurs.append(f"identifiant de tâche en double : {i}")
        vus: dict[str, str] = {}
        for t in self.taches:
            if t.famille not in self.familles:
                erreurs.append(f"{t.id} : famille inconnue {t.famille!r}")
            for mot in t.mots_cles:
                cle = mot.lower()
                if cle in vus and vus[cle] != t.id:
                    erreurs.append(f"mot-clé {mot!r} présent dans {vus[cle]} et {t.id}")
                vus.setdefault(cle, t.id)
        if not any(t.donnees_sensibles_patients for t in self.taches):
            erreurs.append("aucune tâche ne porte donnees_sensibles_patients : la règle HDS serait sans effet")
        if erreurs:
            raise ValueError("; ".join(erreurs))
        return self

    def par_id(self) -> dict[str, Tache]:
        return {t.id: t for t in self.taches}


# --------------------------------------------------------------------------------------
# Zone
# --------------------------------------------------------------------------------------

class Departement(_Strict):
    code: str
    nom: str = Field(min_length=2)

    @field_validator("code")
    @classmethod
    def _code(cls, v: str) -> str:
        if not _RE_DEPARTEMENT.match(v):
            raise ValueError(f"code de département mal formé : {v!r}")
        return v


class Centre(_Strict):
    nom: str
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class FranceEntiere(_Strict):
    perimetre: Literal["metropole"]
    note: str


class Zone(_Strict):
    centre: Centre
    rayon_km: float = Field(gt=0, le=500)
    coeur: tuple[Departement, ...] = Field(min_length=1)
    proximite: tuple[Departement, ...]
    nouvelle_aquitaine: tuple[Departement, ...]
    france_entiere: FranceEntiere

    @model_validator(mode="after")
    def _coherence(self) -> "Zone":
        erreurs: list[str] = []
        for nom_liste in ("coeur", "proximite", "nouvelle_aquitaine"):
            codes = [d.code for d in getattr(self, nom_liste)]
            for c in sorted({x for x in codes if codes.count(x) > 1}):
                erreurs.append(f"{nom_liste} : département en double {c}")
        codes_na = {d.code for d in self.nouvelle_aquitaine}
        coeur = {d.code for d in self.coeur}
        proximite = {d.code for d in self.proximite}
        if coeur & proximite:
            erreurs.append(f"départements à la fois en coeur et en proximité : {sorted(coeur & proximite)}")
        for c in sorted((coeur | proximite) - codes_na):
            erreurs.append(f"département {c} absent de la Nouvelle-Aquitaine")
        if erreurs:
            raise ValueError("; ".join(erreurs))
        return self

    def departements_zone(self) -> tuple[str, ...]:
        """Codes de département de la zone de prospection (coeur puis proximité)."""
        return tuple(d.code for d in self.coeur) + tuple(d.code for d in self.proximite)


# --------------------------------------------------------------------------------------
# Déclencheurs
# --------------------------------------------------------------------------------------

class Declencheur(_Strict):
    id: str
    libelle: str = Field(min_length=5)
    date: date
    precision_date: Literal["ferme", "a_confirmer"]
    secteurs: tuple[str, ...] = Field(min_length=1)
    taches: tuple[str, ...] = ()   # V2.5 : tâches touchées directement ; vide = toutes les tâches
    entreprises_concernees: str = Field(min_length=3)
    source_url: str
    verifie_le: date
    verification: Literal["page_lue", "extrait_de_recherche"]
    resume: str = Field(min_length=10)

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not _RE_IDENTIFIANT.match(v):
            raise ValueError(f"identifiant mal formé : {v!r}")
        return v

    @field_validator("source_url")
    @classmethod
    def _url(cls, v: str) -> str:
        return _url_officielle(v)

    @field_validator("taches")
    @classmethod
    def _taches(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for t in v:
            if not _RE_IDENTIFIANT.match(t):
                raise ValueError(f"identifiant de tâche mal formé : {t!r}")
        if len(set(v)) != len(v):
            raise ValueError("tâche en double dans `taches`")
        return v

    @field_validator("secteurs")
    @classmethod
    def _secteurs(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if TOUS_SECTEURS in v and len(v) > 1:
            raise ValueError('"*" (tous secteurs) ne se combine avec aucun code')
        for code in v:
            if code != TOUS_SECTEURS and not _RE_NAF.match(code):
                raise ValueError(f"code NAF mal formé : {code!r}")
        if len(set(v)) != len(v):
            raise ValueError("code en double dans `secteurs`")
        return v

    @model_validator(mode="after")
    def _dates(self) -> "Declencheur":
        if self.verifie_le < date(2026, 1, 1):
            raise ValueError("date de vérification antérieure à 2026 : à refaire")
        return self


class ASurveiller(_Strict):
    id: str
    libelle: str = Field(min_length=5)
    motif: str = Field(min_length=10)
    source_url: str
    verifie_le: date

    @field_validator("source_url")
    @classmethod
    def _url(cls, v: str) -> str:
        return _url_officielle(v)


class RattachementTaches(_Strict):
    """Statut du rattachement déclencheurs <-> tâches (V2.5) : décision de Mathéo du 2026-10-01, provisoire jusqu'à V2.9."""
    statut: Literal["provisoire", "valide"]
    decide_le: date
    a_relire_a: str = Field(min_length=2)
    note: str = Field(min_length=10)


class ReferentielDeclencheurs(_Strict):
    rafraichi_le: date
    rattachement_taches: RattachementTaches
    declencheurs: tuple[Declencheur, ...]
    a_surveiller: tuple[ASurveiller, ...] = ()

    @model_validator(mode="after")
    def _coherence(self) -> "ReferentielDeclencheurs":
        ids = [d.id for d in self.declencheurs] + [a.id for a in self.a_surveiller]
        erreurs = [f"identifiant en double : {i}" for i in sorted({x for x in ids if ids.count(x) > 1})]
        if erreurs:
            raise ValueError("; ".join(erreurs))
        return self


# --------------------------------------------------------------------------------------
# Chargement
# --------------------------------------------------------------------------------------

def _lire(dossier: Path, nom: str) -> dict[str, Any]:
    chemin = dossier / nom
    try:
        with chemin.open("r", encoding="utf-8") as f:
            brut = yaml.safe_load(f)
    except FileNotFoundError as exc:
        raise ReferentielInvalide(f"{nom} : fichier absent ({chemin})") from exc
    except yaml.YAMLError as exc:
        raise ReferentielInvalide(f"{nom} : YAML illisible : {exc}") from exc
    if not isinstance(brut, dict):
        raise ReferentielInvalide(f"{nom} : la racine doit être un dictionnaire")
    return brut


def _valider(modele: type[BaseModel], nom: str, brut: dict[str, Any]):
    try:
        return modele.model_validate(brut)
    except ValidationError as exc:
        lignes = [f"{nom} : {'.'.join(str(p) for p in e['loc'])} -> {e['msg']}" for e in exc.errors()]
        raise ReferentielInvalide("\n".join(lignes)) from exc


def charger_secteurs_tpe(dossier: Path = CONFIG_DIR) -> ReferentielSecteurs:
    return _valider(ReferentielSecteurs, "secteurs_tpe.yaml", _lire(dossier, "secteurs_tpe.yaml"))


def charger_taches(dossier: Path = CONFIG_DIR) -> ReferentielTaches:
    return _valider(ReferentielTaches, "taches.yaml", _lire(dossier, "taches.yaml"))


def charger_zone(dossier: Path = CONFIG_DIR) -> Zone:
    return _valider(Zone, "zone.yaml", _lire(dossier, "zone.yaml"))


def charger_declencheurs(dossier: Path = CONFIG_DIR) -> ReferentielDeclencheurs:
    return _valider(ReferentielDeclencheurs, "declencheurs.yaml", _lire(dossier, "declencheurs.yaml"))


def verifier_coherence(
    secteurs: ReferentielSecteurs,
    declencheurs: ReferentielDeclencheurs,
    taches: ReferentielTaches | None = None,
) -> None:
    """Contrôle croisé : tout code NAF cité par un déclencheur (ou par la règle HDS des tâches)
    existe dans les secteurs, et les familles de la règle HDS existent."""
    connus = {s.code for s in secteurs.secteurs}
    erreurs = []
    for d in declencheurs.declencheurs:
        for code in d.secteurs:
            if code != TOUS_SECTEURS and code not in connus:
                erreurs.append(f"déclencheur {d.id} : code NAF {code} absent de secteurs_tpe.yaml")
    if taches is not None:
        ids_taches = {t.id for t in taches.taches}
        for d in declencheurs.declencheurs:
            for t in d.taches:
                if t not in ids_taches:
                    erreurs.append(f"déclencheur {d.id} : tâche {t} absente de taches.yaml")
        regle = taches.regle_donnees_sensibles_patients
        for code in regle.secteurs_hors_regle:
            if code not in connus:
                erreurs.append(f"règle HDS : code NAF {code} absent de secteurs_tpe.yaml")
        for famille in regle.familles_secteurs:
            if famille not in secteurs.familles:
                erreurs.append(f"règle HDS : famille {famille!r} absente de secteurs_tpe.yaml")
    if erreurs:
        raise ReferentielInvalide("\n".join(erreurs))


def motif_exclusion_couple(
    code_naf: str,
    id_tache: str,
    secteurs: ReferentielSecteurs,
    taches: ReferentielTaches,
) -> str | None:
    """Motif d'exclusion du couple (secteur × tâche), ou None s'il est retenu.

    Ordre : le secteur lui-même (`reglementaire_lourd`, `materiel_industriel`, `code_naf_agence`), puis la règle
    de données de patients (« HDS »). Un code ou une tâche inconnus lèvent ReferentielInvalide.
    """
    secteur = secteurs.par_code().get(code_naf)
    if secteur is None:
        raise ReferentielInvalide(f"code NAF inconnu : {code_naf}")
    tache = taches.par_id().get(id_tache)
    if tache is None:
        raise ReferentielInvalide(f"tâche inconnue : {id_tache}")
    if secteur.exclusion is not None:
        return secteur.exclusion.motif
    regle = taches.regle_donnees_sensibles_patients
    if (tache.donnees_sensibles_patients
            and secteur.famille in regle.familles_secteurs
            and code_naf not in regle.secteurs_hors_regle):
        return regle.motif
    return None


@lru_cache(maxsize=1)
def secteurs_tpe() -> ReferentielSecteurs:
    return charger_secteurs_tpe()


@lru_cache(maxsize=1)
def taches() -> ReferentielTaches:
    return charger_taches()


@lru_cache(maxsize=1)
def zone() -> Zone:
    return charger_zone()


@lru_cache(maxsize=1)
def declencheurs() -> ReferentielDeclencheurs:
    return charger_declencheurs()
