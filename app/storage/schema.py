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
    # Sous-étape 3.11 : mots-clés courts validés du Scout pour les requêtes
    # de l'Enquêteur (app.pipeline.mots_cles.valider_mots_cles) — migration
    # additive, NULL pour l'historique (app/storage/db.py, _COLONNES_ADDITIVES)
    # et pour toute opportunité dont la proposition du Scout n'a pas passé la
    # validation. `app.pipeline.orchestrator._phase_enquete` dérive un repli
    # par du code (app.pipeline.mots_cles.deriver_mots_cles_repli) quand
    # `mots_cles_en` est NULL, plutôt que d'envoyer une requête vide.
    Column("mots_cles_en", String, nullable=True),
    Column("mots_cles_fr", String, nullable=True),
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
    # Sous-étape 4.1 (migration additive, NULL pour tout l'historique) :
    # `recalcul_4_1` = ligne écrite par `app.recalcul` (règle actuelle
    # appliquée à une analyse ancienne), jamais par une vraie analyse.
    Column("origine", String, nullable=True),
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
    Column("erreur", String, nullable=True),  # "timeout" | "erreur_reseau" (V2.8b, SIRENE : "erreur_reseau:dns", "timeout:delai_connexion"...) — absent si un code HTTP a été reçu
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

etats_disjoncteur_enqueteur = Table(
    "etats_disjoncteur_enqueteur",
    metadata,
    Column("cle", String, primary_key=True),  # nom du fournisseur, ex. "reddit" (app.enqueteur.disjoncteur.NOM_REDDIT)
    Column("echecs_consecutifs", Integer, nullable=False),
    Column("pause_jusqu_a", DateTime(timezone=True), nullable=True),
    Column("date_maj", DateTime(timezone=True), nullable=False),
)
# Sous-étape 3.11 : état persisté du disjoncteur Reddit de l'Enquêteur
# (`app.enqueteur.disjoncteur`) — DOIT survivre plusieurs passages du
# Background Worker (fenêtre de pause en MINUTES, pas « le reste de ce
# passage » comme le disjoncteur du Scout) et rester lisible par
# `app.metriques`, un processus séparé qui ne voit jamais la mémoire du
# worker. Table neuve, pas de migration additive nécessaire (même
# raisonnement que `etats_flux_recherche`/`journal_http` ci-dessus).

etats_disjoncteur_api = Table(
    "etats_disjoncteur_api",
    metadata,
    Column("cle", String, primary_key=True),  # fixe, "modele" (un seul disjoncteur -- Scout+Analyst+Critic partagés)
    Column("echecs_consecutifs", Integer, nullable=False),
    Column("en_erreur", Boolean, nullable=False),
    Column("depuis", DateTime(timezone=True), nullable=True),
    Column("pause_jusqu_a", DateTime(timezone=True), nullable=True),
    Column("dernier_message", Text, nullable=True),
    Column("date_maj", DateTime(timezone=True), nullable=False),
)
# Sous-étape 3.13 : état persisté du disjoncteur de l'appel au modèle
# (`app.pipeline.disjoncteur_api`) -- panne du 26/09/2026 (voir
# rapports/POINT_ETAPE_2026-09-27.md) : ~100 % des appels Scout/Analyst/
# Critic ont échoué pendant ~25 h sans qu'aucun signal ne remonte ailleurs
# qu'une lecture manuelle de la base, chaque dossier retombant en silence sur
# son repli heuristique. Même raisonnement que `etats_disjoncteur_enqueteur`
# ci-dessus (fenêtre en MINUTES, doit survivre plusieurs passages, lisible
# par `app.metriques`) : table neuve, pas de migration additive nécessaire.

