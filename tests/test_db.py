"""Migration additive (sous-étape 0.7) : `metadata.create_all` ne fait
jamais d'ALTER sur une table déjà créée -- `migrer()` doit donc ajouter
lui-même les colonnes manquantes sur une base qui existait AVANT cette
sous-étape, sans jamais toucher aux données déjà présentes."""
from datetime import datetime, timezone

from sqlalchemy import MetaData, Table, Column, String, Float, Integer, DateTime, create_engine, insert, inspect, select

from app.storage.db import migrer


def _creer_ancienne_base(chemin) -> "Engine":
    """Reproduit le schéma `usage_events` D'AVANT la sous-étape 0.7 (sans
    `role` ni `opportunity_id`), avec une ligne déjà dedans -- comme la
    base de production au moment de cette migration."""
    moteur = create_engine(f"sqlite:///{chemin}", future=True, connect_args={"check_same_thread": False})
    ancienne_metadata = MetaData()
    ancien_usage_events = Table(
        "usage_events", ancienne_metadata,
        Column("id", String, primary_key=True),
        Column("run_id", String, nullable=False),
        Column("fournisseur", String, nullable=False),
        Column("modele_ou_actor", String, nullable=False),
        Column("appels", Integer, nullable=False, default=1),
        Column("tokens_in", Integer, nullable=True),
        Column("tokens_out", Integer, nullable=True),
        Column("cout_declare_ou_estime", Float, nullable=False),
        Column("devise", String, nullable=False, default="EUR"),
        Column("date_creation", DateTime(timezone=True), nullable=False),
    )
    ancienne_metadata.create_all(moteur)
    with moteur.begin() as cx:
        cx.execute(insert(ancien_usage_events).values(
            id="historique-1", run_id="run-ancien", fournisseur="anthropic", modele_ou_actor="m",
            appels=1, tokens_in=10, tokens_out=5, cout_declare_ou_estime=0.42, devise="EUR",
            date_creation=datetime.now(timezone.utc),
        ))
    return moteur


def test_migrer_ajoute_les_colonnes_manquantes_sans_toucher_aux_donnees(tmp_path):
    moteur = _creer_ancienne_base(tmp_path / "ancienne.db")

    migrer(moteur)  # doit ajouter role/opportunity_id, jamais lever

    inspecteur = inspect(moteur)
    colonnes = {c["name"] for c in inspecteur.get_columns("usage_events")}
    assert {"role", "opportunity_id"} <= colonnes

    from app.storage.schema import usage_events

    with moteur.connect() as cx:
        ligne = cx.execute(select(usage_events).where(usage_events.c.id == "historique-1")).mappings().first()
    assert ligne["cout_declare_ou_estime"] == 0.42  # donnée d'origine intacte
    assert ligne["role"] is None  # historique -> NULL, jamais inventé
    assert ligne["opportunity_id"] is None


def test_migrer_est_idempotent(tmp_path):
    moteur = _creer_ancienne_base(tmp_path / "ancienne2.db")
    migrer(moteur)
    migrer(moteur)  # un deuxième appel ne doit jamais planter (ALTER déjà appliqué)

    inspecteur = inspect(moteur)
    colonnes = {c["name"] for c in inspecteur.get_columns("usage_events")}
    assert {"role", "opportunity_id"} <= colonnes


def test_migrer_sur_base_toute_neuve_cree_directement_les_bonnes_colonnes(tmp_path):
    moteur = create_engine(
        f"sqlite:///{tmp_path / 'neuve.db'}", future=True, connect_args={"check_same_thread": False},
    )
    migrer(moteur)  # table créée directement par create_all, avec les colonnes -> rien à altérer

    inspecteur = inspect(moteur)
    colonnes = {c["name"] for c in inspecteur.get_columns("usage_events")}
    assert {"role", "opportunity_id"} <= colonnes
