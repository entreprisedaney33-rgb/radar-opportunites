# SCORING-V2 — le score d'une fiche « secteur × tâche »

Version de la règle : **2026.10.1** (`config/fiches.yaml::version`). Calculé par du **code** (`app/scoring_v2.py`), jamais par un modèle.
Tous les nombres ci-dessous sont dans `config/fiches.yaml` ; ce fichier explique la règle, il ne la remplace pas.

## Principe

100 points, cinq critères. Deux scores sont calculés pour chaque fiche :

- **score brut** : estimations centrales ; une obligation dont la date est « à confirmer » compte à demi-tarif (9 points) ;
- **score prudent** : estimations **pessimistes** (borne basse de l'intervalle de confiance à 95 % de la part de la tâche) ; seules les
  dates **fermes** comptent. **C'est le score prudent qui décide.**

Un critère dont la preuve manque (concurrence non évaluée, faisabilité non évaluée) vaut **0** dans les deux scores et apparaît dans
« preuves manquantes » : on ne devine jamais un point.

## Les chiffres d'entrée viennent d'un échantillon

L'étiquetage porte sur au plus 60 offres par code NAF (`config/etiquetage.yaml`). La part d'offres du secteur qui mentionnent la
tâche est donc une **estimation** ; le nombre d'offres = part × toutes les offres collectées du secteur sur 90 jours, avec la borne
basse = (borne basse de la part) × les offres du secteur. Le score ne lit **jamais** le compte brut de l'échantillon.

## Les cinq critères

| Critère | Points | Règle |
|---|---|---|
| **Demande** | 30 | Volume (18 pts max) : échelle logarithmique sur le nombre d'offres estimé sur 90 jours — 0 sous 10 offres, plein à partir de 1 000. Brut : valeur centrale ; prudent : borne basse. + 6 si le salaire annuel médian des offres de la tâche atteint 25 000 € (au moins 5 salaires lisibles). + 6 si la tendance sur 3 mois est **calculée** et positive (elle ne l'est qu'après 180 jours d'accumulation de la collecte). |
| **Proximité** | 20 | Établissements du secteur listés à moins de 100 km de Bordeaux : ≥ 500 → 16, ≥ 200 → 12, ≥ 50 → 8, ≥ 20 → 4. + offres de la tâche estimées dans la zone : ≥ 10 → 4, ≥ 3 → 2 (prudent : borne basse). |
| **Déclencheur** | 15 | Une obligation réglementaire touchant le secteur **et la tâche** (rattachement `taches` de `config/declencheurs.yaml`, **rattachement accepté PROVISOIREMENT le 2026-10-01, à relire à V2.9 sur fiches réelles** : sans lui, la facturation électronique donnerait 15 points à tous les couples ; toute fiche dont ce critère rapporte des points porte un avertissement), datée entre 6 mois dans le passé et 18 mois dans l'avenir (source officielle) : date ferme → 15 ; date à confirmer → 9 (brut seulement). À défaut, une tendance d'offres calculée > +20 % → 8. |
| **Concurrence** | 20 | Règle (V2.6) : outils dédiés présents mais aucun service local → 20 ; aucun outil dédié → 10 (marché non prouvé) ; service local établi → 0. **Tant que la concurrence n'est pas évaluée : 0 point**, « non évalué », listée dans les preuves manquantes — la fiche est produite quand même. Elle est évaluée soit par la recherche web bornée (`app/concurrence.py`, **désactivée** : décision de Mathéo du 2026-10-01, aucune clé de moteur payant), soit en session (procédure V2.6b, `PROCEDURE-V2.6b.md`) ; dans les deux cas chaque outil, prix et prestataire local porte son URL https. Une requête sans résultat ou une panne ne vaut JAMAIS « aucun outil » (ce serait gagner 10 points sur une panne) : elle reste « non évaluée ». |
| **Accessibilité** | 15 | **Porte** d'abord (`config/faisabilite.yaml`) : fermée si investissement > 20 k€, premier revenu au-delà de 12 mois, marché inaccessible, compétence réglementaire lourde ou matériel industriel, problème systémique — ou plus de deux personnes nécessaires. Porte ouverte : 15 si premier revenu en moins de 3 mois, 9 entre 3 et 12 mois. C'est une **hypothèse** de l'Analyste, typée comme telle. |

Plafond atteignable tant que la concurrence n'est pas évaluée : **80** (100 une fois évaluée). **V2.6 ne change pas la version de la règle** : la formule est la même, seule la donnée d'entrée arrive ; une fiche est recalculée quand sa concurrence a été évaluée après son dernier calcul.