faisabilites = Table(
    "faisabilites",
    metadata,
    Column("id", String, primary_key=True),
    Column("opportunity_id", String, nullable=False),
    Column("run_id", String, nullable=True),
    Column("origine", String, nullable=False),  # analyse | reprise_4_1
    Column("payload_json", JSON, nullable=False),  # FaisabiliteSortie (valeurs + justifications, type=hypothese)
    Column("accessible_solo", Boolean, nullable=True),  # dérivé par le CODE ; NULL = non évaluée
    Column("motif_exclusion", Text, nullable=True),  # dérivé par le CODE, lisible ; NULL si accessible
    Column("modele", String, nullable=True),
    Column("date_creation", DateTime(timezone=True), nullable=False),
)
# Sous-étape 4.1 : faisabilité pour Mathéo, append-only (la dernière ligne par
# dossier fait foi). Table neuve : aucune migration de colonne nécessaire.
# Ne sert JAMAIS au score de preuve. Lue par le workflow Jarvis (onglet Radar).


etablissements_secteur = Table(
    "etablissements_secteur",
    metadata,
    Column("id", String, primary_key=True),
    Column("code_naf", String, nullable=False),
    Column("naf_version", String, nullable=False),  # "2" (rév. 2) ; "2.1" après le 1er janvier 2027 (décision du 2026-10-01)
    Column("departement", String, nullable=False),  # code de département, ou "FR" = France métropolitaine (comptage seul)
    # Compte d'ENTREPRISES (unités légales actives) dont l'activité principale est `code_naf` et qui ont au moins
    # un établissement dans le département : c'est ce que l'API renvoie (`total_results`), pas un compte d'établissements.
    Column("nb_entreprises_actives", Integer, nullable=False),
    # Vrai si l'API a plafonné le total à 10 000 : `nb_entreprises_actives` est alors une borne basse, pas un compte.
    Column("comptage_plafonne", Boolean, nullable=False),
    # Établissements réellement listés dans l'échantillon (actifs, même code NAF, dans le département) ; NULL si comptage seul.
    Column("nb_etablissements_listes", Integer, nullable=True),
    Column("echantillon_complet", Boolean, nullable=True),  # vrai si toutes les entreprises du filtre ont été lues
    Column("plafond_echantillon", Integer, nullable=True),
    Column("requetes", Integer, nullable=False),  # nombre d'appels HTTP de CETTE mesure
    Column("source_url", Text, nullable=False),  # URL de l'API + paramètres de la première requête (preuve interrogeable)
    Column("horodatage", DateTime(timezone=True), nullable=False),
)
# Sous-étape V2.2 (RADAR-V2.md) : append-only, une ligne par mesure (code NAF × département) ; la plus récente fait foi,
# les précédentes servent aux tendances. Table neuve : aucune migration de colonne nécessaire.

prospection = Table(
    "prospection",
    metadata,
    Column("id", String, primary_key=True),
    Column("siret", String, nullable=False),
    Column("naf_version", String, nullable=False),
    Column("code_naf", String, nullable=False),
    Column("departement", String, nullable=False),
    Column("siren", String, nullable=True),
    Column("raison_sociale", String, nullable=False),
    Column("adresse", String, nullable=True),
    Column("code_postal", String, nullable=True),
    Column("code_commune", String, nullable=True),
    Column("commune", String, nullable=True),
    Column("latitude", Float, nullable=True),
    Column("longitude", Float, nullable=True),
    Column("distance_centre_km", Float, nullable=True),  # à vol d'oiseau, du centre de config/zone.yaml
    Column("tranche_effectif_salarie", String, nullable=True),  # code INSEE, tel quel
    Column("categorie_entreprise", String, nullable=True),  # PME | ETI | GE, tel quel
    Column("est_siege", Boolean, nullable=True),
    Column("premiere_collecte", DateTime(timezone=True), nullable=False),
    Column("derniere_vue", DateTime(timezone=True), nullable=False),
    UniqueConstraint("siret", "naf_version", name="uq_prospection_siret_naf_version"),
)
# Sous-étape V2.2 : échantillon de prospection (jusqu'à 500 établissements par code NAF × département). Jamais
# supprimée : un établissement qui disparaît d'un rafraîchissement garde sa `derniere_vue` d'origine.


