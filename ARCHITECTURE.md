# ARCHITECTURE.md — constat Phase 0 et choix retenus

> Écrit le 2026-09-25, avant toute création de ressource Render facturée.
> Rien de payant n'a été créé pour ce produit à ce stade — voir §"Ce qui reste à
> décider avant la première nuit" en bas de ce fichier.

## Constat (inspection réelle du compte, pas une hypothèse)

**Dépôt** : `labo-ia`, partagé avec Dorian, à jour au moment de l'écriture
(`git pull --ff-only` propre). Ce produit vit dans
`produits/radar-opportunites/`, comme les autres produits internes du labo
(voir `CLAUDE.md` racine, § « Carte du dépôt »).

**Compte Render** (`My Workspace`, `tea-d562aengi27c73dt5of0`) — services
existants, aucun modifié pour ce projet :

| Service | Type | Plan | Rôle actuel |
|---|---|---|---|
| `n8n-self-hosted` | web_service | standard | n8n de production, ne pas toucher |
| `n8n-db` | postgres | 1c-2g (Frankfurt) | base de n8n — **jamais réutilisée pour le radar** |
| `x402-seller` | web_service | 0.5c-512mb | autre produit du labo |
| `x402-seed-hebdo` | cron_job | starter | autre produit du labo |

**Accès modèle** : `anthropic-key.txt` présent dans `.secrets/` → les rôles
Scout/Analyst/Critic peuvent tourner sur de vrais modèles Claude
(`app/adapters/model_client.py`).

