# Carte du dépôt — `produits/radar-opportunites`

Écrite en sous-étape 0.1 (AMELIORATIONS.md), lecture seule, rien modifié.
À relire au début de chaque sous-étape suivante.

## 1. Arborescence et rôle de chaque module

- `app/cli.py` — commandes `run-once` / `run-forever` / `migrate`.
- `app/config.py` — config YAML + variables d'env (`Settings`, cache).
- `app/metriques.py` — `python -m app.metriques`, lecture seule, 0 appel modèle/réseau.
- `app/models_schemas.py` — schémas Pydantic (frontière de confiance Scout/Analyst/Critic).
- `app/pipeline/orchestrator.py` — cœur : `executer_run` (un passage) et `executer_continu` (worker infini).
- `app/pipeline/budget.py` — `BudgetTracker` / `BudgetDepasse`.
- `app/pipeline/dedupe.py` — URL canonique, empreinte, similarité, fusion.
- `app/pipeline/normalisation.py` — `inferer_secteur` (mots-clés).
- `app/roles/{scout,analyst,critic}.py` + `prompts_communs.py` — les 3 rôles.
- `app/scoring/engine.py` — moteur de score, lié à `SCORING.md`.
- `app/storage/{schema,repo,db}.py` — tables, accès données, `get_engine`/`migrer`.
- `app/adapters/{base,rss_adapter,demo_adapter,http,model_client}.py` — collecte RSS réelle, données démo, retry HTTP, appel modèle.
- `app/web/server.py` — interface Flask interne (Basic Auth), pause, décisions humaines.
- `app/reports/html_report.py` — rapport HTML du matin (échappé).
- `config/*.yaml` — secteurs, poids de scoring, quotas/budget, sources autorisées.
- `scripts/deployer_vers_github.sh` — synchronise vers le dépôt de déploiement public.

## 2. Où vivent les pièces clés

- **Scout** (`app/roles/scout.py`) : hypothèse (acheteur, douleur, mécanisme, why_now) à partir d'**un seul signal** ; repli heuristique honnête si pas de modèle.
- **Analyst** (`app/roles/analyst.py`) : note 7 critères fixes, ne peut citer que les sources déjà rattachées — toute citation hors périmètre est neutralisée après coup.
- **Critic** (`app/roles/critic.py`) : décision (`rejeter`/`a_verifier`/`eligible_revue_humaine`), n'a jamais accès au score. Même neutralisation des sources hors périmètre.
- **Score** : `app/scoring/engine.py::calculer_score`/`evaluer_critere`. Doit rester en accord avec `SCORING.md` (règle documentée, **pas de test qui compare les deux fichiers**). `score_brut` (critères connus) vs `score_prudent` (inconnu ramené à 0 par défaut).
- **`inferer_secteur`** : `app/pipeline/normalisation.py:22`, fonction pure `(texte) -> secteur`, mots-clés par secteur, premier match gagne, défaut `intersectoriel`. **Point ambigu** : l'ordre de priorité dépend seulement de l'ordre d'écriture du dict Python, non documenté comme choix voulu.
- **Dédoublonnage** : `app/pipeline/dedupe.py`, 3 étages — URL canonique (retire tracking) → empreinte SHA-256 du texte normalisé → similarité lexicale (seuil fusion auto 0,85, seuil revue 0,55, seulement si même secteur + acheteurs compatibles).
- **Budget** : `app/pipeline/budget.py::BudgetTracker`, plafond `quotas.yaml::budget_eur_par_jour` (25 €/j), vérifié **avant** l'appel, jamais après.

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

`python -m app.cli run-forever` → `orchestrator.executer_continu`, boucle infinie. **Run journalier** = un run par jour UTC (réutilisé s'il est `en_cours`, sinon créé ; un run d'un jour précédent resté ouvert est clos de force). Plusieurs passages par run (borné par `duree_max_minutes`, 60 min). Le run se termine quand le budget du jour (25 €) est atteint ; le worker attend le changement de jour (poll 300 s) avant d'en ouvrir un nouveau. **Reprise** : chaque passage reprend systématiquement toutes les opportunités `nouveau` (peu importe quel passage les a créées) + un échantillon de 10 % des `rejete` pour contrôle ; jamais `en_analyse`/`incertain`/`a_revoir`/`selectionne`. Toute exception de passage est absorbée sans casser la boucle ; seule `PAUSE_ALL` interrompt un tour.

## 5. Déploiement et suite de tests

`scripts/deployer_vers_github.sh` : clone le dépôt de déploiement public dans un dossier temporaire → `rsync -a --delete` (exclut `.venv`, `__pycache__`, `.pytest_cache`, `*.db`, `.git`) → commit si diff → push. **Point de vigilance signalé (pas théorique)** : la liste d'exclusions rsync **n'exclut pas `.env`** — si un `.env` avec de vrais secrets existe à la racine au moment du lancement, il serait copié vers le dépôt public. `.env` est dans `.gitignore` donc jamais commité dans `labo-ia`, mais le script ne s'appuie pas sur `.gitignore`.

Tests : `PYTHONPATH=. python -m pytest -q`, 10 fichiers dans `tests/`. Fixture `engine_test` (SQLite temporaire migré) + purge des variables d'env sensibles entre tests. Couvre budget, contrôles, dédoublonnage, métriques, normalisation des sorties modèle (sans réseau), pipeline bout en bout en **mode démo**, garde-fou anti-injection (page hostile testée sur Analyst/Critic), ancres du score, stockage, web. Confirmé : aucun test n'appelle un vrai modèle ni le réseau (mode démo ou mocks). **Absence signalée** : pas de fichier de test dédié à l'adaptateur RSS réel (`app/adapters/rss_adapter.py`) ni à `http.py`.

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
