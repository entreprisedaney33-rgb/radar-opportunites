"""Modèle de données (§5 du cahier des charges), en SQLAlchemy Core.

Choix : SQLAlchemy Core (pas l'ORM) pour rester portable entre SQLite (tests
et développement local sans rien installer) et PostgreSQL (Render, en
production) avec les mêmes requêtes. Identifiants en UUID texte générés côté
application : identiques sur les deux moteurs, pas de dépendance à
`SERIAL`/`AUTOINCREMENT`.
"""
from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    JSON,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)

metadata = MetaData()

runs = Table(
    "runs",
    metadata,
    Column("id", String, primary_key=True),
    Column("mode", String, nullable=False),  # 'dry-run' | 'reel'
    Column("debut", DateTime(timezone=True), nullable=False),
    Column("fin", DateTime(timezone=True), nullable=True),
    Column("statut", String, nullable=False),  # en_cours|termine|echoue|interrompu
    Column("version_code", String, nullable=False),
    Column("version_config", String, nullable=False),
    Column("quotas_json", JSON, nullable=False),
    Column("couts_json", JSON, nullable=False, default=dict),
    Column("erreurs_json", JSON, nullable=False, default=list),
    Column("resume_json", JSON, nullable=True),
)

sources = Table(
    "sources",
    metadata,
    Column("id", String, primary_key=True),
    Column("url_canonique", String, nullable=False),
    Column("domaine", String, nullable=False),
    Column("date_publication", DateTime(timezone=True), nullable=True),
    Column("date_collecte", DateTime(timezone=True), nullable=False),
    Column("type", String, nullable=False),  # rss|demo|apify|autre
    Column("extrait", Text, nullable=False),
    Column("empreinte", String, nullable=False),
    Column("droits_collecte", String, nullable=False),
    # Ajoutées en sous-étape 1.1 (migration additive) : NULL pour tout
    # l'historique antérieur.
    Column("flux_origine", String, nullable=True),  # nom du flux (app/sources.yaml)
    Column("requete_origine", String, nullable=True),  # texte de la requête, s'il y a lieu (à partir de 1.2)
    Column("etiquette", String, nullable=True),  # "signal_concurrence" pour un item d'un flux `offre`
    UniqueConstraint("url_canonique", "empreinte", name="uq_source_url_empreinte"),
)

signals = Table(
    "signals",
    metadata,
    Column("id", String, primary_key=True),
    Column("source_id", String, nullable=False),
    Column("run_id", String, nullable=False),
    Column("texte_court", Text, nullable=False),
    Column("categorie", String, nullable=False),
    Column("date_signal", DateTime(timezone=True), nullable=True),
    Column("normalisation_json", JSON, nullable=False, default=dict),
)

opportunities = Table(
    "opportunities",
    metadata,
    Column("id", String, primary_key=True),
    Column("titre", String, nullable=False),
    Column("acheteur", String, nullable=False),
    Column("probleme", Text, nullable=False),
    Column("mecanisme_ia", Text, nullable=False),
    Column("secteur", String, nullable=False),
    # Sous-étape 2.1 : provenance du secteur (citation_verifiee|flux|defaut,
    # voir app/pipeline/normalisation.py) — migration additive, NULL pour
    # l'historique (app/storage/db.py, _COLONNES_ADDITIVES).
    Column("secteur_provenance", String, nullable=True),
    Column("secteur_citation", Text, nullable=True),
    Column("statut", String, nullable=False),
    Column("cluster_id", String, nullable=True),
    Column("date_creation", DateTime(timezone=True), nullable=False),
    Column("date_maj", DateTime(timezone=True), nullable=False),
)

opportunity_evidence = Table(
    "opportunity_evidence",
    metadata,
    Column("id", String, primary_key=True),
    Column("opportunity_id", String, nullable=False),
    Column("source_id", String, nullable=False),
    Column("claim", Text, nullable=False),
    Column("type", String, nullable=False),  # observe|calcule|hypothese|non_verifie
    Column("independant", Boolean, nullable=False, default=True),
    Column("date_creation", DateTime(timezone=True), nullable=False),
)

