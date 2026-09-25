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

## Ressources Render nouvelles proposées (aucune créée à ce stade)

| Ressource | Rôle | Ordre de grandeur (à vérifier sur le compte réel avant activation) |
|---|---|---|
| 1 nouveau PostgreSQL, séparé de `n8n-db` | tables du §5 (runs, sources, signals, opportunities, …) | Render facture ses plans Postgres au mois ; le plan le plus bas payant tourne autour de quelques euros/mois — **à confirmer sur la page tarifaire Render au moment de la création**, pas une estimation fiable ici. |
| 1 nouveau Cron Job | `run-once` nocturne, fenêtre 2h max | Facturé à la minute d'exécution sur l'instance choisie (le plus petit type suffit largement) ; avec ~60h d'exécution max par mois (2h × 30 nuits), le coût mensuel est une fraction du prix d'une instance équivalente tournant 24/7 — **montant exact à vérifier au moment de l'activation**, pas garanti ici. |
| 1 petit service web privé (optionnel, Phase 3) | sert `app/web/server.py` (rapport HTML + export + bouton pause), protégé par mot de passe | Le plus petit plan web payant de Render ; **seulement si les fondateurs veulent l'interface en ligne plutôt qu'un rapport HTML local** — sinon, le rapport peut rester un fichier généré à chaque run, consulté en local. |

**Aucun de ces trois éléments n'est créé avant que les fondateurs valident
un budget et un plafond nocturne** (§4 du cahier des charges, et règle
générale de cette session : toute ressource facturée nouvelle demande un
accord explicite avant activation).

## Ce qui reste à décider avant la première nuit automatisée

1. **Budget nocturne réel** : `config/quotas.yaml` propose 5 €/nuit, 100
   signaux, 15 opportunités, 5 analyses, 5 critiques, 2h — ce sont des
   valeurs de départ modifiables, pas une estimation de coût fournisseur.
2. **Apify** : à activer seulement si un compte + Actor + plafond par
   exécution sont configurés (`config/sources_autorisees.yaml → apify.actif`).
3. **Interface web (Phase 3)** : rapport HTML local suffisant pour démarrer,
   ou service web Render dédié tout de suite ? Change le nombre de
   ressources payantes à créer.
4. **Tarifs modèles** : `app/adapters/model_client.py::PRICES_USD_PAR_MILLION_TOKENS`
   contient des prix indicatifs à vérifier sur la page tarifaire Anthropic
   réelle avant le premier run non `--dry-run`.
