# Audit de la première nuit — 26/09/2026

Sous-étape 3.8 d'`AMELIORATIONS.md`. Lecture seule (rôle `radar_lecture`), aucun appel modèle, aucune modification de code, aucun déploiement. Statut : **FAIT** (accès base rétabli).

---

## 0. Accès rétabli

La panne de connexion qui bloquait cette sous-étape (et déjà 0.5, 0.7, 3.6) est résolue : Mathéo a mis à jour l'adresse IP autorisée côté Render. `python -m app.metriques --jour 2026-09-26` fonctionne à nouveau depuis cette session, ainsi qu'une connexion directe `sqlalchemy` via `radar_lecture`. **Note de méthode** : l'hypothèse de filtrage réseau applicatif avancée dans la version précédente de ce rapport (26/09, matin) est donc fausse — la vraie cause était une liste d'IP autorisées côté Render, pas une limite structurelle de l'environnement d'exécution. Corrigé en §9.

Deux façons ont été utilisées pour répondre aux 8 points, toutes en lecture seule via `radar_lecture` :
- `python -m app.metriques --jour 2026-09-26` (couvre les points 1, 2, 5, 6, et une partie du 3 et 7) ;
- un script d'audit ad hoc, écrit et exécuté depuis cette session (hors dépôt, jamais commité — lecture seule, `SELECT` uniquement, aucune écriture), pour le détail par dossier que `app.metriques` ne calcule pas (chronologie fine, top 10, citations, coût par dossier enrichi/non enrichi, limiteur Reddit).

