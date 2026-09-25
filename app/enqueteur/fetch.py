"""Fetch, extraction et stockage des sources de l'Enquêteur (sous-étape 3.3
d'AMELIORATIONS.md) : pour chaque `ResultatRecherche` retenu
(`app.enqueteur.selection`), lit `robots.txt`, respecte un délai entre
fetchs, un timeout court et une taille maximale de page, extrait le texte
principal, puis stocke le résultat comme n'importe quelle autre source
(`app.storage.repo.upsert_source`) — jamais si la page est injoignable, vide
ou hors taille (point 2 du texte de 3.3).

Sous-étape 3.4 : `collecter_preuves` accepte désormais, en options,
`opportunity_id` et `budget` -- sans eux (comme avant), le comportement est
strictement inchangé (rien de rattaché, aucun compteur consommé). Avec eux
(`app.enqueteur.enqueteur.enqueter_opportunite`, seul appelant réel dans le
pipeline) : chaque page stockée est rattachée à l'opportunité
(`opportunity_evidence`, lien qui n'existait nulle part avant cette
sous-étape -- voir les docstrings de `app.enqueteur.selection`/
`app.enqueteur.gabarits`), jamais deux fois la même preuve sur reprise, et
chaque fetch consomme le compteur journalier posé en 3.1
(`app.pipeline.budget.BudgetTracker.verifier_et_engager_fetch_page`).

Sous-étape 3.4b : `collecter_preuves`/`stocker_page` acceptent désormais un
`etiquette` optionnel (`ETIQUETTE_PREUVE_ENQUETE` par défaut, comportement
inchangé) -- les pages trouvées pour la famille de requêtes `prix`
(`app.enqueteur.enqueteur._enqueter_prix`) sont stockées étiquetées
`ETIQUETTE_PREUVE_PRIX` au lieu de `ETIQUETTE_PREUVE_ENQUETE`, pour que
l'Analyst comme un humain puissent distinguer une preuve de tarification du
reste des preuves d'enquête -- même mécanisme de stockage et de
rattachement (`opportunity_evidence`) que n'importe quelle autre page.

Garde-fou injection (§3.6 du cahier des charges, point 3 du texte de 3.3) :
le texte extrait d'une page est stocké tel quel, comme n'importe quel autre
contenu collecté (`sources.extrait`) — ce module ne l'interprète jamais, ne
l'exécute jamais, ne l'envoie à aucun modèle. Une page qui contiendrait
littéralement une instruction (« ignore tes règles... ») est donc neutre ici
par construction : la même protection déjà testée côté Analyst
(`tests/test_roles.py::test_page_hostile_ne_change_pas_le_role_ni_le_format`,
`app.roles.analyst.neutraliser_analyst`) continue de s'appliquer en aval, sur
le `source_id` comme pour n'importe quelle autre source.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup
from sqlalchemy.engine import Engine

from app import config as cfg
from app.adapters.http import USER_AGENT, ErreurCollecte, get_avec_limite_taille, get_with_retry
from app.enqueteur.fournisseurs import ResultatRecherche
from app.enqueteur.selection import selectionner_resultats
from app.pipeline import dedupe
from app.pipeline.budget import BudgetDepasse, BudgetTracker
from app.storage import repo

logger = logging.getLogger(__name__)

# Distingue ces sources des signaux `douleur` normaux (étiquette absente) et
# des `signal_concurrence` (sous-étape 1.1) : une preuve d'enquête sur UNE
# opportunité précise (rattachement réel à l'opportunité : sous-étape 3.4).
ETIQUETTE_PREUVE_ENQUETE = "preuve_enquete"

# Sous-étape 3.4b : une preuve d'enquête, mais spécifiquement issue de la
# famille de requêtes `prix` (recherche "<nom> pricing"/"<nom> tarifs" ou
# fetch direct de <domaine>/pricing) -- jamais `ETIQUETTE_PREUVE_ENQUETE`.
ETIQUETTE_PREUVE_PRIX = "prix"

_TAGS_A_SUPPRIMER = ("script", "style", "noscript", "nav", "header", "footer", "aside", "form")


@dataclass(frozen=True)
class PageCollectee:
    url: str
    titre: str
    texte: str
    date_collecte: datetime
    horodatage_source: datetime | None
    fournisseur: str
    requete_origine: str | None


def _robots_autorise(url: str, *, timeout: float) -> bool:
    """Sans `robots.txt` accessible (absent, erreur, ou hors délai) : la page
    est considérée AUTORISÉE — convention standard (l'absence de robots.txt
    vaut permission). Un `robots.txt` effectivement lu, lui, est toujours
    respecté. Une seule tentative (`max_retries=1`) : un `robots.txt`
    manquant (404, le cas le plus courant) ne doit pas coûter le même
    nombre de tentatives qu'une vraie page."""
    parts = urlsplit(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    try:
        resp = get_with_retry(robots_url, max_retries=1, timeout=timeout)
    except ErreurCollecte:
        return True
    analyseur = RobotFileParser()
    analyseur.parse(resp.text.splitlines())
    return analyseur.can_fetch(USER_AGENT, url)


def extraire_texte_principal(html: str) -> tuple[str, str]:
    """Fonction pure : (titre, texte). Retire les balises qui ne sont jamais
    du contenu éditorial (scripts, styles, navigation, formulaires...) puis
    aplatit le reste en texte, espaces normalisés.

    Pas une extraction « contenu principal » au sens strict (pas d'algorithme
    de type readability) : aucune bibliothèque d'extraction HTML n'était déjà
    présente dans le projet (`feedparser`, déjà utilisé partout ailleurs, ne
    fait que du flux RSS/Atom, pas des pages web arbitraires) — `beautifulsoup4`
    (parseur `html.parser` intégré à Python, aucune dépendance C) est ajoutée
    pour cette seule sous-étape. Voir le Journal de la sous-étape 3.3."""
    soupe = BeautifulSoup(html, "html.parser")
    titre = ""
    if soupe.title and soupe.title.string:
        titre = " ".join(soupe.title.string.split())
    if soupe.head:
        soupe.head.decompose()  # capturé dans `titre` ci-dessus : jamais compté deux fois dans `texte`
    for tag in soupe(_TAGS_A_SUPPRIMER):
        tag.decompose()
    texte = " ".join(soupe.get_text(separator=" ").split())
    return titre, texte


def recuperer_page(
    resultat: ResultatRecherche, *, timeout: float | None = None, max_octets: int | None = None,
    engine: Engine | None = None,
) -> PageCollectee | None:
    """Une seule page. Ne lève jamais : renvoie `None` pour toute page
    interdite par `robots.txt`, injoignable (timeout, 404, 5xx...), hors
    taille, ou vide après extraction — point 2 du texte de 3.3 (« n'est
    jamais stockée »), appliqué ici, avant le stockage lui-même.

    `engine` (sous-étape 3.7) : optionnel, `None` par défaut -- fourni par
    `collecter_preuves` pour journaliser ce fetch dans `journal_http`, avec
    `resultat.fournisseur` comme libellé (le `robots.txt` lu au-dessus n'est
    lui jamais journalisé : ce n'est pas "une page" au sens de ce plan, et sa
    fréquente absence -- 404, permission par défaut -- fausserait le taux de
    succès mesuré par flux/fournisseur)."""
    quotas = cfg.quotas()
    timeout = timeout if timeout is not None else quotas["enqueteur_fetch_timeout_secondes"]
    max_octets = max_octets if max_octets is not None else quotas["enqueteur_fetch_taille_max_octets"]

    if not _robots_autorise(resultat.url, timeout=timeout):
        logger.info("Enquêteur : robots.txt interdit %s, page ignorée.", resultat.url)
        return None

    journal = (
        {"engine": engine, "contexte": f"enqueteur_fetch:{resultat.fournisseur}"} if engine is not None else {}
    )
    try:
        contenu = get_avec_limite_taille(resultat.url, max_octets=max_octets, timeout=timeout, **journal)
    except ErreurCollecte as exc:
        logger.info("Enquêteur : page injoignable ou trop grande (%s) : %s", resultat.url, exc)
        return None

    titre, texte = extraire_texte_principal(contenu.decode("utf-8", errors="replace"))
    if not texte:
        logger.info("Enquêteur : page vide après extraction, ignorée (%s).", resultat.url)
        return None

    return PageCollectee(
        url=resultat.url,
        titre=titre or resultat.titre,
        texte=texte,
        date_collecte=datetime.now(timezone.utc),
        horodatage_source=resultat.horodatage_source,
        fournisseur=resultat.fournisseur,
        requete_origine=resultat.requete_origine,
    )


def stocker_page(engine: Engine, page: PageCollectee, *, etiquette: str = ETIQUETTE_PREUVE_ENQUETE) -> tuple[str, bool]:
    """Enregistre une page collectée comme n'importe quelle autre source
    (`app.storage.repo.upsert_source`) — mêmes garanties d'idempotence (URL
    canonique + empreinte de contenu : re-enquêter deux fois la même page ne
    crée jamais deux lignes). `etiquette` (sous-étape 3.4b) permet de
    distinguer une preuve de la famille `prix` (`ETIQUETTE_PREUVE_PRIX`) du
    reste des preuves d'enquête -- inchangé par défaut."""
    return repo.upsert_source(
        engine,
        url_canonique=dedupe.canonicaliser_url(page.url),
        domaine=urlsplit(page.url).netloc,
        date_publication=page.horodatage_source,
        type_source="page_web",
        extrait=page.texte,
        empreinte=dedupe.empreinte_contenu(page.texte),
        droits_collecte=f"page web publique récupérée par l'Enquêteur ({page.url}), robots.txt vérifié",
        flux_origine=page.fournisseur,
        requete_origine=page.requete_origine,
        etiquette=etiquette,
    )


def collecter_preuves(
    engine: Engine,
    resultats: list[ResultatRecherche],
    *,
    max_resultats: int | None = None,
    delai_entre_fetchs: float | None = None,
    opportunity_id: str | None = None,
    budget: BudgetTracker | None = None,
    etiquette: str = ETIQUETTE_PREUVE_ENQUETE,
) -> list[str]:
    """Sélectionne (`app.enqueteur.selection.selectionner_resultats`),
    récupère et stocke chaque page retenue, avec un délai entre deux fetchs
    consécutifs. Renvoie les `source_id` des pages effectivement stockées,
    dans l'ordre de sélection — une page injoignable, vide ou hors taille
    n'y apparaît jamais (point 2 du texte de 3.3).

    Sous-étape 3.4b : `etiquette` (par défaut `ETIQUETTE_PREUVE_ENQUETE`,
    comportement inchangé) est transmise telle quelle à `stocker_page` pour
    CHAQUE page de cet appel -- l'appelant qui veut mélanger plusieurs
    étiquettes fait plusieurs appels (voir `app.enqueteur.enqueteur`).

    Sous-étape 3.4, options rétrocompatibles (`None` par défaut : comportement
    strictement inchangé, voir tests existants de la sous-étape 3.3) :
    - `budget` : chaque fetch est protégé par le compteur journalier posé en
      3.1 (`verifier_et_engager_fetch_page`/`enregistrer_fetch_page`) -- un
      plafond atteint arrête simplement la boucle, jamais une exception qui
      remonte.
    - `opportunity_id` : chaque page stockée est en plus rattachée à
      l'opportunité (`opportunity_evidence`) -- jamais deux fois la même
      preuve sur une reprise (une enquête interrompue, relancée au passage
      suivant, ne duplique rien), et `independant` suit la même règle que
      pour l'Analyst (`app.pipeline.orchestrator._phase_analyse_et_critique`) :
      faux si une source déjà citée porte la même empreinte de contenu."""
    quotas = cfg.quotas()
    if max_resultats is None:
        max_resultats = quotas["max_resultats_enquete_par_opportunite"]
    if delai_entre_fetchs is None:
        delai_entre_fetchs = quotas["enqueteur_fetch_delai_secondes"]

    selectionnes = selectionner_resultats(resultats, max_resultats=max_resultats)

    deja_citees: set[str] = repo.sources_deja_citees(engine, opportunity_id) if opportunity_id else set()
    empreintes_existantes: set[str] = (
        repo.empreintes_sources_citees(engine, opportunity_id) if opportunity_id else set()
    )

    source_ids: list[str] = []
    for i, resultat in enumerate(selectionnes):
        if budget is not None:
            try:
                budget.verifier_et_engager_fetch_page()
            except BudgetDepasse as exc:
                logger.info("Enquêteur : %s -- arrêt du fetch pour cette enquête.", exc)
                break
        if i > 0:
            time.sleep(delai_entre_fetchs)
        page = recuperer_page(resultat, engine=engine)
        if budget is not None:
            budget.enregistrer_fetch_page(fournisseur=resultat.fournisseur)
        if page is None:
            continue
        source_id, _cree = stocker_page(engine, page, etiquette=etiquette)
        source_ids.append(source_id)

        if opportunity_id and source_id not in deja_citees:
            empreinte_page = dedupe.empreinte_contenu(page.texte)
            independant = empreinte_page not in empreintes_existantes
            empreintes_existantes.add(empreinte_page)
            deja_citees.add(source_id)
            repo.inserer_evidence(
                engine, opportunity_id=opportunity_id, source_id=source_id,
                claim=f"Enquête ({page.fournisseur}) : {page.titre or page.texte[:120]}",
                type_="non_verifie", independant=independant,
            )
    return source_ids
