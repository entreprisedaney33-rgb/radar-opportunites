# Point d'étape à 24 h — 27/09/2026

Sous-étape 3.12 d'`AMELIORATIONS.md`. Lecture seule (rôle `radar_lecture`), aucun appel modèle, aucune modification de code, aucun déploiement.

**Instant de la mesure : 2026-09-27, 14:41 UTC.**

---

## 0. Périmètre réel — écart avec la demande

La demande initiale porte sur la fenêtre « depuis le déploiement de 3.11 (26/09 après-midi UTC) ». **Cette fenêtre n'existe pas** : la sous-étape 3.11 n'a jamais été synchronisée vers le dépôt public ni déployée sur Render.

Preuves (pas une hypothèse) :
- Le tableau §8 marquait déjà 3.11 « PAS ENCORE déployé, en attente de l'OK de Mathéo », et aucun commit « Journal de déploiement » ne suit `b91f2074` (le commit de code de 3.11) — contrairement à 3.7, 3.9 et 3.10, qui en ont chacun un.
- La base de production ne contient ni la colonne `opportunities.mots_cles_en`/`mots_cles_fr`, ni la table `etats_disjoncteur_enqueteur` (vérifié directement via `information_schema.columns`, `radar_lecture`). `python -m app.metriques` échoue même dessus (`UndefinedColumn: opportunities.mots_cles_en does not exist`), preuve supplémentaire que le code local a avancé au-delà du schéma réellement en production.

**Ce rapport mesure donc depuis le dernier déploiement réel : la sous-étape 3.10, commit public `ebafdc6`, 2026-09-26 ~12:52 UTC.** C'est presque la même fenêtre temporelle que celle demandée (l'après-midi du 26/09), seulement attribuée à la bonne sous-étape. Conséquence pratique : le fournisseur Reddit de l'Enquêteur tourne toujours **actif** (3.11 devait le désactiver par défaut) et les requêtes de l'Enquêteur restent composées à partir du repli par mots-clés dérivés, jamais des `mots_cles_en`/`mots_cles_fr` structurés du Scout.

**En creusant la chronologie pour répondre au point 1, une deuxième chose, plus grave et non demandée, est apparue : une panne totale et silencieuse des appels au modèle, en cours depuis ~25 h. Elle domine tout le reste de ce rapport — voir §1, §3, §5, §6, §8 et surtout §9.**

---

## 1. Chronologie

**Deux runs dans la fenêtre, aucun redémarrage détecté** (un seul run par jour, comme le veut la correction de la sous-étape 0.7) :

| Run | Début (UTC) | Fin (UTC) | Statut | Coût réel enregistré |
|---|---|---|---|---|
| `8e908bc5…` (26/09) | 00:00:04 | 15:25:56 | `termine`, `budget_atteint=true` | 18,84 € (`couts_json`) |
| `541504ec…` (27/09) | 00:00:57 | 13:16:11 | `termine`, `budget_atteint=true` | 0,00 € (`couts_json`) — voir plus bas |

**Coût par heure, depuis le déploiement de 3.10 (12:52 UTC le 26/09)** — colonne « appels » = tous rôles confondus :

| Heure (UTC) | Coût (€) | Appels |
|---|---|---|
| 26/09 12h | 1,3868 (portion 12:52→13:00) | 259 |
| 26/09 13h | 0,0000 | 331 |
| 26/09 14h | 0,0000 | 504 |
| 26/09 15h (jusqu'à 15:26, fin du run) | 0,0000 | 174 |
| 27/09 00h à 13h (13 heures) | 0,0000 | 3 917 (325 à 472/h) |

**Le coût réel bascule à zéro à 12:58:29 UTC le 26/09** (dernier appel Analyst/Critic avec un coût non nul, `tokens_in`/`tokens_out` réels), soit 6 minutes après le déploiement confirmé de 3.10. Tous les appels suivants — jusqu'à maintenant, 25 h plus tard — ont un coût de 0 € et aucun token compté. Ce n'est pas un problème d'affichage : `usage_events.tokens_in`/`tokens_out` valent `NULL` pour ces lignes, signe que l'appel API a échoué avant même de recevoir une réponse facturable (détail causal en §3 et §9).

**Heure d'arrêt sur budget aujourd'hui (27/09) : 13:16:11 UTC**, dernière ligne `usage_events` de la journée, fin du run au même instant. Ce n'est **pas** le plafond en euros qui a arrêté le run (impossible : le coût cumulé réel du jour est de 0,00 €, largement sous les 25 €) — c'est le second garde-fou indépendant posé en sous-étape 0.7, le plafond de **1 300 appels au modèle approfondi par jour** (Analyst + Critic) : exactement 650 appels Analyst + 650 Critic = 1 300/1 300 (100 %) au moment de l'arrêt, les deux jours 26/09 et 27/09. Un appel qui échoue est compté comme un appel (`self.budget.enregistrer_reel(appels=1, ...)` est appelé même dans la branche d'échec, `app/adapters/model_client.py:238-242`) — c'est ce compteur, jamais prévu pour jouer ce rôle, qui empêche aujourd'hui le worker de tourner indéfiniment en pure perte.

