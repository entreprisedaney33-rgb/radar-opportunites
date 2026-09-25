# Diagnostic du dépassement de budget — 25/09/2026

Sous-étape 0.6 d'`AMELIORATIONS.md`. Lecture seule (rôle `radar_lecture`), aucun appel modèle, aucune modification de code, aucun déploiement. Requêtes exécutées entre 18:47 et 18:52 (heure de Paris), soit 16:47–16:52 UTC.

---

## 1. `app.metriques` et le module budget regardent-ils la même chose ?

**`app.metriques`** (`app/metriques.py::calculer_metriques`) calcule `cout_jour_eur` en additionnant la colonne **`usage_events.cout_declare_ou_estime`** pour toutes les lignes dont **`usage_events.date_creation`** tombe dans la journée UTC demandée (`>= début du jour UTC` et `< début du jour suivant`). Clé de regroupement : **le calendrier (jour civil UTC)**.

**Le module budget** (`app/pipeline/budget.py::BudgetTracker`) calcule la dépense déjà engagée avec `repo.cout_total_run(engine, run_id)`, qui additionne la **même colonne `usage_events.cout_declare_ou_estime`**, mais pour toutes les lignes dont **`usage_events.run_id`** correspond au run en cours. Clé de regroupement : **le run**, pas le calendrier.

**Réponse : non, ils ne regardent pas la même chose.** Même table, même champ de coût — mais deux clés d'agrégation différentes. Le plafond de 25 €/jour (§3.4 du cahier des charges) est appliqué **par run**, pas par jour UTC. Les deux ne coïncident que si un seul run existe par journée UTC. Le §3 ci-dessous montre que ce n'était pas le cas aujourd'hui.

---

## 2. Combien de runs aujourd'hui, et le redémarrage du worker repart-il à zéro ?

**5 runs** ont été créés le 25/09/2026 (UTC), tous en mode `reel` :

| Run (id abrégé) | Début UTC | Fin UTC | Statut | Coût déclaré (`usage_events`) | Appels modèle |
|---|---|---|---|---|---|
| `3ac8476e…` | 00:18:53 | 00:22:56 | terminé | 0,349 € | 20 |
| `dfd502ae…` | 01:01:00 | 01:06:57 | terminé | 0,549 € | 26 |
| `6b2de87b…` | 01:25:00 | 01:45:54 | terminé | 1,881 € | 107 |
| `1f2622d5…` | 11:06:47 | 15:36:38 | terminé | **24,991 €** | 1139 |
| `36718268…` | 16:02:31 | *(en cours)* | en_cours | 4,401 € *(et ça continue)* | 205+ |
| **Total du jour** | | | | **32,17 €** | **1497+** |

Le run `1f2622d5…` s'est arrêté **exactement** parce qu'il a heurté le plafond : son `erreurs_json` contient littéralement `"Plafond de 25.00 € dépassé : 24.99 € déjà engagés + 0.03 € estimés pour cet appel."` et son `resume_json` porte `"budget_atteint": true`. Jusque-là, le mécanisme fonctionne comme prévu.

**Le problème est dans ce qui se passe après.** Le code de `executer_continu` (`app/pipeline/orchestrator.py`, lignes ~433-445) est censé, quand `budget_atteint` est vrai, clore le run puis **attendre le changement de jour UTC** avant d'en ouvrir un nouveau :
```python
while datetime.now(timezone.utc).date() == aujourdhui:
    ...
    time.sleep(300)
```
Cette boucle ne peut sortir sans créer un nouveau run que si la date change — elle ne le fait jamais « juste parce que ça fait longtemps ». Or le run suivant (`36718268…`) a démarré à **16:02:31 UTC, le même jour**, seulement 26 minutes après la fin du run précédent (15:36:38). Ce délai est incompatible avec la boucle d'attente ci-dessus si le **process** avait continué de tourner sans interruption.

La seule explication cohérente avec le code : **le process du worker lui-même a redémarré** entre 15:36 et 16:02 (redéploiement, recyclage de l'instance Render, ou plantage — impossible à distinguer avec les seuls accès en lecture seule à la base ; le journal de déploiement/redémarrage de Render n'a pas été consulté, hors périmètre de cette sous-étape). Au redémarrage, `executer_continu` repart du début : `repo.run_en_cours_le_plus_recent()` (`app/storage/repo.py`, ligne 78) ne cherche que les runs au statut **`en_cours`** — or le run précédent venait d'être marqué `termine`. Aucun run « en cours » n'est trouvé pour aujourd'hui, donc un **nouveau run est créé** (`repo.creer_run`), et `BudgetTracker.__init__` recalcule `_depense_engagee = cout_total_run(engine, NOUVEAU_run_id)`, qui vaut **0 €** puisque ce run_id n'a encore aucune ligne dans `usage_events`.