**Instant de la mesure : 2026-09-26, 11:53:38 UTC.** Le run du jour est toujours `en_cours` à cet instant (normal, la journée n'est pas terminée) — ce rapport est donc une photo à mi-journée, pas un bilan de nuit complète. Les chiffres ci-dessous datent tous de cet instant, sauf mention contraire.

---

## 1. Chronologie (runs, redémarrages, budget dans le temps)

**Un seul run aujourd'hui**, `8e908bc5…`, débuté à `2026-09-26 00:00:04 UTC`, statut `en_cours` (`fin` = néant) au moment de la mesure — **aucun redémarrage détecté** depuis minuit UTC (contrairement à plusieurs jours précédents où le worker avait redémarré plusieurs fois).

Coût cumulé du run (tous rôles, colonne `couts_json.total_eur_estime`, recoupée avec la somme heure par heure de `usage_events`, les deux concordent au centime près) :

| Heure (UTC) | Coût (€) | Appels |
|---|---|---|
| 00h | 0,8843 | 353 |
| 01h | 0,7165 | 379 |
| 02h | 1,1666 | 334 |
| 03h | 1,0462 | 311 |
| 04h | 0,6300 | 359 |
| 05h | 1,0531 | 331 |
| 06h | 0,9498 | 315 |
| 07h | 1,9858 | 258 |
| 08h | 2,3471 | 208 |
| 09h | 2,3810 | 168 |
| 10h | 2,1283 | 158 |
| 11h (jusqu'à 11:53) | 2,1661 | 143 |
| **Cumul à 11:53 UTC** | **17,4548 €** | **3 317** |

**Budget du jour : 17,45 € / 25 € engagés (69,8 %) à 11:53 UTC — pas encore atteint.** Le nombre d'appels par heure diminue franchement à partir de 8h (208 puis en baisse) alors que le coût par heure augmente : cohérent avec un mélange d'appels de plus en plus dominé par Analyst/Critic (plus chers) à mesure que la file d'attente de dossiers `nouveau` s'épuise moins vite que les nouveaux signaux n'arrivent (voir point 2).

Le `resume_json` du run ne reflète que le **dernier passage**, pas le cumul de la journée (il est réécrit à chaque passage, voir `app/pipeline/orchestrator.py::executer_continu`) : au moment de la mesure, dernier passage = 4 signaux lus, 4 nouvelles opportunités, 15 enquêtées, 15 analysées, 15 critiquées, budget non atteint.

---

## 2. Entonnoir réel

- **631 opportunités repérées** aujourd'hui, toutes issues de flux `douleur` (`par_type_flux: {"douleur": 632}` — cohérent par construction : un flux `offre` ne crée jamais de signal, voir sous-étape 1.1). 346 items `offre` stockés séparément en `signal_concurrence` (jamais transformés en dossier), signe que le tri douleur/offre tient toujours.
- **Statuts au moment de la mesure** : `nouveau` 246 (39,0 %), `enquete_terminee` 54 (8,6 %, en attente d'Analyst), `en_analyse` 1, `incertain` 279 (44,2 %, décision Critic `a_verifier`), `rejete` 51 (8,1 %). 279 + 51 = **330 dossiers analysés** (52,3 % des 631 repérés) ; les 301 restants (47,7 %) sont en file d'attente.
- **Où la file s'accumule** : le rythme d'arrivée (≈ 631 dossiers en 11h53, soit ≈ 53/h) dépasse le débit d'Analyst/Critic (plafonné à 15 par passage, `max_analyses_par_passage`). La file d'attente grossit mécaniquement au fil de la journée — ce n'est pas une panne, c'est le comportement attendu d'un pipeline plat face à un volume de signaux plus élevé depuis l'étape 1 (325 combinaisons de recherche actives).
- **Décisions Critic** : `a_verifier` 279, `rejeter` 51, **`eligible_revue_humaine` : 0** — le Constat 1 du diagnostic initial (§1 du plan) reste vrai malgré le déploiement de l'Enquêteur : aucun dossier n'a encore dépassé le plafond de preuve nécessaire pour être déclaré éligible.

---

## 3. L'Enquêteur, dossier par dossier

**Sources par dossier (dossiers créés aujourd'hui)** : min 1, médiane 1, max 12, **98,26 % des dossiers du jour n'ont qu'une seule source** — quasiment inchangé par rapport à l'avant-Enquêteur (100 % avant l'étape 3). Sur les 631 dossiers du jour, seuls 91 preuves d'enquête ont été rattachées, réparties par fournisseur : `magasin_interne` 55, `algolia_hn` 36, **`reddit` : 0**.

**Cause du zéro Reddit, vérifiée (pas une hypothèse)** : `reddit.com/robots.txt` interdit tout crawl (`User-agent: * / Disallow: /`, vérifié directement pendant cet audit) et le code de l'Enquêteur respecte scrupuleusement `robots.txt` avant de stocker une page (`app/enqueteur/fetch.py::_robots_autorise`, ligne 145-146 : « robots.txt interdit … page ignorée »). Résultat mesuré : le fournisseur de recherche Reddit de l'Enquêteur a fait 750 tentatives aujourd'hui, dont 473 bloquées en 429 (63,1 %) et 277 « réussies » au niveau HTTP — mais ces 277 succès n'ont produit **aucune** page stockée, puisque `reddit.com` interdit structurellement leur récupération. Ce fournisseur est donc, par construction, incapable de produire la moindre preuve.

**Quotas de l'Enquêteur au moment de la mesure** :
- `requetes_recherche_jour` : **1500 / 1500 (100 %, plafond atteint avant midi UTC)** — voir point 8.
- `fetchs_pages_jour` : 405 / 1000 (40,5 %, marge disponible).

**Concurrents et prix** : 19 sources étiquetées `prix` créées aujourd'hui, mais le fetch direct `<domaine>/pricing` (`enqueteur_fetch:fetch_direct_pricing`) n'a été tenté que 3 fois, et a échoué les 3 fois (`autres_erreurs`, pas des 429/403 — domaines de concurrents injoignables ou route `/pricing` inexistante). Les 19 pages `prix` proviennent donc essentiellement de résultats de recherche (famille `prix` via Algolia HN), pas du fetch direct.

**Répartition détaillée des appels HTTP de l'Enquêteur aujourd'hui** :

| Fournisseur / étape | Appels | 429 | Autres erreurs | Succès | Taux de succès |
|---|---|---|---|---|---|
| `enqueteur_recherche:algolia_hn:comment` | 750 | 0 | 0 | 750 | 100 % |
| `enqueteur_recherche:algolia_hn:ask_hn` | 728 | 0 | 0 | 728 | 100 % |
| `enqueteur_recherche:reddit` | 750 | 473 | 0 | 277 | 37,0 % (mais 0 page utilisable, voir ci-dessus) |
| `enqueteur_fetch:magasin_interne` | 67 | 0 | 0 | 67 | 100 % |
| `enqueteur_fetch:algolia_hn` | 27 | 0 | 0 | 27 | 100 % |
| `enqueteur_fetch:fetch_direct_pricing` | 3 | 0 | 3 | 0 | 0 % |

---

## 4. L'Analyst

Répartition des affirmations (`opportunity_evidence`) créées aujourd'hui par type et par provenance de leur source :

| Type d'affirmation | Source = preuve d'enquête | Source = preuve prix | Source = signal d'origine |
|---|---|---|---|
| `observe` | 12 | 3 | 825 |
| `non_verifie` | 49 | 27 | 100 |
| `hypothese` | — | — | 887 |
| `calcule` | — | — | 4 |

Sur un total d'environ 1 907 affirmations créées aujourd'hui, seules **91 (4,8 %) citent une source apportée par l'Enquêteur** (12+49+3+27), le reste (95,2 %) reposant sur le signal d'origine ou une hypothèse — cohérent avec les 91 preuves d'enquête stockées au point 3. L'Enquêteur, quand il produit quelque chose, semble bien exploité par l'Analyst (pas de preuve ignorée en masse) ; le vrai goulot reste l'alimentation (point 3), pas l'exploitation.

**Top 10 des dossiers du jour par score prudent** :

| Titre | Score prudent | Score brut | Décision | Secteur | Sources |
|---|---|---|---|---|---|
| Whiteboard (YC W26) – IDE open-source | 52,5 | 75,0 | a_verifier | intersectoriel | 1 |
| Acquéreur MSP (consolidation PME) | 52,5 | 61,8 | a_verifier | operations_petites_entreprises | 1 |
| Outil de suivi de temps gratuit (7 utilisateurs) | 47,5 | 63,3 | a_verifier | intersectoriel | 1 |
| Plateforme de rituels créatifs collectifs | 47,5 | 67,9 | a_verifier | intersectoriel | 1 |
| Claude Desktop / Anthropic | 45,0 | 64,3 | a_verifier | flux_documentaires | **7** |
| Aide à la décision d'orientation professionnelle | 45,0 | 50,0 | a_verifier | intersectoriel | 1 |
| DBDelve | 45,0 | 75,0 | a_verifier | outils_internes_it | 1 |
| Gestion de bugs décentralisée hors-ligne | 45,0 | 81,8 | rejeter | intersectoriel | 1 |
| Conformité / extraction d'emails procédures légales | 45,0 | 64,3 | rejeter | flux_documentaires | 1 |
| Aide au rattrapage fiscal petits entrepreneurs | 45,0 | 64,3 | a_verifier | flux_documentaires | 1 |

**Observation clé** : le seul dossier du top 10 avec plusieurs sources (« Claude Desktop / Anthropic », 7 sources) **ne dépasse pas** les dossiers à une seule source — il plafonne même en dessous (45,0 vs 52,5). En regardant son détail par critère : `acheteur_disposition_payer` et `economie_cout_lancement` y restent tous les deux « inconnu » malgré les 7 sources. Cela suggère que le nombre de sources seul ne suffit pas : l'Analyst ne semble pas en tirer de preuve pour ces deux critères précis, même quand la matière existe.

**Score avant/après enrichissement** : non mesurable avec les données actuelles — `scores` est append-only mais aucun des 10 dossiers examinés n'a plus d'une ligne de score aujourd'hui (pas de recalcul observé après une enquête tardive dans cet échantillon). Question laissée ouverte en §9.

---

## 5. Secteur

`secteur_provenance` des 631 dossiers du jour : `defaut` 321 (50,9 %), `citation_verifiee` 253 (40,1 %), `flux` 57 (9,0 %). **Part hors « intersectoriel » : 65,93 %** — largement au-dessus du seuil de réussite fixé pour l'étape 1 (> 40 %).

10 citations vérifiées récentes (secteur déduit d'une citation, pas du flux ni du défaut) :

1. « just get a message saying that due to high call volumes they can't connect me to an agent, and then the call hangs up » → services_professionnels
2. « business returns, bookkeeping/accounting, tax planning/advisory, IRS notices/representation » → operations_petites_entreprises
3. « She tried to talk to tax act and they said she cannot file another amended tax return on their site and needs to mail in a paper tax return amendment » → services_professionnels
4. « They filed an extension for us, however, they still have not filed our taxes. We expected a delay but did not anticipate that it would take 3 months. » → operations_petites_entreprises
5. « I need to create every product with AI to have pictures, and then build them in real world if someone orders it » → e_commerce
6. « companies that use these methods increase their conversion rates and see incredible results » → operations_petites_entreprises
7. « Being simple is what's winning now. » → nouveaux_modeles
8. « Over the past month, there was a big release with the official Meta Ads MCP. […] I've been able to build out an agent that can essentially […] » → agents_ia
9. « 65% of AI users have at least partially replaced Google searches with AI chatbots for product research, according to an SEMRush survey of 2,338 US consumers. » → e_commerce
10. « I just have multiple agents work on a markdown file which I manually perfect, often breaking into multiple different files for large features or PRs » → outils_internes_it

---

## 6. Coût

Total du jour à 11:53 UTC : **17,4548 €** (69,8 % du plafond de 25 €). Par rôle : `scout` 3,0146 €, `analyst` 7,7355 €, `critic` 6,5381 €, `enqueteur_recherche` 0 €, `enqueteur_fetch` 0 € (fournisseurs gratuits — l'Enquêteur ne pèse jamais sur le budget en euros, seulement sur ses deux compteurs dédiés). Coût moyen par dossier analysé : 0,0524 €. Coût moyen par opportunité (repérée) : 0,0384 €.

**Dossier enrichi vs non enrichi** (sur les 375 dossiers ayant eu un coût aujourd'hui) : 11 dossiers avec au moins une preuve d'enquête/prix, coût moyen 0,0376 € ; 364 sans, coût moyen 0,0385 €. **Quasiment identique** — enrichir un dossier ne coûte pratiquement rien de plus en tokens (cohérent avec le point précédent : l'Enquêteur est gratuit). Le vrai coût de l'enrichissement n'est donc pas en euros, mais dans les deux quotas dédiés — et celui des requêtes de recherche est déjà épuisé (point 3, point 8).

**Consommation des plafonds** : requêtes de recherche 1500/1500 (100 %, épuisé avant midi UTC) ; fetchs de page 405/1000 (40,5 %) ; budget en euros 69,8 % à la même heure. Les trois plafonds ne s'épuisent pas au même rythme — le premier à toucher le mur est celui des requêtes de recherche, bien avant les deux autres.

---

## 7. Réseau depuis le déploiement de 3.7

`journal_http` couvre bien toute la période depuis minuit UTC aujourd'hui (première ligne 00:00:05 UTC, dernière 11:44:04 UTC au moment de la mesure) — contrairement au risque signalé dans la version précédente de ce rapport, la fenêtre n'est plus limitée à quelques heures.

| Hôte / catégorie | Appels | 429 | 403 | Autres erreurs | Taux de succès |
|---|---|---|---|---|---|
| Reddit (RSS passif + recherche collecte + recherche Enquêteur, tout confondu) | 1 256 | 799 | 0 | 0 | 36,4 % |
| Hacker News (Algolia recherche + RSS + fetch Enquêteur) | ≈ 2 431 | 0 | 0 | 0 | 100 % |
| Product Hunt / TechCrunch (RSS) | 52 | 0 | 0 | 0 | 100 % |
| Fetch direct pricing (domaines de concurrents) | 3 | 0 | 0 | 3 | 0 % |

**Le limiteur de 6 secondes sur Reddit ne tient pas de façon fiable.** Sur 1 255 intervalles entre deux appels consécutifs vers `reddit.com` aujourd'hui : délai médian 21,5 s (correct), mais **66 intervalles (5,3 %) sont sous les 6 s**, avec un minimum observé de 0,20 s. Cohérent avec le taux de 429 très élevé (63,6 % toutes voies Reddit confondues) : plusieurs chemins de code appellent Reddit indépendamment (collecte RSS passive, recherche du planificateur, recherche de l'Enquêteur), chacun avec vraisemblablement son propre suivi de délai plutôt qu'un verrou global partagé — le débit réel imposé à Reddit dépasse régulièrement ce que Reddit tolère, quel que soit le réglage nominal de 6 s.

---

## 8. Conclusion

1. **L'Enquêteur n'a pas réellement multiplié les sources** depuis son déploiement : 98,26 % des dossiers du jour restent à une seule source (91 preuves d'enquête pour 631 dossiers), contre 100 % avant l'étape 3 — un progrès marginal, pas la multiplication attendue.
2. **Cause identifiée et vérifiée, pas supposée** : le fournisseur de recherche Reddit de l'Enquêteur est structurellement inutile — `reddit.com/robots.txt` interdit tout crawl (vérifié aujourd'hui) et le code respecte cette interdiction avant de stocker une page. Même les 277 recherches Reddit « réussies » sur 750 n'ont produit aucune preuve.
3. **Ce fournisseur inutile consomme pourtant la moitié du quota journalier de requêtes** (750/1 500) — quota de fait épuisé (100 %) avant midi UTC, coupant toute nouvelle enquête pour le reste de la journée, alors que le budget en euros (69,8 %) et le quota de fetchs (40,5 %) ont encore de la marge à la même heure.
4. Le fournisseur Hacker News (Algolia), lui, tourne parfaitement (100 % de succès, ≈ 2 200 appels) mais se retrouve rationné par ce même plafond partagé avec Reddit.
5. **Deuxième bottleneck, indépendant du premier** : même sur le seul dossier enrichi à 7 sources aujourd'hui, les critères `acheteur_disposition_payer` et `economie_cout_lancement` restent « inconnu » — l'Analyst ne semble pas en extraire de preuve, même quand la matière existe.
6. Le plafond de score (max 52,5 aujourd'hui, médiane 22,5) reste donc au même endroit que le diagnostic initial (§1 du plan) l'avait situé — l'alimentation en preuves, pas le Critic — mais pour une raison précise et actionnable désormais, pas seulement « une seule source par construction ».
7. **Correction la plus rentable, proposée sans l'implémenter** : retirer (ou désactiver) le fournisseur Reddit du *registre* de l'Enquêteur (`app/enqueteur/fournisseurs_gratuits.py`) — pas le connecteur de collecte Reddit en amont (RSS/recherche, lui utile et fonctionnel) — ce qui libérerait mécaniquement l'intégralité du quota de 1 500 requêtes/jour pour Algolia HN et le magasin interne, sans rien coûter de plus.
8. Piste secondaire, à vérifier séparément : revoir le prompt Analyst pour les critères `acheteur_disposition_payer`/`economie_cout_lancement` quand des preuves d'enquête sont disponibles.
9. Bonne nouvelle non liée à l'Enquêteur : le tri douleur/offre (étape 1) et le secteur par citation (étape 2) tiennent tous les deux largement leurs objectifs (65,93 % hors intersectoriel, 100 % des opportunités issues de flux douleur).
10. Aucune décision de correction n'est prise ici — lecture seule, conforme au périmètre de cette sous-étape.
