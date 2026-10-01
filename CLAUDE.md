# CLAUDE.md — `produits/radar-opportunites/`

> Index court. Détail complet dans [`README.md`](README.md) (mise en route),
> [`ARCHITECTURE.md`](ARCHITECTURE.md) (constat Phase 0, coûts, décisions en
> attente) et [`SCORING.md`](SCORING.md) (ancres du score).
>
> **Le plan en cours est [`RADAR-V2.md`](RADAR-V2.md)** (depuis le 2026-10-01) — le lire
> avant toute modification. [`AMELIORATIONS.md`](AMELIORATIONS.md) (version 1) reste
> l'historique et la source des règles communes (§0.2, §3, §5), inchangées.

## Mission (version 2)

Cartographier la demande des TPE/PME françaises pour des services IA livrables à deux, en
Gironde d'abord. Preuves chiffrées et sourcées, jamais d'idée sans source. Mathéo prospecte,
le radar dit où.

Description de la version 1 (toujours en base, archivée) : radar nocturne qui repère des opportunités économiques où
l'IA change concrètement le coût/délai/qualité d'un problème identifié, les
qualifie via 3 rôles (Scout/Analyst/Critic) et produit des dossiers
traçables (sources, preuves typées, score déterministe, objections).

**État V2.6 (2026-10-01)** : la concurrence des fiches a son fournisseur web (`app/concurrence.py`), **éteint** — décision de
Mathéo : aucune clé de moteur payant pour l'instant ; le critère « concurrence » reste « non évalué » (0 point) et se remplit en
session par la procédure [`PROCEDURE-V2.6b.md`](PROCEDURE-V2.6b.md), à lancer après V2.8. Rien n'est déployé.

**État V2.8 (2026-10-01/02)** : le worker exécute le **cycle v2** (`app/cycle_v2.py`, `config/cycle_v2.yaml`) et plus jamais le pipeline v1
(`pipeline_v1_actif: false`, rallumable par config). Cartographie initiale si `RADAR_CARTOGRAPHIE_INITIALE=1` (secteurs de priorité 1 de
`config/secteurs_tpe.yaml`, enveloppe `RADAR_ENVELOPPE_INITIALE_EUR`=10 €), puis régime quotidien à 2 €/jour, priorités 1 → 2 → 3. Variables Render
posées par `scripts/render_env.py` (jamais à la main). Suivi : `python scripts/suivre_cartographie.py [--acces]`. Voir le Journal V2.8 de `RADAR-V2.md`.

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

État au 2026-09-29 (sous-étape 4.1) : mode économe (5 €/jour, 260 appels
approfondis, tirage de contrôle 0), bloc de faisabilité `accessible_solo`
(`app/faisabilite.py`, `config/faisabilite.yaml`), recalcul des scores +
statut `archive_faible` sous 50 (`app/recalcul.py`, interrupteurs Render
`RADAR_RECALCUL_4_1` / `RADAR_FAISABILITE_REPRISE`, éteints par défaut).
Reddit (4.0) : optionnelle, en attente d'identifiants ; moteur web payant reporté.

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