**Oui : un redémarrage du worker après qu'un run a été clos le même jour UTC fait repartir le compteur de budget à zéro**, même si la dépense réelle de la journée est déjà au plafond.

Les trois premiers runs de la nuit (00h18, 01h01, 01h25 — coûts 0,35 €, 0,55 €, 1,88 €, bien en dessous du plafond) montrent que ce n'est pas un cas isolé lié au dépassement de budget : le même schéma (un run se termine, un autre redémarre quelques minutes après, même jour UTC) se produit aussi **sans rapport avec le budget**, probablement à cause des redéploiements successifs de cette soirée-là. Le mécanisme de reprise (repartir du retard, jamais perdre un dossier) fonctionne bien pour les *opportunités* ; il ne protège pas le *compteur de budget*.

---

## 3. Répartition du coût par rôle et par heure

`usage_events` n'a pas de colonne « rôle » — seulement `modele_ou_actor` (le nom du modèle). Mais la config (`app/config.py`) fixe un modèle par rôle : `RADAR_MODEL_TRI` (défaut `claude-haiku-4-5-20251001`) pour le **Scout**, `RADAR_MODEL_APPROFONDI` (défaut `claude-sonnet-5`) pour **Analyst et Critic**. La correspondance est vérifiable numériquement : 444 appels `claude-haiku…` = exactement 444 lignes `assessments.role='scout'` ; 1037 appels `claude-sonnet-5` = exactement 519 `analyst` + 518 `critic` = 1037. Le Scout est donc isolable avec certitude ; **Analyst et Critic partagent le même modèle et ne peuvent pas être distingués dans `usage_events`** (pas de clé opportunité/rôle sur cette table) — point de traçabilité à noter pour Fable, pas seulement pour ce diagnostic.

| Heure UTC | Scout (haiku) | Analyst + Critic (sonnet-5) | Total |
|---|---|---|---|
| 00h | 0,036 € (10 appels) | 0,313 € (10) | 0,349 € |
| 01h | 0,203 € (55) | 2,226 € (78) | 2,429 € |
| 11h | 0,329 € (93) | 4,687 € (160) | 5,016 € |
| 12h | 0,230 € (66) | 5,362 € (186) | 5,591 € |
| 13h | 0,248 € (69) | 5,209 € (180) | 5,457 € |
| 14h | 0,225 € (63) | 5,389 € (185) | 5,614 € |
| 15h | 0,119 € (33) | 3,195 € (104) | 3,313 € |
| 16h (partiel) | 0,193 € (55) | 4,062 € (138) | 4,255 € |

