# Carte du dépôt — `produits/radar-opportunites`

Écrite en 0.1, **mise à jour le 2026-10-01 (V2.0)** : la version 1 a beaucoup bougé (étapes 1 à 4.1).
La version 1 est figée (worker à suspendre) ; le plan courant est `RADAR-V2.md`, qui ajoutera
de nouveaux modules (référentiels, établissements, offres d'emploi, fiches) sans toucher à ceux-ci.

## 1. Arborescence et rôle de chaque module

- `app/cli.py` — `run-once` / `run-forever` / `migrate`.
- `app/config.py` — config YAML + variables d'env (`Settings`).
- `app/metriques.py` — `python -m app.metriques` (lecture seule via `radar_lecture`, `--comparer`).
- `app/models_schemas.py` — schémas Pydantic des rôles ; `app/adapters/schema_strict.py` rend les schémas stricts pour l'API.
- `app/pipeline/` — `orchestrator.py` (run journalier, phases, reprise), `budget.py` (plafond € et appels), `disjoncteur_api.py` (arrêt sur échec systémique), `dedupe.py`, `normalisation.py` (`inferer_secteur`), `mots_cles.py`, `planificateur_recherche.py`.
- `app/roles/` — `scout.py`, `analyst.py`, `critic.py`, `faisabilite.py`, `prompts_communs.py`.
- `app/enqueteur/` — Enquêteur multi-sources : fournisseurs (gratuits, Brave désactivé), gabarits, fetch (robots.txt), qualité de page, concurrents, disjoncteur, sélection.
- `app/scoring/engine.py` — score déterministe lié à `SCORING.md` (poids dans `config/poids_scoring.yaml`).
- `app/faisabilite.py`, `app/recalcul.py`, `app/reprise.py` — drapeau `accessible_solo`, recalcul + `archive_faible`, reprise des dossiers créés en repli.
- `app/sources.py`, `app/sources.yaml`, `app/lexique_douleur.*` — sources typées (douleur/offre) et lexique de douleur (anglais).
- `app/adapters/` — `rss_adapter`, `hn_recherche`, `reddit_recherche` et `reddit_api` (Reddit en pause), `http` (retry, journal HTTP), `model_client` (appel modèle), `demo_adapter`.
- `app/storage/{schema,repo,db}.py` — tables, accès, migrations additives.
- `app/web/server.py`, `app/reports/html_report.py` — interface Flask interne, rapport HTML.
- `app/referentiels.py` + `config/{secteurs_tpe,taches,zone,declencheurs}.yaml` — référentiels de la v2 (V2.1), chargés avec validation stricte.
- `app/adapters/recherche_entreprises.py` + `app/etablissements.py` — API Recherche d'entreprises (SIRENE) et rafraîchissement mensuel des établissements (V2.2) ; tables `etablissements_secteur` et `prospection` ; commande `python -m app.cli etablissements` ; test de fumée réel `tests_payants/fumee_etablissements.py`.
- `app/adapters/france_travail.py` + `app/offres.py` — API Offres d'emploi de France Travail (OAuth `client_credentials`, recherche paginée, fenêtres de dates) et collecte par code NAF (V2.3) ; tables `offres_emploi` et `collectes_offres` ; commande `python -m app.cli offres` ; identifiants : `scripts/enregistrer_identifiants_france_travail.py` ; test de fumée réel `tests_payants/fumee_offres.py`.
- `app/etiquetage.py` + `app/agregation.py` + `config/etiquetage.yaml` — étiquetage des offres par tâche (lexique gratuit puis modèle le moins cher, citation vérifiée textuellement, budget/enveloppe, échantillon par code NAF) et agrégation secteur x tâche avec extrapolation (V2.4) ; tables `offres_etiquetage`, `offres_taches`, `demande_secteur_tache` ; commandes `python -m app.cli etiqueter` (`--estimer`) et `agreger` ; tests payants `tests_payants/fumee_etiquetage.py` (modèle réel, ~0,05 €) et `tests_payants/estimation_etiquetage.py` (0 €).
- `app/selection_couples.py` + `app/scoring_v2.py` + `app/fiches.py` + `config/fiches.yaml` + `SCORING-V2.md` — sélection des couples secteur x tâche sur les parts extrapolées (V2.5), score v2 (brut et prudent, par du code), fiches (Analyste, Critic, décision par le code), pré-criblage et plafonds de coût ; table `fiches_secteur_tache` ; commande `python -m app.cli fiches` (`--estimer`) ; tests payants `tests_payants/fumee_fiches.py` (les trois rôles modèle sur données réelles, ~0,3-0,4 €).
- `app/concurrence.py` + `config/concurrence.yaml` + `PROCEDURE-V2.6b.md` — concurrence d'un couple secteur x tâche (V2.6) : recherche web bornée derrière `RADAR_CONCURRENCE_WEB` + clé (DÉSACTIVÉE, décision du 2026-10-01), plafond mensuel strict (`recherches_web`, `RADAR_RECHERCHE_WEB_MAX_MOIS`), tables `concurrence_secteur_tache` et `recherches_web`, liste des 30 meilleures fiches et import validé tout-ou-rien pour la procédure V2.6b ; commande `python -m app.cli concurrence` (`--etat`, `--evaluer-web`, `--lister-session`, `--importer`) ; la concurrence évaluée entre dans `scoring_v2`, le pré-criblage et le recalcul de `fiches.py` ; réutilise `FournisseurBraveSearch` de la 3.5.
- `app/cycle_v2.py` + `config/cycle_v2.yaml` — **cycle du worker (V2.8)**, lancé par `python -m app.cli run-forever` : cartographie initiale (`RADAR_CARTOGRAPHIE_INITIALE=1`, secteurs de priorité 1, enveloppe `RADAR_ENVELOPPE_INITIALE_EUR`, reprenable, idempotente) puis régime quotidien (collecte, établissements, agrégation, fiches, étiquetage ; priorités 1 → 2 → 3 ; 2 €/jour) ; résumé d'avancement dans `runs` et les logs ; le pipeline v1 est désactivé par `pipeline_v1_actif: false` (pas supprimé). `priorite` de chaque secteur dans `config/secteurs_tpe.yaml`.
- `config/*.yaml` — secteurs, poids, quotas/budget, tarifs, faisabilité, domaines exclus, sources autorisées.
- `scripts/render_env.py` (V2.8, **exclu du dépôt public**) — pose les variables du worker via l'API Render (clé lue dans `.secrets/render-key.txt`, jamais d'affichage de valeur), relance le service, attend « live » (`--plan`, `--statut`, `--verifier`, `--logs N`) ; `scripts/suivre_cartographie.py` — avancement de la cartographie et contrôle de lecture de `radar_lecture` sur les tables v2 (lecture seule) ; `tests_payants/fumee_cycle.py` — fumée réelle limitée (1 secteur, 5 offres, 1 fiche, ~0,05 €).
- `scripts/` — `deployer_vers_github.sh` (+ contrôles anti-clé et fichiers interdits), `creer_acces_lecture.py`.
- `tests/` (≈ 50 fichiers, 0 € : aucun réseau, aucun modèle) ; `tests_payants/` (`fumee_api.py`, `fumee_reddit.py`, hors suite, à lancer à la main).
- `rapports/` — points d'étape, diagnostics, `metriques/` (mesures JSON datées).
- Documents : `AMELIORATIONS.md` (v1, historique + règles), `RADAR-V2.md` (plan courant), `ARCHITECTURE.md`, `SCORING.md`, `README.md`.