assessments = Table(
    "assessments",
    metadata,
    Column("id", String, primary_key=True),
    Column("opportunity_id", String, nullable=False),
    Column("run_id", String, nullable=False),
    Column("role", String, nullable=False),  # scout|analyst|critic
    Column("payload_json", JSON, nullable=False),
    Column("modele", String, nullable=False),
    Column("version_prompt", String, nullable=False),
    Column("inconnues_json", JSON, nullable=False, default=list),
    Column("date_creation", DateTime(timezone=True), nullable=False),
)

scores = Table(
    "scores",
    metadata,
    Column("id", String, primary_key=True),
    Column("opportunity_id", String, nullable=False),
    Column("run_id", String, nullable=False),
    Column("version_poids", String, nullable=False),
    Column("valeurs_json", JSON, nullable=False),
    Column("score_brut", Float, nullable=False),
    Column("score_prudent", Float, nullable=False),
    Column("couverture_preuves", Float, nullable=False),
    Column("flags_json", JSON, nullable=False, default=list),
    Column("decision_critic", String, nullable=True),
    Column("date_creation", DateTime(timezone=True), nullable=False),
)
# Append-only par construction : le code ne fait jamais d'UPDATE sur `scores`,
# uniquement des INSERT (voir storage/repo.py). L'historique des scores
# précédents reste donc toujours consultable.

decisions = Table(
    "decisions",
    metadata,
    Column("id", String, primary_key=True),
    Column("opportunity_id", String, nullable=False),
    Column("auteur", String, nullable=False),  # 'humain:<nom>' | 'systeme'
    Column("action", String, nullable=False),  # rejeter|a_verifier|selectionner
    Column("date_creation", DateTime(timezone=True), nullable=False),
    Column("justification", Text, nullable=True),
)

controles = Table(
    "controles",
    metadata,
    Column("cle", String, primary_key=True),  # ex. "pause_all"
    Column("valeur", Boolean, nullable=False),
    Column("date_maj", DateTime(timezone=True), nullable=False),
)
# Contrôle partagé entre le cron nocturne et l'interface web (§4 : bouton
# pause). La variable d'environnement RADAR_PAUSE_ALL reste un second
# levier, indépendant de la base — utilisable même si la base est
# injoignable.

usage_events = Table(
    "usage_events",
    metadata,
    Column("id", String, primary_key=True),
    Column("run_id", String, nullable=False),
    Column("fournisseur", String, nullable=False),  # anthropic|apify|render
    Column("modele_ou_actor", String, nullable=False),
    Column("appels", Integer, nullable=False, default=1),
    Column("tokens_in", Integer, nullable=True),
    Column("tokens_out", Integer, nullable=True),
    Column("cout_declare_ou_estime", Float, nullable=False),
    Column("devise", String, nullable=False, default="EUR"),
    Column("date_creation", DateTime(timezone=True), nullable=False),
    # Ajoutées en sous-étape 0.7 (migration additive, voir
    # app/storage/db.py::_appliquer_migrations_additives) : NULL pour tout
    # l'historique antérieur, renseignées à chaque appel depuis.
    Column("role", String, nullable=True),  # scout|analyst|critic
    Column("opportunity_id", String, nullable=True),  # absent pour le Scout : appelé avant création du dossier
    # Ajoutées en sous-étape 3.10 (migration additive) : NULL pour tout
    # l'historique antérieur, renseignées à chaque appel modèle depuis
    # (app/adapters/model_client.py::appeler_structure). Jamais renseignées
    # pour les compteurs de l'Enquêteur (enqueteur_recherche/enqueteur_fetch,
    # sous-étape 3.1) : ce ne sont pas des appels modèle.
    Column("issue", String, nullable=True),  # valide|normalisee|relancee|perdue
    Column("sortie_tronquee", Boolean, nullable=True),  # stop_reason == "max_tokens"
)