## Ce que le code fait AVANT d'appeler un modèle (coût)

Une fiche coûte environ 0,055 € (Analyste ≈ 0,029 + Critic ≈ 0,023, modèle approfondi, mesuré le 2026-10-01). Trois filtres gratuits, dans l'ordre :

1. les **seuils de sélection** ci-dessous ;
2. le **pré-criblage** : on calcule le score prudent MAXIMAL atteignable (accessibilité parfaite, concurrence non évaluée = 0) ; sous 60, le couple ne pourra jamais être éligible : pas de fiche ;
3. un **plafond de fiches par secteur** (**2**, décision de Mathéo du 2026-10-01 ; les couples de plus forte demande d'abord).

Le Critic, lui, ne tourne que si la fiche peut encore devenir éligible (porte ouverte, score prudent ≥ 60, liste de prospection ≥ 30).

## La décision (par le code)

1. **Sélection** des couples candidats, sur les parts extrapolées et leur intervalle (`config/fiches.yaml::selection`) : secteur et couple non exclus ;
   au moins 30 offres étiquetées dans le secteur (sauf recensement complet) ; borne basse de la part ≥ 3 % ; au moins 50 offres estimées sur 90 jours
   et 20 au pire ; au moins 20 établissements listés dans le rayon.
2. **Gravité des objections du Critic : c'est le CODE qui la retient** (`gravite_retenue`), pas le Critic (décision de Mathéo du 2026-10-01) :
   - **seuls** `tache_non_automatisable`, `reglementaire` et `cible_injoignable` peuvent être **structurelles** ;
   - `concurrence_locale` n'est structurelle **que si le Critic nomme un service local précis ET donne sa source** (adresse https) ; sans les deux, elle est
     rabattue en « à vérifier » (la concurrence n'étant pas évaluée, le Critic ne doit rien inventer) ;
   - `deja_equipe` (« les établissements ont peut-être déjà un outil ») et `chiffres_fragiles` ne sont **jamais** structurelles.
3. **Exclue**, avec motif : porte d'accessibilité fermée ; plus de deux personnes nécessaires ; objection structurelle d'un des trois types ci-dessus (un rejet du
   Critic n'exclut que s'il est appuyé par une telle objection).
4. **« Déjà équipés » envoie la fiche en « à vérifier »**, quel que soit le score : la fiche porte alors, comme prochain test, **une question à poser au client** —
   « Qui s'occupe aujourd'hui de <la tâche> dans votre établissement, avec quel outil, et combien de temps par semaine cela représente-t-il ? ».
5. **Éligible à la prospection** : score prudent ≥ 60, **aucune** objection structurelle retenue, **aucune** objection « déjà équipés », liste de prospection ≥ 30
   établissements, le Critic ayant répondu et ne recommandant pas de rejeter. Un Critic qui n'a pas pu répondre ne rend jamais une fiche éligible.
6. **Service local établi** (concurrence évaluée, `service_local.present`) : la fiche ne peut pas être éligible ; elle part « à vérifier » avec le nom du prestataire et sa source (décision de Mathéo du 2026-10-01, V2.6). Elle ne masque jamais une exclusion.
7. Sinon **à vérifier**, avec la liste des preuves manquantes (critère non évalué, score prudent insuffisant, liste trop courte, objection à lever, question à poser).

Le Critic n'a **jamais** accès au score. Une fiche n'est jamais produite par repli : sans sortie valide de l'Analyste, aucune ligne n'est écrite.

## Budget de la première cartographie (décision de Mathéo du 2026-10-01)

Enveloppe unique de **50 €** (`RADAR_ENVELOPPE_INITIALE_EUR=50`, jamais au-delà de `enveloppe_max_eur`), cumulée sur l'étiquetage des offres ET les fiches ; 60 offres
étiquetées par secteur ; 2 fiches par secteur au plus. Le code s'arrête de lui-même à l'enveloppe.

## Rafraîchissement

Une fiche est recalculée après 30 jours, ou quand un chiffre d'entrée (offres estimées, part, offres dans la zone, établissements, stock du secteur) a bougé de plus de 20 %
depuis son calcul, ou quand la version de la règle ou du prompt a changé. Les anciennes lignes sont conservées (historique).

## Ce que le score ne dit pas

Il mesure la **demande** observée dans les offres d'emploi, la **proximité** des cibles et l'**accessibilité** estimée ; il ne prouve ni qu'un acheteur paiera, ni
que la concurrence est faible (V2.6). Les « j'irais voir / non, parce que » de Mathéo (V2.8-V2.9) sont le seul juge qui compte.
