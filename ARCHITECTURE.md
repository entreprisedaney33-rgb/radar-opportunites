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

## Choix : Cron Job Render seul, pas de Workflows

Le volume nocturne visé (100 signaux, 15 opportunités, 5 analyses
approfondies, 2h) est largement dans les capacités d'un seul processus
Python séquentiel. Render Workflows apporterait de la distribution
(Scout/Analyst/Critic en parallèle sur plusieurs machines) mais :
- ajoute une dépendance et une complexité de déploiement non justifiées par
  ce volume ;
- ne planifie pas nativement ses exécutions à la date de rédaction du
  cahier des charges (il faudrait de toute façon garder un Cron Job
  déclencheur) ;
- rendrait le suivi de budget/dédoublonnage plus difficile à raisonner (état
  partagé entre plusieurs processus).

**Retenu pour la V1** : un seul Cron Job Render, `python -m app.cli run-once`,
état entièrement en base (aucun disque persistant requis — conforme aux
contraintes des Cron Jobs Render). Migration vers Workflows seulement si un
volume nocturne futur le justifie.

## Ressources Render créées le 2026-09-25 (tarifs vérifiés sur render.com/pricing ce jour-là)

Fondateurs prévenus du coût avant création (échange du 2026-09-25) : accord donné.

| Ressource | Rôle | Coût réel |
|---|---|---|
| Postgres `radar-opportunites-db` (`dpg-daqro2navr4c739aopt0-a`, Frankfurt) | tables du §5, séparées de `n8n-db` | plan `basic_256mb` = **6 $/mois** |
| Cron Job `radar-opportunites-nightly` (`crn-daqrq2bncjis73b7bq90`) | `python -m app.cli run-once`, 01:00 UTC (~03:00 Europe/Paris en été, 02:00 en hiver) | plan `starter` (0,5 CPU/512 Mo), facturé à la seconde, **~0,60 $/mois** pour 2h/nuit max |
| Service web privé | pas créé | Optionnel — le rapport reste un fichier HTML généré à chaque run pour l'instant. |

**Total nouveau : ~7 $/mois de frais fixes**, séparé du budget de 25 €/nuit
pour les appels Claude (relevé de 5 à 25 € le 2026-09-25 à la demande de
Mathéo — un seul passage par nuit, pas plusieurs ; s'arrête dès que le
budget ou un des quotas de volume est atteint ; voir `config/quotas.yaml`
pour le détail et pourquoi la dépense réelle restera probablement bien en
dessous de 25 € la plupart des nuits).

### Dépôt de déploiement

Render ne se connecte jamais à `labo-ia` (données clients sensibles). Le
Cron Job est branché sur
[`entreprisedaney33-rgb/radar-opportunites`](https://github.com/entreprisedaney33-rgb/radar-opportunites)
(public — Render ne peut pas lire un dépôt privé sans un accord GitHub
manuel côté fondateurs, voir §"Décision prise" plus bas). La copie de
travail reste dans `labo-ia/produits/radar-opportunites/` ; synchroniser
avec `./scripts/deployer_vers_github.sh "message"` après chaque changement
à déployer.

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

1. **Le Cron Job est actif dès ce soir (01:00 UTC)**, `RADAR_PAUSE_ALL=0` —
   pour le mettre en pause après avoir relu le dossier de test du
   2026-09-25 : passer `RADAR_PAUSE_ALL` à `1` dans les variables
   d'environnement du service sur le dashboard Render, ou lancer
   l'interface web (`app/web/server.py`) une fois déployée pour le bouton
   pause.
2. **Apify** : à activer seulement si un compte + Actor + plafond par
   exécution sont configurés (`config/sources_autorisees.yaml → apify.actif`).
3. **Interface web** : pas encore déployée (rapport HTML seul pour
   l'instant) — +7 $/mois si les fondateurs la veulent en ligne.
3. **Interface web (Phase 3)** : rapport HTML local suffisant pour démarrer,
   ou service web Render dédié tout de suite ? Change le nombre de
   ressources payantes à créer.
4. **Tarifs modèles** : `app/adapters/model_client.py::PRICES_USD_PAR_MILLION_TOKENS`
   contient des prix indicatifs à vérifier sur la page tarifaire Anthropic
   réelle avant le premier run non `--dry-run`.