**Accès recherche/collecte manquant** : aucune clé de recherche web
(Google/Bing/SerpAPI), aucun compte Apify actif dans ce dépôt (seuls des
projets Apify **archivés**, `archive/autres-projets/apify-actors/`, sans
clé valide). Le labo a par ailleurs 3 Apify Actors internes déjà publiés
(`produits/apify-actors/`), dont un `eu-tender-screen` (appels d'offres UE)
qui pourrait devenir une source pertinente plus tard — non branché en V1,
faute de compte/plafond configuré pour ce produit-ci.

**Conséquence pour la V1** : collecte par flux RSS publics (gratuits, sans
clé) + un adaptateur de démonstration clairement étiqueté `[DEMO]`
(`app/adapters/demo_adapter.py`), conformément à la consigne du cahier des
charges quand un accès manque.

## Choix : Cron Job au départ, Background Worker depuis le 25/09/2026

**Choix initial (25/09/2026, matin)** : un Cron Job Render, un passage par
nuit. Render Workflows a été écarté (distribution non justifiée par ce
volume, pas de planification native, suivi budget/dédoublonnage plus dur à
raisonner entre plusieurs processus).

**Changé le 25/09/2026 (soir), à la demande explicite de Mathéo** : "il ne
faut jamais qu'il s'arrête, il doit continuer à chercher des opportunités
tout le temps." Un Cron Job ne peut pas faire ça — Render l'indique
explicitement dans sa doc : *"Render stops an active run after 12 hours. To
perform tasks that run longer than this (or continuously), instead create a
workflow or background worker."* Symptôme observé avant le changement : des
dossiers restaient bloqués au statut `nouveau` pour toujours dès que le
quota d'analyses d'un passage était atteint, sans être jamais repris.

**Retenu depuis** : un **Background Worker** Render (`python -m app.cli
run-forever`), qui tourne en continu. Architecture : un seul `run` par
journée UTC, à l'intérieur duquel plusieurs **passages** s'enchaînent
indéfiniment (collecte + Scout, puis Analyst/Critic). Le retard non traité
d'un passage (tout ce qui reste au statut `nouveau`) est **toujours** repris
en priorité au passage suivant — voir
`app/pipeline/orchestrator.py::_selectionner_pour_analyse` — donc plus
jamais de dossier abandonné à mi-chemin. Pause entre deux passages : 10 min
si rien de nouveau trouvé (ne pas marteler des flux RSS qui n'ont pas
changé), 30 s sinon. Le run du jour se termine (statut `termine`) dès que le
budget quotidien est atteint ; le worker attend alors le changement de jour
UTC avant de recommencer.

## Ressources Render (créées le 2026-09-25, Cron Job remplacé par un Background Worker le soir même)

Fondateurs prévenus du coût avant chaque création : accord donné à chaque fois.

| Ressource | Rôle | Coût réel |
|---|---|---|
| Postgres `radar-opportunites-db` (`dpg-daqro2navr4c739aopt0-a`, Frankfurt) | tables du §5, séparées de `n8n-db` | plan `basic_256mb` = **6 $/mois** |
| Background Worker `radar-opportunites-worker` | `python -m app.cli run-forever`, tourne en continu | plan `starter` (0,5 CPU/512 Mo), **~7 $/mois** (facturé comme un service toujours actif, pas à la minute comme l'ancien Cron Job) |
| Service web privé | pas créé | Optionnel — le rapport reste consultable via le nouvel onglet "Radar" du Jarvis LABO pour l'instant. |

**Total nouveau : ~13 $/mois de frais fixes**, séparé du budget de 25 €/jour
pour les appels Claude (`config/quotas.yaml`, `budget_eur_par_jour` — la
dépense réelle restera probablement bien en dessous la plupart des jours,
la vraie limite est le volume de signaux disponibles, pas l'argent).

### Dépôt de déploiement

Render ne se connecte jamais à `labo-ia` (données clients sensibles). Le
Background Worker est branché sur
[`entreprisedaney33-rgb/radar-opportunites`](https://github.com/entreprisedaney33-rgb/radar-opportunites)
(public — Render ne peut pas lire un dépôt privé sans un accord GitHub
manuel côté fondateurs, voir §"Décision prise" plus bas). La copie de
travail reste dans `labo-ia/produits/radar-opportunites/` ; synchroniser
avec `./scripts/deployer_vers_github.sh "message"` après chaque changement
à déployer.

### Visualisation : onglet "Radar" du Jarvis LABO (25/09/2026)

Plutôt qu'un service web séparé, le rapport est consultable directement
dans l'app télécommande interne (`produits/jarvis/telecommande-pages/`,
tenant LABO uniquement — jamais exposé à MCS) : nouvel onglet "Radar",
webhook n8n dédié `jarvis-radar-recap` (workflow satellite "🎛️ Jarvis Web ·
Radar (opportunités)", authentifié comme le reste de l'app, requête SQL en
lecture seule sur le Postgres du radar — jamais `n8n-db`). Détail par
dossier au clic (preuves, objection du Critic, prochain test), bandeau
d'état en clair, bouton actualiser, export texte téléchargeable. Voir
`interne/infra/VERROU.md` (section "Jarvis interne") pour le détail
technique du workflow.

### Premier run réel (test manuel avant l'ouverture des quotas de nuit, 2026-09-25)

Lancé via l'API Render (`POST /v1/services/{id}/jobs`), quotas par défaut de
`config/quotas.yaml` (pas de `--dry-run`, sources RSS réelles + Claude
réel) : **10 signaux lus, 10 opportunités créées, 5 analyses et 5 critiques
terminées, budget non dépassé, aucune source indisponible.** Un bug réel
trouvé et corrigé dans la foulée : le modèle enveloppait parfois sa réponse
structurée dans une clé unique (`{"repondre": {...}}` ou
`{"parameter": {...}}`) ou rendait une liste sous forme de chaîne JSON —
`app/adapters/model_client.py::_normaliser_sortie_outil` corrige ça avant
la validation stricte (5 tests dédiés). Avant la correction, ces dossiers
tombaient sur le repli heuristique (jamais un crash, jamais un fait
inventé — juste moins riche) ; la correction a été synchronisée vers le
dépôt de déploiement.

### Décision prise : dépôt de déploiement public

Le nouveau dépôt devait être privé par défaut (le code encode la stratégie
de scoring), mais Render ne peut pas construire depuis un dépôt privé sans
un accord GitHub App donné manuellement depuis le compte du propriétaire
(étape hors API, impossible à faire depuis cette session). Les fondateurs
ont choisi de le passer en public le 2026-09-25, comme `x402-seller`.

## Ce qui reste à décider

1. **Le Background Worker tourne en continu**, `RADAR_PAUSE_ALL=0` — pour
   tout arrêter : passer `RADAR_PAUSE_ALL` à `1` dans les variables
   d'environnement du service sur le dashboard Render (le worker vérifie
   cette valeur avant chaque nouveau passage, il s'arrête proprement au
   passage en cours), ou via le bouton pause de l'interface web
   (`app/web/server.py`, pas encore déployée) une fois qu'elle le sera.
2. **Apify** : à activer seulement si un compte + Actor + plafond par
   exécution sont configurés (`config/sources_autorisees.yaml → apify.actif`).
3. **Interface web dédiée (Phase 3)** : pas déployée — l'onglet "Radar" du
   Jarvis LABO suffit pour l'instant. +7 $/mois si un service web séparé
   est voulu plus tard en plus.
4. **Tarifs modèles** : `app/adapters/model_client.py::PRICES_USD_PAR_MILLION_TOKENS`
   contient des prix indicatifs à vérifier sur la page tarifaire Anthropic
   réelle avant le premier run non `--dry-run`.