source_requetes = Table(
    "source_requetes",
    metadata,
    Column("id", String, primary_key=True),
    Column("source_id", String, nullable=False),
    Column("flux_origine", String, nullable=True),
    Column("requete_origine", String, nullable=False),
    Column("date_creation", DateTime(timezone=True), nullable=False),
    UniqueConstraint("source_id", "flux_origine", "requete_origine", name="uq_source_requete"),
)
# Sous-étape 1.4 : dédoublonnage multi-requêtes. Un même post retrouvé par
# plusieurs requêtes de recherche différentes (Reddit, Hacker News...) ne crée
# jamais deux lignes dans `sources` (uq_source_url_empreinte ci-dessus,
# inchangée) — mais chaque requête distincte qui l'a retrouvé est tracée ici
# (voir app/storage/repo.py::upsert_source), pour mesurer quelles expressions
# du lexique de douleur sont productives (app/metriques.py). Table neuve, pas
# de migration additive nécessaire (comme etats_flux_recherche et
# tirages_controle_rejetes ci-dessous).

etats_flux_recherche = Table(
    "etats_flux_recherche",
    metadata,
    Column("cle", String, primary_key=True),  # id du flux, ex. "reddit_recherche:smallbusiness:manually_en"
    Column("derniere_visite", DateTime(timezone=True), nullable=False),
)
# Sous-étape 1.2 : mémoire du planificateur de recherche Reddit (rotation
# sub × expression — le produit dépasse 200 flux, voir
# app/pipeline/planificateur_recherche.py). Table neuve (pas de migration
# additive nécessaire, `metadata.create_all` la crée directement) : un flux
# jamais visité est simplement absent de cette table.

journal_http = Table(
    "journal_http",
    metadata,
    Column("id", String, primary_key=True),
    Column("horodatage", DateTime(timezone=True), nullable=False),
    Column("hote", String, nullable=False),  # ex. "www.reddit.com", "hn.algolia.com", ou le domaine fetché
    Column("flux_ou_fournisseur", String, nullable=False),  # ex. "reddit_recherche:smallbusiness:manually_en"
    Column("code_http", Integer, nullable=True),
    Column("erreur", String, nullable=True),  # "timeout" | "erreur_reseau" — absent si un code HTTP a été reçu
    Column("duree_ms", Float, nullable=False),
)
# Sous-étape 3.7 (AMELIORATIONS.md) : une ligne par appel HTTP réel de la
# collecte (RSS, recherche Reddit/HN) et de l'Enquêteur (recherche, fetch de
# page) — écrite depuis `app/adapters/http.py`, le seul point de passage de
# tous ces appels (garde-fou : aucun appel direct à `requests` ailleurs).
# Jamais de contenu de page ni d'URL complète (peut porter des paramètres
# sensibles) : seulement l'hôte et un libellé de flux/fournisseur. Table
# neuve, pas de migration additive nécessaire (même raisonnement que
# `etats_flux_recherche`/`tirages_controle_rejetes` ci-dessus). Jamais relue
# par le pipeline lui-même — uniquement par `app.metriques` (observabilité).

tirages_controle_rejetes = Table(
    "tirages_controle_rejetes",
    metadata,
    Column("id", String, primary_key=True),
    Column("opportunity_id", String, nullable=False),
    Column("run_id", String, nullable=False),
    Column("date_creation", DateTime(timezone=True), nullable=False),
    Column("decision_avant", String, nullable=False),
    Column("decision_apres", String, nullable=False),
    UniqueConstraint("opportunity_id", name="uq_tirage_controle_opportunity"),
)
# Journal de l'échantillon de contrôle des rejetés (§2, pipeline) : une
# opportunité `rejete` ne peut être retirée au tirage qu'UNE SEULE FOIS au
# total (contrainte d'unicité ci-dessus, en plus du filtre applicatif dans
# `app/pipeline/orchestrator.py::_selectionner_pour_analyse`) — voir
# `rapports/DIAGNOSTIC_BUDGET_2026-09-25.md`, §4.
