# Radar d'opportunités économiques IA

Trouve, chaque nuit, où un problème économique concret rencontre une
capacité IA récente — et produit des dossiers traçables (sources, preuves,
score, objections) plutôt qu'une liste d'idées. Voir le cahier des charges
complet pour le "pourquoi" ; ce fichier est la mise en route.

## Mise en route locale

```bash
cd produits/radar-opportunites
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp env.example .env   # remplir ANTHROPIC_API_KEY si vous l'avez, sinon laisser vide (mode démo/heuristique)
```

Premier essai, sans rien dépenser ni toucher à une vraie base :

```bash
PYTHONPATH=. python -m app.cli run-once --dry-run --demo
```

Ouvre le fichier `rapport_<run_id>.html` généré dans le dossier courant.
`--dry-run` écrit dans une base SQLite jetable (`radar_dry_run.db`,
recréée à chaque fois) et interdit tout appel modèle payant.

Deuxième essai, avec des sources RSS réelles (toujours gratuit, `--dry-run`
bloque quand même les appels modèle payants) :

```bash
PYTHONPATH=. python -m app.cli run-once --dry-run --max-signals 20 --max-deep-dives 2
```

Pour un run "réel" (base configurée par `DATABASE_URL`, appels modèle
payants si `ANTHROPIC_API_KEY` est renseignée) :

```bash
PYTHONPATH=. python -m app.cli migrate      # crée les tables si nécessaire
PYTHONPATH=. python -m app.cli run-once --max-signals 20 --max-deep-dives 2
```

En production, `run-once` n'est utilisé que pour tester manuellement. Le
Background Worker tourne avec `run-forever` (un seul run par journée UTC,
plusieurs passages enchaînés indéfiniment, jamais de dossier laissé à
mi-chemin — voir `ARCHITECTURE.md`) :

```bash
PYTHONPATH=. python -m app.cli run-forever   # tourne sans jamais s'arrêter (Ctrl+C pour stopper en local)
```

## Tests

```bash
PYTHONPATH=. python -m pytest -q
```

Aucun test n'appelle un vrai modèle ni le réseau : les garanties de
sécurité (citation hors périmètre neutralisée, dédoublonnage, budget,
reprise après panne, PAUSE_ALL) sont vérifiées mécaniquement, sans dépendre
d'un fournisseur externe.

## Déployer une modification (Phase 3, Render réel)

Render ne se connecte jamais à `labo-ia` (voir `ARCHITECTURE.md`). Après
avoir testé en local :

```bash
./scripts/deployer_vers_github.sh "ce que ce déploiement change"
```

Synchronise ce dossier vers
[`entreprisedaney33-rgb/radar-opportunites`](https://github.com/entreprisedaney33-rgb/radar-opportunites)
(auto-déploiement activé côté Render sur push vers `main`).

## Interface interne (optionnelle, Phase 3)

```bash
RADAR_UI_PASSWORD=change-moi PYTHONPATH=. python -m app.web.server
```

Puis `http://localhost:8000` (identifiant HTTP Basic quelconque, mot de
passe = `RADAR_UI_PASSWORD`). Liste des runs, dossier détaillé, export
CSV/JSON, décision humaine, bouton pause.

## Où sont les décisions importantes

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — constat Phase 0 (compte Render
  réel, accès disponibles/manquants), choix Background Worker (§"Choix"),
  ressources créées et leur coût, ce qui reste à décider.
- [`SCORING.md`](SCORING.md) — les ancres exactes du score, tenues
  identiques au code de `app/scoring/engine.py`.
- `config/*.yaml` — secteurs, poids du score, quotas/budget nocturne,
  sources autorisées. Rien de tout ça n'est en dur dans le code.
- `robots.txt` : la collecte du Scout (recherche) et l'Enquêteur suivent la
  même politique — extraits de flux de recherche oui (jamais fetchés ni
  interprétés), crawl direct d'une page interdite par `robots.txt` non
  (Reddit, notamment : `Disallow: /` — jamais fetché nulle part dans ce
  projet, voir `app.enqueteur.fetch.FOURNISSEURS_EXTRAIT_DIRECT`).

## Variables d'environnement

Voir [`env.example`](env.example) pour la liste exacte et ce que chacune
fait en son absence (le système ne plante jamais faute d'accès : il
dégrade en mode démo/heuristique, clairement marqué comme tel dans les
rapports).
