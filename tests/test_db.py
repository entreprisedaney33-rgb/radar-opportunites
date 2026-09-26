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


def _creer_base_avant_3_10(chemin) -> "Engine":
    """Reproduit `usage_events` juste AVANT la sous-étape 3.10 (avec `role`/
    `opportunity_id`, sans `issue`/`sortie_tronquee`), une ligne dedans."""
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
        Column("role", String, nullable=True),
        Column("opportunity_id", String, nullable=True),
    )
    ancienne_metadata.create_all(moteur)
    with moteur.begin() as cx:
        cx.execute(insert(ancien_usage_events).values(
            id="historique-3.9", run_id="run-ancien", fournisseur="anthropic", modele_ou_actor="m",
            appels=1, tokens_in=10, tokens_out=5, cout_declare_ou_estime=0.42, devise="EUR",
            date_creation=datetime.now(timezone.utc), role="critic", opportunity_id="opp-1",
        ))
    return moteur


def test_migrer_ajoute_issue_et_sortie_tronquee_sans_toucher_aux_donnees(tmp_path):
    """Sous-étape 3.10 : `sortie_tronquee` est la première colonne additive
    BOOLÉENNE (toutes les précédentes sont du texte) -- ce test vérifie en
    plus qu'un booléen inséré après la migration fait bien un aller-retour
    correct (pas juste "la colonne existe"), preuve que le type SQL par
    colonne (`app.storage.db._COLONNES_ADDITIVES`) est bien appliqué."""
    moteur = _creer_base_avant_3_10(tmp_path / "avant_3_10.db")

    migrer(moteur)

    inspecteur = inspect(moteur)
    colonnes = {c["name"] for c in inspecteur.get_columns("usage_events")}
    assert {"issue", "sortie_tronquee"} <= colonnes

    from app.storage.schema import usage_events

    with moteur.connect() as cx:
        ligne = cx.execute(select(usage_events).where(usage_events.c.id == "historique-3.9")).mappings().first()
    assert ligne["cout_declare_ou_estime"] == 0.42  # donnée d'origine intacte
    assert ligne["role"] == "critic"  # migration précédente (0.7) intacte
    assert ligne["issue"] is None  # historique -> NULL, jamais inventé
    assert ligne["sortie_tronquee"] is None

    with moteur.begin() as cx:
        cx.execute(insert(usage_events).values(
            id="nouvelle-3.10", run_id="run-test", fournisseur="anthropic", modele_ou_actor="m",
            appels=1, tokens_in=10, tokens_out=5, cout_declare_ou_estime=0.01, devise="EUR",
            date_creation=datetime.now(timezone.utc), role="analyst", opportunity_id="opp-2",
            issue="valide", sortie_tronquee=True,
        ))
    with moteur.connect() as cx:
        nouvelle = cx.execute(select(usage_events).where(usage_events.c.id == "nouvelle-3.10")).mappings().first()
    assert nouvelle["issue"] == "valide"
    assert nouvelle["sortie_tronquee"] is True  # vrai booléen, pas la chaîne "True"/"1"


def test_migrer_sur_base_toute_neuve_cree_directement_les_bonnes_colonnes(tmp_path):
    moteur = create_engine(
        f"sqlite:///{tmp_path / 'neuve.db'}", future=True, connect_args={"check_same_thread": False},
    )
    migrer(moteur)  # table créée directement par create_all, avec les colonnes -> rien à altérer

    inspecteur = inspect(moteur)
    colonnes = {c["name"] for c in inspecteur.get_columns("usage_events")}
    assert {"role", "opportunity_id"} <= colonnes