offres_emploi = Table(
    "offres_emploi",
    metadata,
    Column("id", String, primary_key=True),
    Column("id_offre", String, nullable=False, unique=True),  # identifiant France Travail : dédoublonnage
    Column("naf_version", String, nullable=False),  # nomenclature du `code_naf` ci-dessous ("2" aujourd'hui)
    Column("code_naf", String, nullable=True),
    Column("intitule", String, nullable=False),
    Column("description", Text, nullable=True),
    Column("rome_code", String, nullable=True),
    Column("type_contrat", String, nullable=True),
    Column("commune", String, nullable=True),  # code INSEE
    Column("code_postal", String, nullable=True),
    Column("departement", String, nullable=True),
    Column("latitude", Float, nullable=True),
    Column("longitude", Float, nullable=True),
    Column("salaire_libelle", String, nullable=True),  # libellé brut de France Travail, tel quel
    # Conversions annuelles BRUTES APPROXIMATIVES du libellé (mensuel x nombre de mois, horaire x 1 820 h) ; NULL si illisible.
    Column("salaire_annuel_min_eur", Float, nullable=True),
    Column("salaire_annuel_max_eur", Float, nullable=True),
    Column("entreprise_nom", String, nullable=True),
    Column("tranche_effectif_etab", String, nullable=True),
    Column("date_creation", DateTime(timezone=True), nullable=False),
    Column("date_actualisation", DateTime(timezone=True), nullable=True),
    Column("premiere_collecte", DateTime(timezone=True), nullable=False),
    Column("derniere_vue", DateTime(timezone=True), nullable=False),
)
# Sous-étape V2.3 (RADAR-V2.md) : une ligne par offre d'emploi active de France Travail, dédoublonnée par
# `id_offre`. Jamais supprimée : une offre qui disparaît de l'API (pourvue, retirée) garde sa `derniere_vue`.
# Le texte de l'offre sert aux comptes et à l'étiquetage (V2.4), jamais à un affichage tel quel (conditions d'utilisation).

collectes_offres = Table(
    "collectes_offres",
    metadata,
    Column("id", String, primary_key=True),
    Column("code_naf", String, nullable=False),
    Column("naf_version", String, nullable=False),
    Column("debut", DateTime(timezone=True), nullable=False),  # plage de dates de CRÉATION interrogée
    Column("fin", DateTime(timezone=True), nullable=False),
    Column("nb_offres", Integer, nullable=False),
    Column("nb_nouvelles", Integer, nullable=False),
    Column("requetes", Integer, nullable=False),
    Column("fenetres_tronquees", Integer, nullable=False),  # > 0 : des offres ont pu manquer (fenêtre d'une heure trop pleine)
    Column("horodatage", DateTime(timezone=True), nullable=False),
)
# Sous-étape V2.3 : journal append-only des collectes, par code NAF. La `fin` de la dernière collecte d'un code
# fixe le début de la suivante (avec un jour de chevauchement) ; les `requetes` servent aux métriques.


offres_etiquetage = Table(
    "offres_etiquetage",
    metadata,
    Column("id_offre", String, primary_key=True),  # identifiant France Travail (offres_emploi.id_offre) : une ligne par offre
    # ok = lexique + modèle faits ; lexique_seul = modèle pas encore passé (option, accès ou budget) ; echec_modele = appel perdu
    Column("statut", String, nullable=False),
    Column("version", String, nullable=False),  # config/etiquetage.yaml::version au moment de l'étiquetage
    Column("modele", String, nullable=True),
    Column("nb_lexique", Integer, nullable=False),  # tâches trouvées par le lexique
    Column("nb_citations_proposees", Integer, nullable=False),  # tâches proposées par le modèle (avant vérification)
    Column("nb_citations_verifiees", Integer, nullable=False),  # dont la citation est textuellement dans l'offre
    Column("etiquetee_le", DateTime(timezone=True), nullable=False),
)
# Sous-étape V2.4 : état d'étiquetage par offre. Sert à ne JAMAIS repayer une offre déjà étiquetée et à mesurer le
# taux de citation vérifiée (somme des vérifiées / somme des proposées).

