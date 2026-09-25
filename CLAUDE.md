# CLAUDE.md — `produits/radar-opportunites/`

> Index court. Détail complet dans [`README.md`](README.md) (mise en route),
> [`ARCHITECTURE.md`](ARCHITECTURE.md) (constat Phase 0, coûts, décisions en
> attente) et [`SCORING.md`](SCORING.md) (ancres du score).
>
> Le plan d'évolution en cours est dans [`AMELIORATIONS.md`](AMELIORATIONS.md)
> — le lire avant toute modification.

Produit interne : radar nocturne qui repère des opportunités économiques où
l'IA change concrètement le coût/délai/qualité d'un problème identifié, les
qualifie via 3 rôles (Scout/Analyst/Critic) et produit des dossiers
traçables (sources, preuves typées, score déterministe, objections).

## Ce dossier-ci n'est PAS déployé tel quel

Ce dossier, dans `labo-ia`, est la **copie de travail** (là où on code et on
teste). Render ne se connecte **jamais** à `labo-ia` directement — ce
monorepo contient des données clients sensibles que Render n'a aucune
raison de pouvoir lire (même convention que `x402-seller`/`jarvis-app` :
chaque produit déployé a son propre petit dépôt externe).

- **Dépôt de déploiement** : [`entreprisedaney33-rgb/radar-opportunites`](https://github.com/entreprisedaney33-rgb/radar-opportunites)
  (GitHub, **public** — Render ne peut pas construire depuis un dépôt privé
  sans un accord GitHub App manuel, voir ARCHITECTURE.md). C'est CE dépôt
  que Render construit et lance.
- **Synchroniser une modification** vers ce dépôt, après avoir testé en
  local : `./scripts/deployer_vers_github.sh "message"` — clone le dépôt de
  déploiement dans un dossier temporaire, y recopie ce dossier-ci (sans
  `.venv`/`__pycache__`/bases locales), commit et pousse. Rien d'automatique :
  à lancer à la main après chaque changement qu'on veut déployer.
- Ne jamais éditer directement dans le dépôt de déploiement : toujours
  modifier ici, tester, puis synchroniser.

État au 2026-09-25 :
- **Phase 0 et Phase 1 faites** : pipeline local complet et testé (38 tests,
  aucun appel réseau/modèle dans les tests).
- **Phase 3 (Render réel)** : dépôt de déploiement créé et synchronisé.
  Postgres + **Background Worker** (`python -m app.cli run-forever`, tourne
  en continu — remplace le Cron Job du matin même, qui ne peut pas tourner
  en continu, voir ARCHITECTURE.md) créés le 2026-09-25.
- **Onglet "Radar"** dans le Jarvis LABO (`produits/jarvis/telecommande-pages/`,
  tenant LABO uniquement) — webhook n8n dédié `jarvis-radar-recap`, voir
  `ARCHITECTURE.md`.
- Pas de compte Apify actif pour ce produit ; pas de clé de recherche web —
  collecte V1 = flux RSS publics (7 sources) + adaptateur de démonstration
  `[DEMO]` (`app/adapters/demo_adapter.py`).
- Base de données : **jamais** `n8n-db` — ce produit a son propre Postgres
  Render, séparé.