def _creer_ancienne_table_sources(chemin) -> "Engine":
    """Reproduit le schéma `sources` D'AVANT la sous-étape 1.1 (sans
    `flux_origine`/`requete_origine`/`etiquette`), avec une ligne dedans."""
    moteur = create_engine(f"sqlite:///{chemin}", future=True, connect_args={"check_same_thread": False})
    ancienne_metadata = MetaData()
    ancien_sources = Table(
        "sources", ancienne_metadata,
        Column("id", String, primary_key=True),
        Column("url_canonique", String, nullable=False),
        Column("domaine", String, nullable=False),
        Column("date_publication", DateTime(timezone=True), nullable=True),
        Column("date_collecte", DateTime(timezone=True), nullable=False),
        Column("type", String, nullable=False),
        Column("extrait", String, nullable=False),
        Column("empreinte", String, nullable=False),
        Column("droits_collecte", String, nullable=False),
    )
    ancienne_metadata.create_all(moteur)
    with moteur.begin() as cx:
        cx.execute(insert(ancien_sources).values(
            id="historique-source-1", url_canonique="https://exemple.invalid/x", domaine="exemple",
            date_publication=None, date_collecte=datetime.now(timezone.utc), type="rss",
            extrait="texte", empreinte="abc", droits_collecte="test",
        ))
    return moteur


def test_migrer_ajoute_les_colonnes_1_1_sur_sources_sans_toucher_aux_donnees(tmp_path):
    moteur = _creer_ancienne_table_sources(tmp_path / "ancienne_sources.db")

    migrer(moteur)

    inspecteur = inspect(moteur)
    colonnes = {c["name"] for c in inspecteur.get_columns("sources")}
    assert {"flux_origine", "requete_origine", "etiquette"} <= colonnes

    from app.storage.schema import sources

    with moteur.connect() as cx:
        ligne = cx.execute(select(sources).where(sources.c.id == "historique-source-1")).mappings().first()
    assert ligne["empreinte"] == "abc"  # donnée d'origine intacte
    assert ligne["flux_origine"] is None
    assert ligne["etiquette"] is None


def _creer_ancienne_table_opportunities(chemin) -> "Engine":
    """Reproduit le schéma `opportunities` D'AVANT la sous-étape 2.1 (sans
    `secteur_provenance`/`secteur_citation`), avec une ligne dedans."""
    moteur = create_engine(f"sqlite:///{chemin}", future=True, connect_args={"check_same_thread": False})
    ancienne_metadata = MetaData()
    anciennes_opportunities = Table(
        "opportunities", ancienne_metadata,
        Column("id", String, primary_key=True),
        Column("titre", String, nullable=False),
        Column("acheteur", String, nullable=False),
        Column("probleme", String, nullable=False),
        Column("mecanisme_ia", String, nullable=False),
        Column("secteur", String, nullable=False),
        Column("statut", String, nullable=False),
        Column("cluster_id", String, nullable=True),
        Column("date_creation", DateTime(timezone=True), nullable=False),
        Column("date_maj", DateTime(timezone=True), nullable=False),
    )
    ancienne_metadata.create_all(moteur)
    with moteur.begin() as cx:
        cx.execute(insert(anciennes_opportunities).values(
            id="historique-opp-1", titre="t", acheteur="a", probleme="p", mecanisme_ia="m",
            secteur="e_commerce", statut="nouveau", cluster_id=None,
            date_creation=datetime.now(timezone.utc), date_maj=datetime.now(timezone.utc),
        ))
    return moteur


def test_migrer_ajoute_les_colonnes_2_1_sur_opportunities_sans_toucher_aux_donnees(tmp_path):
    moteur = _creer_ancienne_table_opportunities(tmp_path / "ancienne_opportunities.db")

    migrer(moteur)

    inspecteur = inspect(moteur)
    colonnes = {c["name"] for c in inspecteur.get_columns("opportunities")}
    assert {"secteur_provenance", "secteur_citation"} <= colonnes

    from app.storage.schema import opportunities

    with moteur.connect() as cx:
        ligne = cx.execute(select(opportunities).where(opportunities.c.id == "historique-opp-1")).mappings().first()
    assert ligne["secteur"] == "e_commerce"  # donnée d'origine intacte
    assert ligne["secteur_provenance"] is None
    assert ligne["secteur_citation"] is None
