# BASELINE — métriques de pilotage

Sert de point de référence "avant/après" pour toute future amélioration du
Scout/Analyst/Critic (voir `BRIEF-FABLE5-AMELIORER-RADAR.md`, remis à
Mathéo le 25/09/2026). Généré par `python -m app.metriques --jour AAAA-MM-JJ`
— lecture seule, aucun appel modèle. Détail complet dans
`rapports/metriques/<jour>.json`.

## 2026-09-25 (première mesure, journée en cours au moment de la mesure)

| Métrique | Valeur |
|---|---|
| Opportunités repérées | 120 |
| Opportunités analysées | 64 (56 encore en attente ou en cours) |
| Statuts | rejeté 18 · incertain 46 · en_analyse 1 · nouveau 55 |
| Décisions du Critic | rejeter 18 · à_vérifier 46 · **éligible_revue_humaine 0** |
| Score prudent | min 0 · médiane 25 · p90 35 · **max 45** · >60 : 0 · >80 : 0 |
| Part hors "intersectoriel" | **10,83 %** (89,17 % tombent dans la catégorie fourre-tout) |
| Sources par dossier | min 1 · médiane 1 · max 1 · **100 % des dossiers n'ont qu'une seule source** |
| Objections du Critic par type | (vide — le type n'existe pas encore dans le modèle de données) |
| Coût du jour | 4,5463 € |
| Coût moyen par dossier analysé | 0,071 € |

**Le chiffre le plus parlant de cette première mesure : `sources_par_dossier`
est bloqué à 1 partout.** Aucun dossier, aujourd'hui, n'a jamais accumulé
plus d'une preuve — cohérent avec le constat du brief pour Fable 5 : l'Analyst
n'a aucune capacité de recherche indépendante dans cette V1, il ne peut que
réutiliser l'unique signal que le Scout lui a donné. Combiné à `max: 45` sur
le score et `0` décision positive du Critic, cette mesure confirme
objectivement, sur un vrai run, ce qui n'était jusque-là qu'une hypothèse
qualitative.

À reproduire après chaque changement (sourcing du Scout, capacité de
recherche de l'Analyst, calibration du Critic) pour vérifier ce qui a
réellement bougé — pas seulement à l'œil.
