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

## Trois lignes de lecture (sous-étape 0.3, AMELIORATIONS.md)

1. **La part de dossiers à une seule source confirme-t-elle le constat 1 ?**
   Oui, sans ambiguïté : 100 % des 120 dossiers repérés le 25/09
   (`sources_par_dossier.part_une_seule_source = 1.0`, min = médiane = max = 1)
   n'ont jamais eu qu'une seule source. L'Analyst n'a donc jamais eu, sur
   cette journée, plus d'un fait à sa disposition par dossier — le constat 1
   du plan (« l'alimentation en preuves est le goulot, pas le Critic ») est
   confirmé chiffre à l'appui, pas seulement en hypothèse.

2. **Le maximum de score observé est-il cohérent avec un plafond à 50 ?**
   Oui : le score prudent plafonne à 45/100 sur la journée (médiane 25,
   p90 35, 0 dossier au-dessus de 60). Avec une seule source par dossier et
   une règle de score 0 / 50 % / 100 % où l'ancre à 100 % exige deux faits
   forts sourcés par critère, aucun dossier ne peut mathématiquement
   dépasser ~50 sur la plupart des critères — le plafond observé colle à ce
   que prédit la mécanique du score, pas à un réglage trop sévère du Critic.

3. **Quelle part des opportunités vient de chaque flux ?**
   **Non calculable avec les données actuelles.** `app.metriques` (sous-étape
   0.2) calcule une répartition par secteur (`par_secteur`), mais pas de
   répartition par flux d'origine — cette notion n'existe formellement dans
   le modèle de données qu'à partir de la sous-étape 1.1 (`flux_origine`).
   En base, `sources.domaine` porte déjà le nom lisible du flux (voir
   `app/adapters/rss_adapter.py`, `domaine=self.nom`) : la donnée existe
   techniquement, mais son calcul demanderait soit une requête ad hoc sur la
   base de production, soit d'attendre l'étape 1. Cette session locale n'a
   pas d'accès à la base Render (pas de `DATABASE_URL` configuré ici) pour
   lancer cette requête. Voir la question ouverte correspondante en §9 de
   `AMELIORATIONS.md`.