## 2. Où vivent les pièces clés

- **Scout** (`app/roles/scout.py`) : hypothèse (acheteur, douleur, mécanisme, why_now) à partir d'**un seul signal** ; repli heuristique honnête si pas de modèle.
- **Analyst** (`app/roles/analyst.py`) : note 7 critères fixes, ne peut citer que les sources déjà rattachées — toute citation hors périmètre est neutralisée après coup.
- **Critic** (`app/roles/critic.py`) : décision (`rejeter`/`a_verifier`/`eligible_revue_humaine`), n'a jamais accès au score. Même neutralisation des sources hors périmètre.
- **Score** : `app/scoring/engine.py::calculer_score`/`evaluer_critere`. Doit rester en accord avec `SCORING.md` (règle documentée, **pas de test qui compare les deux fichiers**). `score_brut` (critères connus) vs `score_prudent` (inconnu ramené à 0 par défaut).
- **`inferer_secteur`** : `app/pipeline/normalisation.py:22`, fonction pure `(texte) -> secteur`, mots-clés par secteur, premier match gagne, défaut `intersectoriel`. **Point ambigu** : l'ordre de priorité dépend seulement de l'ordre d'écriture du dict Python, non documenté comme choix voulu.
- **Dédoublonnage** : `app/pipeline/dedupe.py`, 3 étages — URL canonique (retire tracking) → empreinte SHA-256 du texte normalisé → similarité lexicale (seuil fusion auto 0,85, seuil revue 0,55, seulement si même secteur + acheteurs compatibles).
- **Budget** : `app/pipeline/budget.py::BudgetTracker`, plafond `quotas.yaml::budget_eur_par_jour` (5 €/j depuis 4.1, + plafond d'appels 260), vérifié **avant** l'appel ; compteur journalier réel (0.7).

## 3. Tables et champs principaux (`app/storage/schema.py`)

| Table | Champs principaux | Rôle |
|---|---|---|
| `runs` | id, mode, debut, fin, statut, couts_json, erreurs_json | Une exécution / le run journalier |
| `sources` | id, url_canonique, domaine, date_collecte, type, empreinte | Page/entrée collectée (upsert idempotent) |
| `signals` | id, source_id, run_id, categorie | Trace qu'une source a été lue dans un run (reprise) |
| `opportunities` | id, titre, acheteur, probleme, secteur, statut, cluster_id | Le dossier central |
| `opportunity_evidence` | id, opportunity_id, source_id, claim, type, independant | Preuves/affirmations rattachées (nom exact, pas « affirmations ») |
| `assessments` | id, opportunity_id, role, payload_json, modele | Sortie JSON complète de chaque rôle |
| `scores` | id, opportunity_id, score_brut, score_prudent, decision_critic | **Append-only**, jamais d'UPDATE |
| `decisions` | id, opportunity_id, auteur, action, justification | Décisions humaines (interface web) |
| `controles` | cle, valeur | Pause partagée cron/web |
| `usage_events` | id, run_id, fournisseur, cout_declare_ou_estime | Journal des appels payants |

Pas de table « affirmations » au sens strict : le mapping réel est `opportunity_evidence` + `assessments`.

## 4. Background Worker

`python -m app.cli run-forever` → `orchestrator.executer_continu`, boucle infinie. **Run journalier** = un run par jour UTC (réutilisé s'il est `en_cours`, sinon créé ; un run d'un jour précédent resté ouvert est clos de force). Plusieurs passages par run (borné par `duree_max_minutes`, 60 min). Le run se termine quand le budget du jour (5 €) ou le plafond d'appels est atteint ; le worker attend le changement de jour (poll 300 s) avant d'en ouvrir un nouveau. **Reprise** : chaque passage reprend les opportunités `nouveau` ; tirage de contrôle des `rejete` à 0 en mode économe (4.1) ; `a_reprendre` retraité en priorité ; jamais `en_analyse`/`incertain`/`a_revoir`/`selectionne`. Toute exception de passage est absorbée sans casser la boucle ; seule `PAUSE_ALL` interrompt un tour.

## 5. Déploiement et suite de tests

`scripts/deployer_vers_github.sh` : clone le dépôt de déploiement public dans un dossier temporaire → `rsync -a --delete` (exclut `.venv`, `__pycache__`, `.pytest_cache`, `*.db`, `.git`) → commit si diff → push. **Point de vigilance signalé (pas théorique)** : (point 0.4 depuis traité : contrôles anti-clé et fichiers interdits ajoutés au script) la liste d'exclusions rsync n'excluait pas `.env` — si un `.env` avec de vrais secrets existe à la racine au moment du lancement, il serait copié vers le dépôt public. `.env` est dans `.gitignore` donc jamais commité dans `labo-ia`, mais le script ne s'appuie pas sur `.gitignore`.

Tests : `PYTHONPATH=. python -m pytest -q`, ≈ 50 fichiers dans `tests/`. Fixture `engine_test` (SQLite temporaire migré) + purge des variables d'env sensibles entre tests. Couvre budget, contrôles, dédoublonnage, métriques, normalisation des sorties modèle (sans réseau), pipeline bout en bout en **mode démo**, garde-fou anti-injection (page hostile testée sur Analyst/Critic), ancres du score, stockage, web. Confirmé : aucun test n'appelle un vrai modèle ni le réseau (mode démo ou mocks). **Absence signalée** : pas de fichier de test dédié à l'adaptateur RSS réel (`app/adapters/rss_adapter.py`) ni à `http.py`.

## 6. Catégories de secteur (`config/secteurs.yaml`, valeurs exactes)

```
operations_petites_entreprises
services_professionnels
flux_documentaires
e_commerce
outils_internes_it
intersectoriel
agents_ia
nouvelles_interfaces
apis_x402
nouveaux_modeles
```

Secteurs exclus : `sante`, `donnees_personnelles_sensibles`. **Point ambigu** : `intersectoriel` est à la fois `SECTEUR_PAR_DEFAUT` codé en dur dans `normalisation.py` et une entrée de `secteurs.yaml` — rien ne vérifie automatiquement que les deux restent cohérents si l'un des deux change.
