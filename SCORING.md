# SCORING.md — ancres du score, version 2026.09.1

Ce fichier doit rester identique à ce que fait réellement
[`app/scoring/engine.py`](app/scoring/engine.py). Si vous changez l'un, changez l'autre
**dans le même commit**.

Le score ne provient jamais du "ton" ou du sentiment d'un modèle. Il provient
d'affirmations typées et sourcées (`observé`, `calculé`, `hypothèse`,
`non_vérifié`) que l'Analyst extrait, et que ce code compte selon des règles
fixes.

## Les 7 critères (poids dans `config/poids_scoring.yaml`)

| Critère | Maximum |
|---|---:|
| Problème, fréquence et coût actuel | 20 |
| Acheteur et disposition à payer | 20 |
| Gain réalisable par IA | 15 |
| Accès aux clients | 15 |
| Concurrence et différenciation | 10 |
| Économie et coût de lancement | 10 |
| Faisabilité et risque | 10 |
| **Total** | **100** |

## Les 3 ancres, par critère (`evaluer_critere`)

Pour un critère donné, seules comptent les affirmations **sourcées** (au
moins un `source_id` réel, une source effectivement collectée) :

- **Inconnu** (`fraction = None`) : aucune affirmation sourcée de type
  `observé`/`calculé`, ni même de type `hypothèse`. Le critère n'a
  simplement pas été renseigné, ou toutes ses affirmations ont perdu leur
  source (par exemple parce qu'une source citée n'était pas dans le
  périmètre autorisé — voir plus bas).
- **0,5 — indices partiels** : soit exactement une affirmation
  `observé`/`calculé` sourcée, soit uniquement des affirmations
  `hypothèse` (aucune observée/calculée). Un seul fait dur, ou seulement
  des pistes non confirmées : ni absent, ni solide.
- **1,0 — preuves solides** : au moins deux affirmations `observé`/`calculé`
  sourcées.

Il n'y a volontairement pas de palier intermédiaire supplémentaire : trois
ancres reproductibles valent mieux qu'un barème fin mais arbitraire.

## `score_brut` vs `score_prudent`

- **`score_brut`** : moyenne pondérée calculée **uniquement sur les
  critères connus** (ceux qui ne sont pas `None`). Un dossier qui n'a
  investigué que 2 critères sur 7, mais très bien, peut afficher un
  `score_brut` élevé — c'est voulu : ce chiffre répond à "sur ce qu'on a
  regardé, qu'est-ce que ça donne ?", pas "est-ce qu'on a tout regardé ?".
- **`score_prudent`** : calculé sur les 100 points totaux. Un critère
  inconnu ne rapporte **jamais 50% par défaut** — il rapporte
  `fraction_inconnue_score_prudent × son maximum`, une valeur de
  configuration (`config/poids_scoring.yaml`), à 0,0 par défaut. C'est ce
  chiffre qui sert au classement du top de la nuit : il pénalise
  mécaniquement un dossier peu investigué, sans jugement de valeur du
  modèle.
- **`couverture_preuves`** : part (0 à 1) du total de 100 points portée par
  des critères connus. Un `score_brut` élevé avec une `couverture_preuves`
  faible est un signal d'alerte à afficher, pas à cacher.

## Ce qui n'entre jamais dans le score

- **Le sentiment du Critic.** Le Critic rend une décision
  (`rejeter`/`a_verifier`/`eligible_revue_humaine`) qui change le **statut**
  de l'opportunité, jamais ses points. Une objection Critic ne devient un
  point de score que si elle fait évoluer les affirmations de l'Analyst
  elles-mêmes (nouvelle version du dossier, donc nouvel `INSERT` dans
  `scores` — jamais un `UPDATE` du score précédent).
- **Une citation hors périmètre.** Si un rôle cite une source qui n'a pas
  été effectivement collectée et rattachée à l'opportunité (donc
  potentiellement inventée, ou reprise d'un texte hostile qui essaierait de
  se faire passer pour une preuve), le code neutralise l'affirmation :
  `source_ids` vidé et type forcé à `non_vérifié` — voir
  `app/roles/analyst.py::_neutraliser_sources_hors_perimetre` et
  l'équivalent dans `critic.py`. Une affirmation `non_vérifiée` n'est
  jamais comptée comme `observé`/`calculé`/`hypothèse` par
  `evaluer_critere`.

## Historique, jamais écrasé

Chaque calcul de score est un nouvel `INSERT` dans la table `scores` — un
score précédent n'est jamais modifié ni supprimé (`app/storage/repo.py::inserer_score`).
`historique_scores(opportunity_id)` renvoie tout l'historique, dans l'ordre.