offres_taches = Table(
    "offres_taches",
    metadata,
    Column("id", String, primary_key=True),
    Column("id_offre", String, nullable=False),
    Column("tache_id", String, nullable=False),  # config/taches.yaml::id
    Column("provenance", String, nullable=False),  # lexique | citation_verifiee
    Column("citation", Text, nullable=False),  # lexique : le mot-clé trouvé ; citation_verifiee : l'extrait copié de l'offre
    Column("modele", String, nullable=True),  # renseigné pour citation_verifiee
    Column("version", String, nullable=False),
    Column("date_creation", DateTime(timezone=True), nullable=False),
    UniqueConstraint("id_offre", "tache_id", "provenance", name="uq_offres_taches"),
)
# Sous-étape V2.4 : append-only. Une offre « mentionne » une tâche si elle a au moins une ligne ici, quelle que soit la
# provenance ; une citation que le modèle n'a pas pu prouver textuellement n'est JAMAIS écrite.

demande_secteur_tache = Table(
    "demande_secteur_tache",
    metadata,
    Column("id", String, primary_key=True),
    Column("code_naf", String, nullable=False),
    Column("naf_version", String, nullable=False),
    Column("tache_id", String, nullable=False),
    Column("fenetre_jours", Integer, nullable=False),  # stock d'offres actives créées dans les N derniers jours
    # Dénominateur : offres du secteur dans la fenêtre dont l'étiquetage est COMPLET (statut ok) ; `couverture_etiquetage`
    # = ce nombre / toutes les offres du secteur dans la fenêtre. Une couverture basse rend la part trompeuse.
    Column("nb_offres_secteur", Integer, nullable=False),
    Column("nb_offres_secteur_total", Integer, nullable=False),
    Column("couverture_etiquetage", Float, nullable=False),
    Column("nb_offres_tache", Integer, nullable=False),  # France
    Column("nb_offres_tache_zone", Integer, nullable=False),  # départements de la zone (config/zone.yaml)
    Column("nb_citation_verifiee", Integer, nullable=False),  # parmi nb_offres_tache : au moins une citation vérifiée
    Column("nb_lexique_seul", Integer, nullable=False),  # parmi nb_offres_tache : trouvées par le lexique uniquement
    Column("part_offres_tache", Float, nullable=False),  # nb_offres_tache / nb_offres_secteur
    # Extrapolation (échantillon par code, config/etiquetage.yaml::echantillon_max_par_code) : part x toutes les offres
    # collectées du secteur dans la fenêtre. Égal à l'observé quand la couverture est de 100 % (aucune incertitude).
    Column("nb_offres_tache_estime", Integer, nullable=False),
    Column("nb_offres_tache_zone_estime", Integer, nullable=False),
    Column("part_ic95_bas", Float, nullable=False),   # intervalle de confiance de Wilson à 95 % sur la part
    Column("part_ic95_haut", Float, nullable=False),
    Column("salaire_median_annuel_eur", Float, nullable=True),  # médiane des milieux de fourchette ; NULL si aucun salaire lisible
    Column("nb_salaires", Integer, nullable=False),
    # Durée réelle d'accumulation de la collecte (jours depuis la première collecte du code) : chaque chiffre de demande
    # doit dire sur quelle durée il est construit (décision de Mathéo du 2026-10-01).
    Column("accumulation_jours", Integer, nullable=False),
    Column("tendance_3_mois_pct", Float, nullable=True),  # NULL tant que l'accumulation est insuffisante
    Column("tendance_statut", String, nullable=False),  # calculee | accumulation_insuffisante | sans_reference
    Column("motif_exclusion", String, nullable=True),  # secteur ou règle HDS (referentiels.motif_exclusion_couple) ; NULL si retenu
    Column("calcule_le", DateTime(timezone=True), nullable=False),
)
# Sous-étape V2.4 : instantanés append-only ; la ligne la plus récente d'un (code, tâche) fait foi, une nouvelle ligne
# n'est écrite que si un chiffre a changé (V2.5 recalcule une fiche quand ses agrégats bougent de plus de 20 %).