**Nombre de passages : estimation, pas une mesure exacte** — `resume_json` du run n'est réécrit qu'à chaque passage et ne garde donc que le dernier (déjà noté dans l'audit du 26/09) ; aucune autre table ne compte les passages. En divisant les 878 appels Analyst réels par le plafond de 15 analyses/passage (`max_analyses_par_passage`), on obtient au moins **≈ 59 passages** sur la fenêtre — un plancher, pas un compte exact.

**Redémarrages : aucun détecté.** Un seul run par jour calendaire UTC, sans run intermédiaire — comportement normal, sans les redémarrages multiples observés certains jours précédents.

---

## 2. Entonnoir

- **494 opportunités créées** dans la fenêtre (toutes issues de flux `douleur`, comme attendu depuis l'étape 1). Répartition par flux d'origine (top 5 sur 25 flux actifs) : `r/smallbusiness` (21), `r/sysadmin` (15), recherche HN « manually » (15), recherche HN « tedious » (15), `r/Accounting` (13) — cohérent avec le lexique de douleur de la sous-étape 1.2, aucun flux `offre` n'a créé de dossier.
- **Statuts au moment de la mesure, dossiers de la fenêtre uniquement** : `incertain` 464 (94,0 %), `enquete_terminee` 30 (6,0 %, en attente d'Analyst). Aucun dossier au statut `nouveau` ni `en_analyse` — la file d'attente en amont du Scout est vide, l'Enquêteur et le Scout absorbent le flux sans retard.
- **Décisions Critic dans la fenêtre : `a_verifier` 464 (100 %), 0 `rejeter`, 0 `eligible_revue_humaine`.** Ce n'est pas un signe de bonne qualité — voir §3 et §5 : ces 464 décisions sont produites par le repli sans modèle, pas par un vrai jugement du Critic. Le fait qu'aucune ne soit `rejeter` s'explique par le motif fixe du repli (« Mode sans modèle : impossible de challenger le dossier automatiquement », toujours `a_verifier`), pas par une amélioration réelle de la qualité des dossiers.
- **File d'attente, vue d'ensemble (toutes dates confondues, pas seulement la fenêtre)** : 1 602 dossiers au statut `incertain` au total, 30 `enquete_terminee`, 14 `rejete`, 1 `en_analyse`. La file de dossiers `incertain` en attente de revue humaine continue de grossir jour après jour (le Constat 1 du diagnostic initial — le Critic ne peut jamais dire « éligible » — reste vrai, et depuis 25 h, plus aucun jugement réel n'est même tenté).

---

## 3. Fiabilité des sorties (comparaison avec la 3.10)

La demande initiale comparait au 78 % (Critic) / 19 % (Analyst) d'échec de schéma mesuré le 26/09 avant la 3.10. **Ce chiffre n'est plus comparable tel quel** : depuis le déploiement de 3.10, le type d'échec a changé de nature.

| Rôle | Appels dans la fenêtre | `issue = perdue` (échec) | Taux d'échec | Coût perdu |
|---|---|---|---|---|
| Analyst | 880 | 878 | **99,77 %** | 0,00 € (l'appel échoue avant tout token facturable) |
| Critic | 880 | 878 | **99,77 %** | 0,00 € |
| Scout | 496 | 496 | **100 %** | 0,00 € |

**Le mécanisme est différent de celui corrigé en 3.10.** En 3.10, l'échec était un rejet de schéma *après* une réponse reçue et facturée (100 % des échecs de l'époque, 0 échec réseau) — corrigé par `"strict": True` + une relance. Ici, l'échec se produit **avant** toute réponse : `client.messages.create()` lève une exception, capturée par la branche `except Exception` de `_un_appel` (`app/adapters/model_client.py:236-243`), qui écrit une ligne `usage_events` avec `tokens_in=NULL`, `tokens_out=NULL`, `cout_reel=0.0`, `issue=ISSUE_PERDUE` — et **ne relance jamais** (ce chemin est explicitement non-relancé, voir le commentaire du code : « erreur réseau/API — déjà journalisé et jamais relancé »). Aucune ligne `issue=relancee` n'existe dans toute la fenêtre : la totalité des 1 756 échecs Analyst+Critic sont des échecs de première tentative, jamais un rejet de schéma après coup.

Grâce aux replis déterministes déjà prévus dans le code pour ce cas exact (docstring de `scout.py` : « le repli s'utilise quand l'accès manque, quand le budget est [dépassé] ») :
- `_scout_heuristique` produit un titre/texte sans citer aucun modèle — 496 opportunités créées ainsi, `assessments.modele = 'heuristique'`.
- `_critic_heuristique` (et son équivalent côté Analyst) renvoie un motif fixe, retrouvé tel quel dans `assessments.payload_json` pour les 878 dossiers concernés : *« Mode sans modèle : impossible de challenger le dossier automatiquement. »*

**Aucun dossier n'a jamais été bloqué** (conforme au garde-fou « jamais de dossier qui reste coincé ») — mais depuis 25 h, ce que le pipeline produit n'est plus une analyse : c'est un repli honnête qui se déclare lui-même sans matière.

---

## 4. L'Enquêteur (3.9, et 3.11 non déployée)

**Sources par dossier (dossiers créés dans la fenêtre)** : min 1, médiane 1, max 12, **80,36 % à une seule source** — en net progrès par rapport aux 98,26 % mesurés le 26/09 matin (avant la panne du §3), et ce malgré la panne : l'Enquêteur ne dépend pas du modèle Anthropic pour chercher/récupérer des pages, seulement de fournisseurs de recherche/fetch HTTP, tous restés fonctionnels.

**Résultats par requête et pages stockées, par fournisseur/étiquette** (top domaines) :

| Domaine | Étiquette | Pages stockées |
|---|---|---|
| `www.reddit.com` | `extrait_flux` | 47 |
| `news.ycombinator.com` | `preuve_enquete` | 38 |
| `news.ycombinator.com` | `prix` | 15 |
| `gaiabot.lol` | `prix` | 14 |
| `github.com` | `preuve_enquete` | 14 |
| `github.com` | `prix` | 9 |
| … (une longue traîne de domaines à 1 page chacun) | | |

**Appels réseau de l'Enquêteur, par fournisseur** (extrait, hôtes les plus actifs) :

| Fournisseur | Appels | 429 | Autres erreurs | Succès | Taux de succès |
|---|---|---|---|---|---|
| `enqueteur_recherche:algolia_hn:comment` | 1 616 | 0 | 1 | 1 615 | 99,9 % |
| `enqueteur_recherche:algolia_hn:ask_hn` | 1 605 | 0 | 1 | 1 604 | 99,9 % |
| `enqueteur_recherche:reddit` | 993 | 485 | 0 | 508 | 51,2 % |
| `enqueteur_fetch:magasin_interne` (tous domaines) | ≈ 400 | 0 | 0 | ≈ 400 | 100 % |

Le fournisseur Reddit de l'Enquêteur reste actif dans cette fenêtre (3.11, qui devait le désactiver par défaut, n'est pas déployée — §0) : 51,2 % de succès HTTP, en net progrès par rapport au 37 % (avec 0 page utilisable) mesuré le 26/09 matin, cohérent avec le changement « Reddit sans crawl » de la sous-étape 3.9 (extrait de recherche stocké directement, jamais de fetch de page bloqué par `robots.txt`) — donc, contrairement à l'audit du 26/09, ce fournisseur produit désormais réellement des preuves (47 pages `extrait_flux`).

**Requêtes évitées / disjoncteur** : ces deux mécanismes viennent de la sous-étape 3.11 (`requetes_evitees_jour`, `etats_disjoncteur_enqueteur`), **non déployée** — colonnes absentes en base, rien à mesurer ici (voir §0).

**Consommation des plafonds** :

| Jour | Requêtes de recherche | Fetchs de pages |
|---|---|---|
| 26/09 (depuis 12:52 UTC) | 3 121 | 132 |
| 27/09 (jour complet) | 3 500 | 799 |

Le plafond configuré de requêtes de recherche est de 3 000/jour (relevé en 3.9). Les deux jours le dépassent légèrement (+4 % le 26/09, +17 % le 27/09) — la 3ᵉ millième requête du 27/09 est survenue à **09:34:32 UTC**, plusieurs heures avant la fin réelle des requêtes ce jour-là. Mesuré via `journal_http` (comptage de toutes les lignes `enqueteur_recherche%`), pas via le compteur interne exact du budget — l'écart est trop faible pour conclure à un vrai dépassement du garde-fou §3.4 sans vérifier le compteur interne lui-même (hors périmètre lecture-seule de cette sous-étape), mais assez systématique (les deux jours) pour être signalé. Le plafond de fetchs de pages (1 000-1 500 selon le jour) n'est, lui, jamais approché (799/1 500 au maximum).

---

## 5. Scores

**La distribution du score prudent est réduite à un point unique : 878/878 scores produits dans la fenêtre valent exactement 0,0/100** (min = médiane = p90 = max = 0,0 ; 0 dossier > 50, 0 dossier > 60). Ce n'est pas une régression du calcul de score : c'est la conséquence directe et attendue de la panne du §3 — sans aucune preuve retenue par l'Analyst (repli sans modèle), le score déterministe de `SCORING.md` calcule honnêtement 0 point sur les 7 critères, chacun avec `niveau_preuve="inconnu"` :

| Critère | Points max | Points obtenus (moyenne sur les 878 dossiers) |
|---|---|---|
| `probleme_frequence_cout` | 20 | 0,0 |
| `acheteur_disposition_payer` | 20 | 0,0 |
| `gain_realisable_ia` | 15 | 0,0 |
| `acces_clients` | 15 | 0,0 |
| `concurrence_differenciation` | 10 | 0,0 |
| `economie_cout_lancement` | 10 | 0,0 |
| `faisabilite_risque` | 10 | 0,0 |

**Comparaison demandée (25/09 : max 45 ; 26/09 matin : max 52,5)** : la fenêtre actuelle est donc, pour la première fois depuis le début de ce plan, **en dessous** de ces deux repères — pas parce que le plafond de preuve (Constat 1 du diagnostic initial) s'est resserré, mais parce qu'aucune preuve n'est plus jamais évaluée du tout depuis 25 h.

**Top 10 « par score » : sans objet tel que demandé** — les 878 scores de la fenêtre sont tous strictement égaux (0,0), un classement serait arbitraire. À titre d'illustration, 10 dossiers réels créés dans la fenêtre (ordre indifférent, tous à 0,0/`a_verifier`) :

| Titre (tronqué) | Secteur | Sources |
|---|---|---|
| Anyone using Zoom Phone with Gibraltar numbers?… | intersectoriel | 1 |
| Replacing 2x FortiGate 60F, what options do I have?… | intersectoriel | 1 |
| Boss is pushing for certs… | outils_internes_it | 1 |
| P800 RAID-0 Bad Block (strategy)… | intersectoriel | 1 |
| « Tax write-offs » as profits… | flux_documentaires | 1 |
| Quel niveau de RTO-RPO pour vos ERP ?… | outils_internes_it | 1 |
| Comment tenir devant un ordinateur toute la journée ?… | intersectoriel | 1 |
| Inestabilidad recurrente en red… | intersectoriel | 1 |
| Plateforme DCP-Global ou équivalent de gestion de prestataires indépendants | operations_petites_entreprises | 1 |
| Plateformes/outils pour freelancers face à la substitution IA | intersectoriel | 1 |

**Détail par critère et objections du Critic, pour les 3 premiers** : identique pour les trois — les 7 critères listés plus haut, tous à `fraction=null`, `points=null`, `niveau_preuve="inconnu"` ; `objections=[]` ; `faits_contestes=[]` ; `recherches_supplementaires=["revue humaine (mode sans modèle : aucune critique automatique)"]` ; `motif="Mode sans modèle : impossible de challenger le dossier automatiquement."` — le Critic n'a émis aucune objection non pas parce que le dossier est solide, mais parce qu'il ne s'est jamais réellement exécuté.

---

## 6. Secteur

| Provenance | Dossiers |
|---|---|
| `defaut` (secteur par défaut du flux) | 414 (83,8 %) |
| `flux` | 80 (16,2 %) |
| `citation_verifiee` | **0 (0,0 %)** |

**Conséquence directe et non anticipée de la panne du §3, sur l'étape 2 cette fois** : la sous-étape 2.2 (« le Scout propose son propre secteur + citation ») dépend d'un appel modèle réussi du Scout — depuis 25 h, 0/496 tentatives Scout réussissent, donc 0 secteur par citation vérifiée dans toute la fenêtre. Tout le secteur affiché aujourd'hui vient uniquement du `secteur_par_defaut` de chaque flux (`sources.yaml`, posé en 1.1) ou du secteur du flux lui-même — jamais d'une citation lue et vérifiée dans le texte, contrairement à ce que l'étape 2 est censée apporter.

**Répartition par secteur** :

| Secteur | Dossiers | Part |
|---|---|---|
| `intersectoriel` | 281 | 56,9 % |
| `operations_petites_entreprises` | 68 | 13,8 % |
| `flux_documentaires` | 67 | 13,6 % |
| `e_commerce` | 35 | 7,1 % |
| `outils_internes_it` | 27 | 5,5 % |
| `nouveaux_modeles` | 11 | 2,2 % |
| `services_professionnels` | 5 | 1,0 % |

**Part hors intersectoriel : 43,12 %** — reste au-dessus du seuil de réussite de l'étape 1 (> 40 %), mais nettement sous les 65,93 % mesurés le 26/09 matin (avant la panne), pour la raison ci-dessus : le canal `citation_verifiee`, qui apportait la majorité de la diversité sectorielle mesurée ce matin-là (253/631, 40,1 % des dossiers), est aujourd'hui totalement à l'arrêt.

---

## 7. Réseau

| Hôte / catégorie | Appels | 429 | 403 | Timeouts/autres erreurs | Taux de succès |
|---|---|---|---|---|---|
| `hn.algolia.com` (recherche Enquêteur) | 3 221 | 0 | 0 | 2 | 99,94 % |
| `www.reddit.com` (recherche Enquêteur, sitewide) | 993 | 485 | 0 | 0 | 51,2 % |
| `www.reddit.com` (RSS + recherche sub×expression du Scout, tout confondu) | ≈ 800 | ≈ 210 | 0 | 0 | ≈ 74 % |
| `news.ycombinator.com`/`hnrss.org` (RSS, offre) | ≈ 270 | 0 | 0 | 2 | ≈ 99 % |
| Fetch direct de pages (magasin interne, concurrents) | 834 | 0 | 0 | 0 | 100 % |

**Temps moyen d'attente Reddit (tous chemins de collecte Reddit confondus, 1 795 intervalles mesurés)** : médiane **42,5 s**, minimum 4,0 s, maximum ≈ 8h36 (30 949 s, pendant une pause du disjoncteur ou une période sans activité). **71 intervalles (4,0 %) sont sous les 6 s** et 85 (4,7 %) sous les 12 s — en net progrès par rapport aux 5,3 % sous 6 s mesurés le 26/09 matin, cohérent avec l'espacement à 12 s posé côté Scout en sous-étape 3.10. Le taux de 429 sur Reddit (51,2 % pour le seul fournisseur de l'Enquêteur, ci-dessus) reste élevé mais nettement moins catastrophique que le 63,1 % du 26/09 matin.

---

## 8. Coût

**Total réel de la fenêtre (depuis 12:52 UTC le 26/09) : 0,069 070 766 €** — à comparer aux ≈ 17-19 €/jour habituels avant la panne. Par rôle :

| Rôle | Coût | Appels |
|---|---|---|
| Analyst | 0,035050 € | 880 |
| Critic | 0,034021 € | 880 |
| Scout | 0,000000 € | 496 |
| Enquêteur (recherche + fetch) | 0,000000 € | 3 443 |

**Dossier enrichi vs non enrichi** : sur les 879 dossiers ayant eu un coût non nul associé dans la fenêtre (au sens large — au moins une ligne `usage_events` liée), coût moyen 0,000 079 € — un ordre de grandeur qui ne veut plus rien dire une fois qu'on sait que 878/880 lignes Analyst/Critic sont à 0,00 € pile. La comparaison enrichi/non-enrichi qui avait du sens le 26/09 matin (0,0376 € vs 0,0385 €, quasi identique) n'est plus interprétable ici — tout est proche de zéro pour la même raison (aucun appel ne va jusqu'au bout).

**Projection à 30 jours — deux scénarios, très différents** :
- **Si la panne du §3 n'est pas corrigée** : au rythme actuel (≈ 0,069 € / 26 h ≈ 0,064 €/jour), la facture resterait proche de **2 €/mois** — mais pour zéro dossier réellement analysé, uniquement des replis vides.
- **Si l'appel modèle est rétabli au rythme d'avant la panne** (≈ 18,8 € accumulés en 12h52 le 26/09, soit ≈ 1,46 €/h, plafonné par le budget dur de 25 €/jour) : la facture reviendrait à l'ordre de **600 à 750 €/mois** (25 €/jour × 30, cohérent avec le plafond dur déjà en place), avec cette fois de vrais dossiers analysés.

---

## 9. Conclusion

1. **Non, le plafond de 50 n'est pas tombé** — il s'est effondré à 0. Le facteur limitant n'est plus « une seule source par dossier » (Constat 1 du diagnostic initial, largement amélioré depuis : 80,36 % à une seule source contre 98,26 % le 26/09 matin) : c'est qu'**aucun modèle n'est plus jamais appelé avec succès depuis 25 h**, donc plus aucun jugement, quelle que soit la qualité des preuves disponibles.
2. **Panne totale, vérifiée chiffres à l'appui, pas une hypothèse** : depuis 2026-09-26 ~13:41 UTC (49 minutes après le déploiement réel de 3.10), 99,8 % des appels Analyst et Critic et 100 % des appels Scout échouent au niveau de l'appel API lui-même (`client.messages.create()` lève une exception) — jamais un rejet de schéma comme la panne corrigée en 3.10.
3. **Le pipeline n'a jamais bloqué un seul dossier** grâce aux replis déterministes déjà prévus pour ce cas (`_scout_heuristique`, `_critic_heuristique`) — mais 494 dossiers créés depuis, et 878 scores, sont donc du vide honnête (score 0,0, `niveau_preuve="inconnu"` partout), pas une analyse.
4. **Effet en cascade sur l'étape 2** : 0 % de secteur par citation vérifiée dans la fenêtre (contre 40,1 % le 26/09 matin) — cette étape dépend elle aussi d'un appel Scout réussi.
5. **Effet en cascade sur le budget** : le coût réel enregistré étant 0 €, le plafond de 25 €/jour ne peut plus jamais se remplir — c'est le second garde-fou indépendant posé en 0.7 (1 300 appels approfondis/jour) qui arrête seul le run chaque jour, un rôle qu'il n'était pas conçu pour jouer seul.
6. **La sous-étape 3.11 n'a jamais été déployée** (vérifié : ni commit de synchronisation, ni colonnes en base) — le fournisseur Reddit de l'Enquêteur tourne donc toujours actif par défaut, et les requêtes restent composées par repli, jamais par les mots-clés structurés du Scout.
7. **Cause de la panne non confirmée** (aucun accès aux logs Render ni à la console Anthropic depuis cette session) : hypothèse la plus vraisemblable — dérive de version, `requirements.txt` ne fixant qu'un plancher (`anthropic>=0.40`) pour le SDK, chaque déploiement pouvant installer une version différente ; la coïncidence temporelle avec le déploiement de 3.10 est très forte, mais le champ `strict` ajouté par 3.10 est bien documenté dans le SDK 1.8.0 installé en local, donc ce n'est pas certain.
8. **Correction la plus rentable avant l'étape 4, proposée sans l'implémenter, par ordre de priorité** :
   a. **Urgence 1, avant tout le reste** : Mathéo consulte les logs Render du Background Worker autour de 2026-09-26 13:41 UTC (message `Appel modèle échoué (...)`, `app/adapters/model_client.py:237`) pour obtenir le message d'exception exact et rétablir les vrais appels au modèle.
   b. Une fois rétabli : figer les versions dans `requirements.txt` (au moins `anthropic==<version testée>`) pour qu'un déploiement ne puisse plus jamais changer silencieusement une dépendance critique.
   c. Ajouter une alerte explicite (log de niveau erreur distinct, ou compteur dédié dans `app.metriques`) quand le taux d'échec `issue=perdue` dépasse un seuil sur une fenêtre courte — cette panne a duré 25 h sans qu'aucun signal ne remonte ailleurs que dans une lecture manuelle de la base.
9. **Secondaire, à vérifier séparément** : le plafond de requêtes de recherche de l'Enquêteur (3 000/jour) semble légèrement dépassé les deux jours de la fenêtre (+4 % puis +17 %, mesuré via `journal_http`, pas via le compteur interne exact) — à confirmer avant de considérer que le garde-fou §3.4 (« jamais dépassé ») est vraiment respecté à l'appel près.
10. **Aucune correction n'est appliquée ici** — lecture seule, conforme au périmètre de cette sous-étape. La décision et la priorisation reviennent à Mathéo/Fable (voir §9 d'`AMELIORATIONS.md`).
