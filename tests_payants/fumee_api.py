"""Test de fumée PAYANT (sous-étape 3.13 d'AMELIORATIONS.md) -- COÛTE DE
L'ARGENT RÉEL (quelques centimes : 3 appels réels, un par rôle). JAMAIS dans
la suite par défaut (`tests/`, aucun appel réseau/modèle, garde-fou §0.2.5) —
ce fichier vit dans `tests_payants/`, hors de tout nom `test_*.py` reconnu
par pytest, lancé UNIQUEMENT à la main :

    ANTHROPIC_API_KEY=... python tests_payants/fumee_api.py

Objectif précis : la panne du 26/09/2026 (voir
`rapports/POINT_ETAPE_2026-09-27.md`) est passée inaperçue pendant ~25 h
parce qu'AUCUN test, réel ou simulé, n'appelle jamais vraiment l'API
Anthropic -- la suite par défaut simule le client, elle ne peut donc jamais
détecter un rejet 400 de l'API elle-même sur la forme réelle du schéma
envoyé. Ce script exerce les TROIS rôles (Scout, Analyst, Critic) sur un
dossier fixture minimal, avec un VRAI `ModelClient`, et échoue (code de
sortie 1) si l'un des trois retombe sur son repli heuristique -- exactement
le symptôme de la panne : un appel qui échoue ne lève pas forcément une
exception visible, il se contente de renvoyer une sortie de moins bonne
qualité, silencieusement.

D'après §5 (procédure de mise en production), obligatoire et vert avant tout
déploiement touchant `app/adapters/model_client.py` ou `app/models_schemas.py`."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from sqlalchemy import create_engine

from app import config as cfg
from app.pipeline.budget import BudgetTracker
from app.roles import analyst as role_analyst
from app.roles import critic as role_critic
from app.roles import scout as role_scout
from app.storage.db import migrer


def main() -> int:
    settings = cfg.get_settings()
    if not settings.has_model_access:
        print("ERREUR : ANTHROPIC_API_KEY absente -- ce test de fumée a besoin d'un vrai accès modèle.", file=sys.stderr)
        return 1

    from app.adapters.model_client import ModelClient

    with tempfile.TemporaryDirectory() as tmp:
        engine = create_engine(f"sqlite:///{Path(tmp) / 'fumee_api.db'}", future=True)
        migrer(engine)
        # Plafonds larges (quelques centimes attendus, jamais plus de 3
        # appels réels) -- ce test ne doit jamais être bloqué par le budget.
        budget = BudgetTracker(engine, "fumee-api-run", plafond_eur=1.0, plafond_appels_approfondis=10)
        model_client = ModelClient(settings, budget)

        echecs: list[str] = []

        texte_signal = (
            "Je passe environ 6 heures par semaine à rapprocher manuellement les paiements "
            "reçus sur notre compte bancaire avec les factures envoyées à nos clients -- "
            "aucun outil ne fait ça automatiquement pour une petite structure comme la nôtre."
        )
        scout_sortie, via_modele_scout = role_scout.executer_scout(
            signal_id="fumee-signal-1", texte=texte_signal, secteur="intersectoriel",
            model_client=model_client, modele=settings.model_tri,
        )
        print(f"Scout   : via_modele={via_modele_scout}  candidat={scout_sortie.opportunity_candidate!r}")
        if not via_modele_scout:
            echecs.append("scout")

        opportunite = {
            "titre": scout_sortie.opportunity_candidate, "acheteur": scout_sortie.buyer,
            "probleme": scout_sortie.pain,
        }
        preuves = [{"source_id": "fumee-source-1", "extrait": texte_signal}]

        analyst_sortie, via_modele_analyst = role_analyst.executer_analyst(
            opportunity_id="fumee-opp-1", opportunite=opportunite, preuves=preuves,
            model_client=model_client, modele=settings.model_approfondi,
        )
        print(f"Analyst : via_modele={via_modele_analyst}  criteres={len(analyst_sortie.criteres)}")
        if not via_modele_analyst:
            echecs.append("analyst")

        critic_sortie, via_modele_critic = role_critic.executer_critic(
            opportunity_id="fumee-opp-1", opportunite=opportunite, analyst_sortie=analyst_sortie,
            preuves=preuves, model_client=model_client, modele=settings.model_approfondi,
        )
        print(f"Critic  : via_modele={via_modele_critic}  decision={critic_sortie.decision.value}")
        if not via_modele_critic:
            echecs.append("critic")

        cout_total = budget.cout_total_reel()
        print(f"Coût réel total : {cout_total:.4f} €")

    if echecs:
        print(f"\nÉCHEC : rôle(s) retombé(s) sur le repli sans modèle : {', '.join(echecs)}", file=sys.stderr)
        return 1

    print("\nOK : les trois rôles ont répondu via un vrai appel au modèle.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
