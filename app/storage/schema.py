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
)