fiches_secteur_tache = Table(
    "fiches_secteur_tache",
    metadata,
    Column("id", String, primary_key=True),
    Column("code_naf", String, nullable=False),
    Column("tache_id", String, nullable=False),
    Column("naf_version", String, nullable=False),
    Column("version_score", String, nullable=False),  # config/fiches.yaml::version au moment du calcul
    Column("version_prompt", String, nullable=False),
    Column("decision", String, nullable=False),  # eligible_prospection | a_verifier | exclue (décidée par le CODE)
    Column("motifs_json", JSON, nullable=False),  # motif d'exclusion, ou preuves manquantes
    Column("score_brut", Float, nullable=False),
    Column("score_prudent", Float, nullable=False),
    Column("score_json", JSON, nullable=False),  # détail critère par critère, brut et prudent, avec les preuves
    Column("agregats_json", JSON, nullable=False),  # chiffres d'entrée figés à la date de la fiche (comparés au rafraîchissement)
    Column("fiche_json", JSON, nullable=True),  # sortie de l'Analyste (affirmations vérifiées seulement) ; NULL si l'Analyste n'a pas tourné
    Column("critique_json", JSON, nullable=True),  # sortie du Critic ; NULL s'il n'a pas tourné (porte d'accessibilité fermée)
    Column("modele_analyste", String, nullable=True),
    Column("modele_critic", String, nullable=True),
    Column("calcule_le", DateTime(timezone=True), nullable=False),
)
# Sous-étape V2.5 : append-only. La ligne la plus récente d'un (code, tâche) fait foi ; les précédentes montrent l'évolution
# (score, décision) d'un mois à l'autre. Aucune fiche n'est jamais produite par repli : sans Analyste valide, pas de ligne.


# Sous-étape V2.6 : concurrence d'un couple secteur x tâche. Append-only : la ligne ÉVALUÉE la plus récente d'un (code, tâche) fait foi
# pour le score ; une ligne `non_evalue` (pas de clé, plafond atteint, aucun résultat) n'efface jamais une évaluation antérieure.
concurrence_secteur_tache = Table(
    "concurrence_secteur_tache",
    metadata,
    Column("id", String, primary_key=True),
    Column("code_naf", String, nullable=False),
    Column("tache_id", String, nullable=False),
    Column("naf_version", String, nullable=False),
    Column("source", String, nullable=False),  # web_brave | session_claude
    Column("statut", String, nullable=False),  # evalue | non_evalue
    Column("motif", String, nullable=True),  # pourquoi non évalué (ou remarque sur l'évaluation)
    Column("outils_json", JSON, nullable=False),  # [{nom, url, prix: {texte, source_url}|None, prix_non_trouve}]
    Column("service_local_json", JSON, nullable=True),  # {present, nom, url, preuve} ; NULL si non évalué
    Column("requetes_json", JSON, nullable=False),  # requêtes réellement posées (traçabilité)
    Column("evalue_le", DateTime(timezone=True), nullable=False),
)

# Sous-étape V2.6 : une ligne PAR requête de recherche web, écrite AVANT l'appel (plafond mensuel strict RADAR_RECHERCHE_WEB_MAX_MOIS).
recherches_web = Table(
    "recherches_web",
    metadata,
    Column("id", String, primary_key=True),
    Column("fournisseur", String, nullable=False),
    Column("mois", String, nullable=False),  # AAAA-MM (UTC)
    Column("requete", Text, nullable=False),
    Column("code_naf", String, nullable=True),
    Column("tache_id", String, nullable=True),
    Column("nb_resultats", Integer, nullable=True),  # NULL tant que l'appel n'est pas revenu
    Column("date_creation", DateTime(timezone=True), nullable=False),
)
