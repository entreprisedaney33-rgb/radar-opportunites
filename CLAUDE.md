# CLAUDE.md — `produits/radar-opportunites/`

> Index court. Détail complet dans [`README.md`](README.md) (mise en route),
> [`ARCHITECTURE.md`](ARCHITECTURE.md) (constat Phase 0, coûts, décisions en
> attente) et [`SCORING.md`](SCORING.md) (ancres du score).

Produit interne : radar nocturne qui repère des opportunités économiques où
l'IA change concrètement le coût/délai/qualité d'un problème identifié, les
qualifie via 3 rôles (Scout/Analyst/Critic) et produit des dossiers
traçables (sources, preuves typées, score déterministe, objections).

État au 2026-09-25 (première session) :
- **Phase 0 et Phase 1 faites** : pipeline local complet et testé (30 tests,
  aucun appel réseau/modèle dans les tests), sans rien créer de payant.
- **Phase 3 (Render réel) pas commencée** : `render.yaml` est une
  proposition non déployée. Voir `ARCHITECTURE.md` pour le budget à valider
  avec Dorian avant toute activation payante ou nocturne réelle.
- Pas de compte Apify actif pour ce produit ; pas de clé de recherche web —
  collecte V1 = flux RSS publics + adaptateur de démonstration `[DEMO]`
  (`app/adapters/demo_adapter.py`).
- Base de données : **jamais** `n8n-db` — ce produit aura son propre
  Postgres Render, séparé, une fois le budget validé.
