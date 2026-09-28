# Point d'étape après 3.13 — 28/09/2026

Sous-étape 3.14 d'`AMELIORATIONS.md`. Lecture seule (rôle `radar_lecture`), aucun appel modèle, aucune modification de code, aucun déploiement.

**Instant de la mesure : 2026-09-28, ~12:05 UTC.**

---

## 0. Périmètre réel — écart avec la demande

La demande porte sur la fenêtre « depuis le déploiement de 3.13 (27/09) jusqu'à maintenant ». **Cette fenêtre n'existe pas, pour la même raison qu'en sous-étape 3.12** : 3.13 (comme 3.11, qu'elle embarque) n'a jamais été synchronisée vers le dépôt public ni déployée sur Render.

Preuves (pas une hypothèse) :
- `git log` sur `produits/radar-opportunites` : le dernier commit est `be5e55f2 [3.13] ...`, sans commit « Journal de déploiement » derrière lui — contrairement à 3.7, 3.9, 3.10, qui en ont chacun un. Le Journal de 3.13 lui-même dit : *« PAS déployé, en attente de l'OK de Mathéo (radar et Jarvis séparément) »*.
- La base de production ne contient ni `opportunities.mots_cles_en`/`mots_cles_fr` (3.11), ni les tables `etats_disjoncteur_enqueteur` (3.11) ou `etats_disjoncteur_api` (3.13) — vérifié directement via `information_schema`. `python -m app.metriques` échoue toujours sur `opportunities.mots_cles_en does not exist`, exactement comme au 27/09.
- Le dernier déploiement réel reste donc **3.10** (commit public `ebafdc6`, 2026-09-26 ~12:52 UTC) — inchangé depuis le rapport précédent.

**Conséquence directe, plus grave que le simple écart de fenêtre : la panne totale des appels au modèle décrite dans `rapports/POINT_ETAPE_2026-09-27.md` n'a jamais été interrompue.** Le correctif écrit et prouvé par un appel payant réel en sous-étape 3.13 (`app.adapters.schema_strict`) existe dans le code local depuis le 27/09, mais tant qu'il n'est pas déployé, il ne protège rien en production. **Au moment de cette mesure, la panne dure depuis 2026-09-26 13:41:59 UTC, soit environ 46 h 23 min sans interruption** — deux jours calendaires complets (27/09) plus la matinée du 28/09.

**Ce rapport mesure donc, comme le 3.12, depuis le dernier déploiement réel (3.10) pour la vue d'ensemble, et depuis l'instant du rapport précédent (2026-09-27 14:41 UTC) pour le détail heure par heure — c'est la fenêtre qui répond le plus honnêtement à « qu'est-ce qui s'est passé depuis la dernière fois qu'on a regardé ».**