Le Scout (haiku, modèle bon marché) coûte peu (≈ 1,58 € sur la journée, moins de 5 % du total). L'essentiel de la dépense — **plus de 95 %** — vient d'Analyst + Critic (sonnet-5), ce qui est cohérent avec le cahier des charges (le tri est fait au modèle le moins cher, l'analyse approfondie au modèle le plus cher).

---

## 4. Opportunités traitées plus de deux fois par un même rôle

**10 opportunités** ont eu Analyst et/ou Critic appelés plus de deux fois aujourd'hui (le Scout, lui, n'est jamais rappelé : 1 appel partout). Analyst et Critic sont systématiquement à égalité (un Analyst sans Critic assorti n'existe pas dans ces cas) — ce sont bien des **cycles complets répétés**, pas des passages interrompus.

| Titre (tronqué) | Analyst / Critic | Statut final |
|---|---|---|
| Solutions de continuité de service et disaster recovery… | 5 / 5 | rejeté |
| Plateforme d'agrégation d'expériences entrepreneuriales… | 5 / 5 | rejeté |
| Plateforme SaaS de disaster recovery… | 4 / 4 | rejeté |
| Analyse/automatisation d'interfaces héritées… | 3 / 3 | incertain |
| Accompagnement psychologique entrepreneurs post-faillite… | 3 / 3 | incertain |
| Orchestration de workflows bioinformatiques… | 3 / 3 | incertain |
| Interface utilisateur adaptative (paradigmes legacy)… | 3 / 3 | incertain |
| Jev-browse — navigation pour agents de code… | 3 / 3 | incertain |
| Diagnostic IA douleurs lombaires/cervicales… | 3 / 3 | incertain |
| NOAN — couche de faits pour agents IA… | 3 / 3 | incertain |

**Pourquoi.** En rejouant l'historique des décisions (`scores.decision_critic`, ordonné par date) pour les trois cas les plus répétés, le mécanisme responsable est identifiable avec certitude : c'est **l'échantillon de contrôle des rejetés**, prévu et codé exprès (`app/pipeline/orchestrator.py::_selectionner_pour_analyse`, `config/quotas.yaml::echantillon_rejetes_pour_controle: 0.10`). À chaque passage, 10 % du lot d'analyses est tiré au hasard parmi les opportunités déjà au statut `rejete` — sans plafond, sans mémoire de « déjà retesté aujourd'hui ». Un dossier rejeté reste éligible au tirage indéfiniment tant qu'il reste `rejete`. Exemple vérifié (opportunité « disaster recovery », id `5e4c0adc…`) : rejetée à 01:44, retirée puis re-rejetée à 11:37, 11:47, 12:06 et 13:45 — cinq cycles complets Analyst+Critic sur le **même** dossier, à travers **deux runs différents** (donc le redémarrage du worker ne l'a pas non plus arrêté). Sur les 10 cas, 3 finissent `rejete`, 7 finissent `incertain` (le tirage répété a fini par produire un avis différent).

Ce n'est **pas** un bug de reprise « en priorité qui reboucle » ni un statut jamais mis à jour (le code met bien à jour `opportunities.statut` à chaque cycle, vérifié) : c'est un tirage de contrôle qui fonctionne comme écrit, juste sans limite. Coût : environ 50 appels sonnet-5 « en trop » sur la journée, soit **≈ 1,5 €** — réel, mais une part mineure du dépassement (le run à lui seul a représenté 25 €).

---

## 5. Le coût enregistré est-il réel ou estimé ?

**C'est une estimation, jamais un montant facturé.** `app/adapters/model_client.py::_estimer_cout_eur` calcule le coût à partir des **vrais** nombres de tokens renvoyés par l'API Anthropic (`resp.usage.input_tokens` / `output_tokens` quand l'appel réussit), multipliés par un tarif **codé en dur et marqué « indicatif — à vérifier avant tout run réel »** (`PRICES_USD_PAR_MILLION_TOKENS`, jamais vérifié à ce jour d'après le commentaire du fichier), puis convertis en euros avec un taux **approximatif** (`USD_VERS_EUR = 0.92`, lui aussi signalé comme à vérifier). Donc : vrais tokens × tarif non confirmé × change approximatif. Si l'appel échoue (réseau, erreur), le coût enregistré est 0 € (aucune fabrication de dépense sur un appel qui n'a pas eu lieu). Le budget suit une estimation cohérente et cumulable, pas une facture réelle — la vraie facture Anthropic n'a pas été consultée dans ce diagnostic (hors périmètre, aucun accès à la console Anthropic).

---

## 6. Conclusion

Le plafond de 25 €/jour a bien été dépassé dans les faits : **32,17 € dépensés aujourd'hui** (chiffre encore en hausse au moment d'écrire ces lignes, le run en cours n'étant pas terminé) contre 25 € autorisés — un dépassement de 7,17 €, soit +29 %. Le mécanisme n'est pas une faille du calcul du coût (§5, l'estimation est cohérente et basée sur de vrais tokens) ni principalement le rééchantillonnage des rejetés (§4, ≈1,5 € seulement) : c'est un **défaut de conception**, le plafond est appliqué par run (`BudgetTracker`, clé `run_id`) alors que la règle voulue est journalière (§3.4 du cahier des charges, clé calendrier) — les deux ne coïncident que s'il existe un seul run par jour UTC, ce qui n'a pas été le cas aujourd'hui (5 runs, dont un redémarrage 26 minutes après que le plafond a été atteint). La correction proposée, **sans l'implémenter ici** : faire porter le plafond sur la dépense réelle de la journée UTC en cours (somme sur tous les runs du jour, pas seulement le run courant) plutôt que sur le seul run actif — et, séparément, plafonner le nombre de fois qu'un dossier déjà `rejete` peut être retiré au tirage de contrôle par jour.