Deux autres prémisses de la demande ne correspondent pas non plus à l'état réel du code déployé, signalées ici pour ne pas fausser les sections concernées :
- **« Reddit désactivé »** (point 3 de la demande) : c'est la sous-étape 3.11 qui devait désactiver par défaut le fournisseur Reddit de l'Enquêteur — non déployée, donc Reddit **reste actif** en production (plafonné à 30 % du quota et en mode recherche-sans-crawl depuis la 3.9, qui elle est bien déployée). §3 mesure la vraie situation.
- **« Le Critic maintenant qu'il est entendu »** (point 5) : l'étape 5 (sortie structurée typée du Critic, décision reprise par le code, banc d'étalonnage) n'a **pas commencé** — les 6 sous-étapes 5.1 à 5.6 sont toutes « À FAIRE » dans le Journal global (§8 d'`AMELIORATIONS.md`). Les objections du Critic restent du texte libre, sans champ `type` ; il n'existe aucune décision « par le code » à comparer à celle du Critic. §5 donne ce qui est réellement mesurable.

---

## 1. Santé

### Appels par rôle, depuis le rapport précédent (2026-09-27 14:41 UTC → maintenant, ~21 h 24)

| Rôle | Appels | Échecs (`issue=perdue`) | Taux de sorties valides | Coût |
|---|---:|---:|---:|---:|
| Scout | 166 | 166 | **0,00 %** | 0,00 € |
| Analyst | 195 | 195 | **0,00 %** | 0,00 € |
| Critic | 195 | 195 | **0,00 %** | 0,00 € |
| Enquêteur (recherche) | 1 875 | 0 | 100,00 % | 0,00 € (gratuit) |
| Enquêteur (fetch) | 817 | 0 | 100,00 % | 0,00 € (gratuit) |

**Le seuil de réussite demandé (> 95 % de sorties valides par rôle) est manqué de 95 points pour les trois rôles qui appellent le modèle.** Aucune heure de la fenêtre ne fait exception (détail ci-dessous) : ce n'est pas une dégradation ponctuelle, c'est un arrêt complet et continu.

### Cumul depuis le vrai début de la panne (2026-09-26 13:41:59 UTC → maintenant, ~46 h 23)

| Rôle | Appels | Échecs | Taux de sorties valides | Coût |
|---|---:|---:|---:|---:|
| Scout | 656 | 656 | 0,00 % | 0,00 € |
| Analyst | 1 073 | 1 073 | 0,00 % | 0,00 € |
| Critic | 1 073 | 1 073 | 0,00 % | 0,00 € |
| Enquêteur (recherche) | 4 301 | 0 | 100,00 % | 0,00 € |
| Enquêteur (fetch) | 1 626 | 0 | 100,00 % | 0,00 € |

**Coût par heure sur la fenêtre : 0,00 € à chaque heure, sans exception** (48 tranches horaires vérifiées une à une). Le dernier appel Scout/Analyst/Critic avec des tokens réellement facturés date toujours de **2026-09-26 12:58:29 UTC** — inchangé depuis le rapport du 27/09, aucun appel n'a réussi depuis.

### Runs et heure d'arrêt sur budget

| Run | Début (UTC) | Fin (UTC) | Statut | Coût réel | Cause d'arrêt |
|---|---|---|---|---:|---|
| `541504ec…` (27/09) | 00:00:57 | 13:16:11 | `termine` | 0,00 € | Plafond de 1 300 appels approfondis/jour (1300/1300) |
| `3a347b75…` (28/09) | 00:01:12 | *en cours* | `en_cours` | 0,00 € | — pas encore arrêté : **390/1 300 appels approfondis engagés** au moment de la mesure (30 %), va très probablement de nouveau buter sur ce même plafond dans l'après-midi |

**Ce n'est toujours pas le budget en euros qui arrête le run** (impossible, il reste à 0,00 € tout le temps) — c'est le second garde-fou indépendant de la sous-étape 0.7 (1 300 appels/jour), qui n'a jamais été conçu pour jouer ce rôle et qui, pour la deuxième journée complète d'affilée, sert seul de filet.

### Disjoncteur API (sous-étape 3.13)

**Sans objet : le mécanisme n'existe pas en production.** La table `etats_disjoncteur_api` est absente de la base (confirmé §0). Le disjoncteur écrit et testé en 3.13 — qui aurait dû arrêter le pipeline au bout de 5 échecs consécutifs et afficher un état rouge explicite — n'a jamais tourné une seule fois pendant ces 46 h de panne, puisqu'il n'a jamais été déployé. Le pipeline continue donc de créer des dossiers vides en silence, exactement comme décrit le 27/09, sans aucune alerte automatique.

---

## 2. Reprise

**Le mécanisme de reprise de la sous-étape 3.13 (`app.reprise`, `_phase_reprise`) n'a jamais été exécuté en production, pour la même raison que le disjoncteur : il n'est pas déployé.** Il n'y a donc, à ce stade, aucune reprise automatique à mesurer — seulement l'ampleur du problème qu'elle est censée corriger.

- **Dossiers créés depuis le début de la panne (2026-09-26 13:41:59 UTC) : 653**, contre 494 au moment du rapport du 27/09 — **+159 dossiers vides supplémentaires** créés depuis, à un rythme d'environ 7 dossiers/heure qui ne s'est jamais interrompu.
- **Retraités avec succès (au moins un vrai appel Analyst ou Critic après coup) : 0 / 653 (0 %).** Vérifié directement sur `usage_events` : les 1 073 appels Analyst et les 1 073 appels Critic effectués depuis le début de la panne sont *tous* en échec (`issue=perdue`), sans une seule exception — aucun de ces 653 dossiers n'a donc pu être réévalué, même par hasard.
- **Restent à reprendre : 653 / 653 (100 %)**, tous au statut `incertain` — aucun dossier n'est bloqué à un autre statut, mais aucun n'a non plus été marqué `a_reprendre` (ce statut, posé par 3.13, n'existe pas davantage en base : `SELECT DISTINCT statut` ne renvoie que `incertain`/`rejete`/`en_analyse`).
- **Hypothèses Scout exploitables produites depuis la panne : 0 / 656 tentatives.** Chaque appel Scout de la fenêtre est un repli déterministe (`buyer: "à confirmer (mode sans modèle : aucune extraction automatique de l'acheteur)"`, `ai_mechanism: "à définir en analyse"`, `pain` = simple copie du texte brut du signal) — aucun n'a extrait un acheteur, un mécanisme IA ou un secteur. Deux exemples réels vérifiés dans le payload stocké (28/09) le confirment mot pour mot.

**Point à retenir : la « reprise » n'est pas un problème en attente de mesure — c'est un correctif prêt, testé, prouvé par un appel payant réel (0,0392 €), qui dort dans le code depuis 24 h de plus qu'au dernier rapport, pendant que le nombre de dossiers à reprendre continue de grossir.**

---

## 3. Enquêteur

L'Enquêteur ne dépend pas du modèle Anthropic (recherche et fetch HTTP uniquement) : il a continué de tourner normalement pendant toute la panne, et améliore même la richesse des dossiers pendant que personne ne les juge.

### Sources par dossier

| Fenêtre | n dossiers | min | médiane | max | Part à une seule source |
|---|---:|---:|---:|---:|---:|
| Depuis le rapport précédent (27/09 14:41 → maintenant) | 165 | 1 | **3** | 11 | **43,0 %** |
| Depuis le début de la panne (26/09 13:41:59 → maintenant) | 653 | 1 | 1 | 12 | 71,5 % |

La fenêtre récente est nettement meilleure que le cumul complet (43,0 % contre 71,5 % à une seule source) — signe que l'enrichissement s'améliore avec le temps sur cette période, probablement l'effet cumulé du plafonnement Reddit (3.9) et de l'espacement (3.10), tous deux réellement déployés.

### Requêtes par fournisseur et par famille (depuis le rapport précédent, `journal_http`)

| Fournisseur | Appels | 429 | Autres erreurs | Succès | Taux de succès |
|---|---:|---:|---:|---:|---:|
| `algolia_hn:comment` | 973 | 0 | 0 | 973 | 100,0 % |
| `algolia_hn:ask_hn` | 973 | 0 | 0 | 973 | 100,0 % |
| `reddit` (recherche) | 902 | 436 | 0 | 466 | 51,7 % |
| `magasin_interne` (fetch) | 729 | 0 | 14 (403) | 715 | 98,1 % |
| `fetch_direct_pricing` | 81 | 0 | 35 | 46 | 56,8 % |

**Requêtes évitées / disjoncteur Reddit de l'Enquêteur (sous-étape 3.11) : sans objet, non déployée.** Le fournisseur Reddit continue de composer ses requêtes à partir du repli par mots-clés dérivés (jamais des `mots_cles_en`/`mots_cles_fr` structurés du Scout, puisque ce champ n'existe même pas en base) — un mécanisme moins précis, mais qui n'a en pratique presque plus d'effet puisque le Scout ne produit plus jamais de vraie hypothèse (§2).

### Pages stockées par étiquette (depuis le rapport précédent)

| Étiquette | Pages |
|---|---:|
| `signal_concurrence` | 339 |
| *(source Scout d'origine, sans étiquette)* | 166 |
| `prix` | 36 |
| `preuve_enquete` | 35 |
| `extrait_flux` (Reddit) | 18 |

### Consommation des plafonds (requêtes de recherche / fetchs de pages, par jour calendaire UTC)

| Jour | Requêtes de recherche | Fetchs de pages |
|---|---:|---:|
| 27/09 (jour complet) | **3 500 / 3 000 (+16,7 %)** | 799 / 1 500 |
| 28/09 (jusqu'à ~12h UTC) | 2 848 / 3 000 (94,9 %) | 812 / 1 500 |

Le plafond de requêtes de recherche (3 000/jour) est de nouveau dépassé le 27/09, dans les mêmes proportions qu'au rapport précédent — toujours pas corrigé, toujours pas un dépassement massif (mesuré via `journal_http`, pas le compteur interne exact du budget), mais systématique sur trois jours consécutifs désormais.

### « Reddit désactivé » : quelle part des dossiers n'a que HN et le magasin interne ?

**Reddit n'est pas désactivé en production (§0)** — la question telle que posée présuppose 3.11. Ce qui est réellement mesurable, sur les 165 dossiers de la fenêtre récente :
- **43,6 % (72/165) n'ont aucun enrichissement du tout** — uniquement la source Scout d'origine (aucune requête n'a rien trouvé, ou le dossier n'a pas encore été traité par l'Enquêteur).
- Sur les **56,4 % enrichis**, la combinaison la plus fréquente est `HN + magasin interne + prix` (48 dossiers) — **Reddit ne contribue qu'à 17,0 % du total (28/165)**, malgré le fait qu'il tourne toujours activement. Ce n'est donc pas un effet d'une désactivation, mais la conséquence de son faible taux de succès HTTP (51,7 %, ci-dessus) et de son plafond à 30 % du quota (3.9, réellement déployée).

---

## 4. Le plafond de score, critère par critère

**Aucun score réel n'a été produit depuis le 26/09 12:58 UTC** (§1) : les 1 073+1 073 assessments Analyst/Critic de la fenêtre sont tous des replis à `score_prudent=0`. L'analyse par critère demandée n'a de sens que sur les **dossiers réellement évalués avant la panne** — c'est la population utilisée ci-dessous (997 scores réels, `date_creation < 2026-09-26 13:41:59`, tous produits entre le 25/09 et le 26/09 midi).

**Distribution du score prudent (997 scores réels)** : min 0,0 — médiane 22,5 — p90 37,5 — **max 55,0** — 6 dossiers > 50 — **0 dossier > 60, aucun n'a jamais dépassé ce seuil depuis le début du projet.**

### Top 30 (réel), moyenne par critère et répartition 0 / 50 % / 100 %

| Critère | Max | Points moyens (/max) | % du max | Inconnu (0) | 50 % | 100 % |
|---|---:|---:|---:|---:|---:|---:|
| `probleme_frequence_cout` | 20 | 14,33 | 71,7 % | 1 | 15 | 14 |
| `acces_clients` | 15 | 8,50 | 56,7 % | 1 | 24 | 5 |
| `concurrence_differenciation` | 10 | 5,67 | 56,7 % | 2 | 22 | 6 |
| `faisabilite_risque` | 10 | 6,50 | 65,0 % | 0 | 21 | 9 |
| `gain_realisable_ia` | 15 | 5,50 | 36,7 % | 10 | 18 | 2 |
| **`acheteur_disposition_payer`** | 20 | **5,00** | **25,0 %** | **16 (53,3 %)** | 13 | **1** |
| **`economie_cout_lancement`** | 10 | **2,67** | **26,7 %** | **14 (46,7 %)** | 16 | **0** |

**Deux critères plafonnent tout le monde, y compris les 30 meilleurs dossiers jamais produits :**
- `acheteur_disposition_payer` (20 points, le critère le plus lourd avec `probleme_frequence_cout`) : inconnu dans plus de la moitié du top 30 (16/30), et sur les 14 restants, jamais mieux qu'« indices partiels » sauf **une seule fois sur trente** — jamais deux affirmations solides (`observé`/`calculé`) sur la disposition à payer, même dans les meilleurs dossiers.
- `economie_cout_lancement` (10 points) : inconnu dans 14/30, et **jamais un seul « fort » sur les trente** (0/30 à 100 %) — c'est le seul des 7 critères qui n'atteint jamais le niveau de preuve maximal dans tout le top 30.

**Ces preuves manquantes sont-elles introuvables ou présentes mais non citées ?** Vérifié dossier par dossier (jointure `opportunity_evidence` → `sources`, sur les 16+14 cas « inconnu ») : **presque toujours introuvables, pas non citées.** 15 des 16 dossiers « inconnu » sur `acheteur_disposition_payer`, et 13 des 14 sur `economie_cout_lancement`, n'ont **aucune** source étiquetée `prix` du tout attachée — l'Enquêteur n'a simplement jamais trouvé ni tenté de trouver une page de tarification pour ces dossiers (pas de concurrent identifié par la sous-étape 3.4b, ou requête sans résultat).

Le seul cas inverse (dossier `79a9d42a…`, dans le top 30) est instructif : il a bien 8 sources étiquetées `prix`, et pourtant les deux critères restent « inconnu ». En lisant le contenu réel de ces sources, ce n'est pas un défaut de citation de l'Analyst mais un **faux positif du mécanisme d'identification de concurrents (3.4b)** : le marqueur d'offre par mot entier a identifié comme « concurrents » des articles TechCrunch sans rapport (« Mark Wahlberg is coming to Disrupt 2026 », l'accord cloud Anthropic–Akamai) plutôt que de vrais éditeurs concurrents — les pages « prix » récupérées ne parlent donc jamais de prix réel, et l'Analyst a eu raison de ne rien en tirer.

**Conclusion de cette section : le plafond de 55 (jamais 60) tient à deux critères précis, et la cause dominante est en amont de l'Analyst — l'Enquêteur ne produit quasiment jamais de preuve de prix/disposition à payer exploitable, que ce soit par absence de requête (cas dominant) ou par une identification de concurrents trop bruitée (cas minoritaire mais réel).**

---

## 5. Le Critic

**L'étape 5 n'a pas commencé (§0)** : pas de champ `type` sur les objections, pas de décision reprise par le code. Ce qui suit est ce qui est réellement mesurable aujourd'hui.

### Répartition des décisions (997 scores réels, tous avant la panne)

| Décision | Dossiers | Part |
|---|---:|---:|
| `a_verifier` | 725 | 72,7 % |
| `rejeter` | 272 | 27,3 % |
| **`eligible_revue_humaine`** | **0** | **0,0 %** |

**Le Critic n'a jamais, une seule fois depuis le tout début du projet, rendu la décision « éligible ».** Le Constat 1 du diagnostic initial (§1 d'`AMELIORATIONS.md`) reste entièrement vrai.

### Objections

Sur 504 assessments Critic réels échantillonnés (avant la panne), **162 (32,1 %) portent au moins une objection** — objections en texte libre uniquement (champ `texte` + `source_ids`, pas de `type`), donc aucune répartition par type possible. Exemple réel (dossier à faible score) : *« L'unique source disponible est une citation d'opinion rapportée (Jensen Huang) sur un forum, sans données chiffrées, méthodologie ou étude vérifiable (…) Aucune preuve de disposition à payer : zéro affirmation sur ce critère (…) »* — le contenu, quand le Critic tourne réellement, est bien un vrai examen critique du dossier, pas une formalité.

**Fait notable trouvé en préparant le détail des 3 premiers dossiers (§6) : même les meilleurs dossiers de toute l'histoire du radar (scores 55,0 / 55,0 / 52,5) ont un Critic en mode repli** (« Mode sans modèle : impossible de challenger le dossier automatiquement »), daté du **25/09**, donc *avant* la panne du 26/09 et avant même le déploiement du correctif de fiabilité (3.10, déployé le 26/09). Cela confirme que le problème de fiabilité du Critic — 78,33 % d'échecs mesuré le 26/09 matin, corrigé par 3.10 — touchait déjà les tout meilleurs dossiers dès le 25/09 : le score de ces dossiers repose donc sur un Analyst réel et riche, mais sur **aucune critique réelle jamais reçue**.

**Taux d'accord avec le code, premiers dossiers éligibles** : sans objet (§0) — il n'existe pas de décision codée à comparer, et aucun dossier n'a jamais atteint `eligible_revue_humaine`.

---

## 6. Top 10 des dossiers (réels, avant la panne)

| # | Score | Décision | Secteur | Sources | Titre |
|---|---:|---|---|---:|---|
| 1 | 55,0 | a_verifier | intersectoriel | 1 | Plateforme d'analyse et de cartographie des tendances technologiques en temps réel |
| 2 | 55,0 | a_verifier | intersectoriel | 1 | Plateforme de wellness numérique combinant virtualisation environnementale et IA générative |
| 3 | 52,5 | a_verifier | intersectoriel | 1 | Plateforme SaaS d'optimisation automatique de kernels CUDA pilotée par agents IA |
| 4 | 52,5 | a_verifier | intersectoriel (défaut) | 1 | Whiteboard (YC W26) – IDE open-source pour la conception logicielle |
| 5 | 52,5 | a_verifier | intersectoriel | 1 | Plateforme d'applications serveur haute performance basée sur Rust (Topcoat) |
| 6 | 52,5 | a_verifier | operations_petites_entreprises (défaut) | 1 | Acquéreur chevronné cherchant à consolider une PME MSP en croissance contrôlée |
| 7 | 50,0 | a_verifier | intersectoriel | 1 | Fonds de capital-risque spécialisé en IA pour le marché indien |
| 8 | 50,0 | rejeter | intersectoriel | 1 | Plateforme de communication sécurisée pour petits groupes de confiance basée sur SSH |
| 9 | 50,0 | rejeter | e_commerce | 1 | Wave – Outil SaaS de génération de backgrounds visuels avec dégradés animés |
| 10 | 50,0 | a_verifier | intersectoriel | 1 | Storia – Teleprompter intelligent avec suggestions de mood et pacing |

**Les 10 premiers dossiers de toute l'histoire du radar n'ont chacun qu'une seule source** — même constat que celui du diagnostic initial, l'Enquêteur (déployé après leur création, le 25/09) n'a jamais eu l'occasion de les enrichir rétroactivement.

### Détail complet des 3 premiers

**1. Plateforme d'analyse et de cartographie des tendances technologiques en temps réel (55,0, `a_verifier`)**
- **Hypothèse Scout** — acheteur : éditeurs de contenu tech, analystes de tendances, veilleurs technologiques, investisseurs VC ; douleur : difficulté à identifier rapidement les sujets émergents sur les communautés tech ; mécanisme IA : agrégation/classification automatique des discussions, détection d'émergence de sujets, cartographie visuelle dynamique ; pourquoi maintenant : explosion des sources d'info + outils NLP matures.
- **Affirmations (Analyst), une par critère, toutes sourcées sur le même post Show HN** : outil déjà construit (`probleme_frequence_cout`, observé) ; post à 1 point/0 commentaire = aucune traction commerciale mesurable (`acheteur_disposition_payer`, observé — mais classé en défaveur, d'où le critère à 25 % malgré une affirmation) ; usage probable de NLP mais aucun détail technique (`gain_realisable_ia`, hypothèse) ; diffusion Show HN sans preuve de conversion (`acces_clients`, observé) ; le créateur demande lui-même « Is it useful? What's missing? », signe de différenciation non validée (`concurrence_differenciation`, observé) ; prototype fonctionnel déjà en place, coût de dev engagé mais non chiffré (`economie_cout_lancement`, observé) ; prototype fonctionnel + très faible engagement = risque de marché non résolu (`faisabilite_risque`, 2 affirmations observées).
- **Objections du Critic** : aucune — repli sans modèle (§5).
- **Prochain test le moins coûteux (Analyst)** : publier un sondage ou une landing page auprès d'éditeurs de contenu tech / analystes / VC pour mesurer l'intérêt et la disposition à payer avant tout développement supplémentaire.

**2. Plateforme de wellness numérique combinant virtualisation environnementale et IA générative (55,0, `a_verifier`)**
- **Hypothèse Scout** — acheteur : entreprises santé mentale/RH, plateformes de méditation/wellness, éditeurs lifestyle ; douleur : stress/TDAH/surcharge mentale des travailleurs ; mécanisme IA : IA générative pour produire des environnements de détente personnalisés ; pourquoi maintenant : normalisation du prototypage par IA, urgence du bien-être mental.
- **Affirmations (Analyst)** : créateur du produit déclare vivre un TDAH et un stress important (`probleme_frequence_cout`, observé) ; produit = side-project Show HN sans mention de paiement (`acheteur_disposition_payer`, observé — un seul fait, donc 50 % et non 0, mais un fait qui joue contre le critère) ; la source ne mentionne **pas explicitement** de composant IA générative, malgré l'hypothèse du Scout — l'usage de l'IA reste une hypothèse non confirmée (`gain_realisable_ia`) ; diffusion via Hacker News, pas de canal B2B RH/wellness (`acces_clients`, observé) ; positionnement « jardin zen virtuel » simple (`concurrence_differenciation`, observé) ; projet solo, coût de dev probablement faible mais non chiffré (`economie_cout_lancement`, hypothèse) ; produit déjà fonctionnel et public (`faisabilite_risque`, observé).
- **Objections du Critic** : aucune — repli sans modèle.
- **Prochain test le moins coûteux (Analyst)** : interviewer 5 à 10 responsables RH ou wellness managers pour valider l'intérêt et la disposition à payer avant tout développement supplémentaire ; en variante, une landing page B2B RH/wellness pour mesurer les demandes de démo.

**3. Plateforme SaaS d'optimisation automatique de kernels CUDA pilotée par agents IA (52,5, `a_verifier`)**
- **Hypothèse Scout** — acheteur : entreprises HPC, studios d'IA/ML, centres de recherche avec infrastructure GPU ; douleur : optimisation manuelle et chronophage de kernels CUDA, expertise rare et coûteuse ; mécanisme IA : agents capables de compiler/exécuter/benchmarker/profiler et itérer en autonomie ; pourquoi maintenant : maturité des frameworks multi-agents (langgraph), adoption croissante des GPU.
- **Affirmations (Analyst)** : l'auteur a personnellement travaillé à optimiser des kernels CUDA (`probleme_frequence_cout`, observé) ; **aucune affirmation du tout** sur `acheteur_disposition_payer` (0 %, inconnu — aucun signal de prix/budget/intérêt commercial) ; système d'agents réellement construit, benchmarks/profiling via Nsight fonctionnels (`gain_realisable_ia`, 2 affirmations observées — c'est le seul des 3 dossiers à atteindre 100 % sur ce critère) ; post Show HN, sans donnée d'engagement (`acces_clients`, observé) ; projet présenté comme exploration personnelle, pas produit commercial (`concurrence_differenciation`, observé) ; harnais de test développé en side-project (`economie_cout_lancement`, observé) ; prototype fonctionnel déjà démontré (`faisabilite_risque`, observé).
- **Objections du Critic** : aucune — repli sans modèle.
- **Prochain test le moins coûteux (Analyst)** : contacter 5 à 10 ingénieurs HPC/ML (commentaires du post ou LinkedIn) pour valider par entretien si l'optimisation CUDA manuelle représente un coût/temps significatif, et s'ils seraient prêts à payer pour un outil agentic automatisé.

---

## 7. Conclusion

1. **Non, le pipeline ne fonctionne toujours pas de bout en bout.** La panne totale décrite le 27/09 n'a jamais cessé : 46 h 23 min sans un seul appel Scout, Analyst ou Critic réussi, jusqu'à l'instant de cette mesure. Ce n'est pas une nouvelle panne — c'est la même, non corrigée en production.
2. **La cause n'est pas mystérieuse : le correctif existe, testé, prouvé par un vrai appel payant (0,0392 €), depuis la sous-étape 3.13 (27/09).** Il n'a simplement jamais été poussé vers le dépôt public ni déployé sur Render — `git log` et le schéma de production le confirment tous les deux, indépendamment.
3. **Le coût de l'attente se compte en dossiers, pas en euros** : 653 dossiers vides créés depuis le début de la panne (+159 depuis le dernier rapport), 0 reprise, 0 hypothèse Scout exploitable, 0 secteur par citation vérifiée. Le coût réel en euros reste, lui, à 0,00 € tout du long.
4. **L'Enquêteur, qui ne dépend pas du modèle, continue de bien fonctionner et s'améliore même** : 43,0 % de dossiers à une seule source sur la fenêtre récente (contre 71,5 % sur tout le cumul de la panne), grâce aux corrections déjà déployées (3.9, 3.10). Ce n'est plus le goulot principal — mais tant que rien ne juge les dossiers, cette richesse ne sert à rien.
5. **Sur les dossiers réellement notés avant la panne (997), le plafond de score reste à 55, jamais 60**, et l'analyse critère par critère (demandée pour la première fois ici) l'explique précisément : `acheteur_disposition_payer` (inconnu dans 53 % du top 30, jamais « fort » sauf une fois) et `economie_cout_lancement` (inconnu dans 47 %, jamais « fort », 0/30) sont les deux vrais plafonds — presque toujours parce que l'Enquêteur ne trouve ou ne tente jamais de page de prix pour ces dossiers, rarement (1 cas sur 30) parce que le mécanisme d'identification de concurrents (3.4b) se trompe de cible.
6. **Même les 3 meilleurs dossiers de toute l'histoire du projet n'ont jamais reçu de vraie critique** : leur Critic est en mode repli depuis le 25/09, avant même la panne officielle — signe que la fragilité corrigée par 3.10 touchait déjà les dossiers les plus prometteurs.
7. **Le Critic n'a jamais dit « éligible »** — 0 fois sur 997 scores réels. L'étape 5 (calibrage, décision par le code, objections typées) n'a pas commencé.
8. **« Reddit désactivé » ne correspond pas à la réalité déployée** : Reddit reste actif côté Enquêteur (3.11 non déployée), à 51,7 % de succès HTTP, plafonné à 30 % du quota — il ne contribue qu'à 17,0 % des dossiers enrichis, pas par désactivation mais par performance réseau et par quota.
9. **Correction la plus rentable, sans l'implémenter, par ordre de priorité, sans changement depuis le 27/09** :
   a. **Urgence absolue, avant tout le reste** : déployer 3.13 (avec 3.11). Le correctif est prêt depuis 24 h de plus qu'hier ; chaque heure d'attente ajoute environ 7 dossiers vides et retarde d'autant la calibration du Critic (étape 5), qui a besoin de vrais scores pour démarrer.
   b. Une fois redéployé : vérifier dans les 15 minutes qui suivent que le disjoncteur API et `app.reprise` fonctionnent réellement sur les 653 dossiers en attente (pas seulement sur le test de fumée).
   c. Ensuite seulement : s'attaquer au vrai plafond de score identifié en §4 (`acheteur_disposition_payer`/`economie_cout_lancement`) — par exemple en étendant les familles de requêtes de l'Enquêteur au-delà de « demande »/« concurrence »/« prix » vers des signaux de pricing plus systématiques, ou en fiabilisant l'identification de concurrents de 3.4b (le faux positif observé en §4).
10. **Aucune correction n'est appliquée ici** — lecture seule, conforme au périmètre de cette sous-étape. La décision revient à Mathéo/Fable.
