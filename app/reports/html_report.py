"""Génère le rapport HTML du matin (§4, §6). Tout le contenu collecté est
échappé avant insertion : ce texte vient du web, ce n'est jamais du HTML de
confiance à exécuter tel quel."""
from __future__ import annotations

from html import escape
from zoneinfo import ZoneInfo

from sqlalchemy.engine import Engine

from app.storage import repo
from app.storage.schema import opportunity_evidence, sources


def _preuves_de(engine: Engine, opportunity_id: str) -> list[dict]:
    from sqlalchemy import select

    with engine.connect() as cx:
        rows = cx.execute(
            select(opportunity_evidence.c.claim, opportunity_evidence.c.type, opportunity_evidence.c.independant,
                   sources.c.url_canonique)
            .select_from(opportunity_evidence.join(sources, opportunity_evidence.c.source_id == sources.c.id))
            .where(opportunity_evidence.c.opportunity_id == opportunity_id)
        ).all()
    return [{"claim": c, "type": t, "independant": i, "url": u} for c, t, i, u in rows]


def generer_rapport_html(engine: Engine, run_id: str) -> str:
    run = repo.get_run(engine, run_id)
    top = repo.lister_opportunites_par_run(engine, run_id)

    heure_paris = run["debut"].astimezone(ZoneInfo("Europe/Paris")).strftime("%Y-%m-%d %H:%M") if run else "?"

    lignes_dossiers = []
    for entree in top:
        opp = entree["opportunite"]
        score = entree["dernier_score"]
        if not opp:
            continue
        preuves = _preuves_de(engine, opp["id"])
        liens = "".join(
            f'<li><a href="{escape(p["url"])}" target="_blank" rel="noopener">{escape(p["url"])}</a> '
            f'— <em>{escape(p["type"])}</em>{" (non indépendante)" if not p["independant"] else ""} : '
            f'{escape(p["claim"][:200])}</li>'
            for p in preuves
        )
        score_brut = score["score_brut"] if score else "?"
        score_prudent = score["score_prudent"] if score else "?"
        couverture = score["couverture_preuves"] if score else "?"
        decision = score["decision_critic"] if score else "?"
        flags = ", ".join(score["flags_json"]) if score and score["flags_json"] else "aucun"

        lignes_dossiers.append(f"""
        <section class="dossier">
          <h2>{escape(opp['titre'])} <span class="statut statut-{escape(opp['statut'])}">{escape(opp['statut'])}</span></h2>
          <p><strong>Acheteur :</strong> {escape(opp['acheteur'])} — <strong>Secteur :</strong> {escape(opp['secteur'])}</p>
          <p><strong>Problème :</strong> {escape(opp['probleme'])}</p>
          <p><strong>Mécanisme IA :</strong> {escape(opp['mecanisme_ia'])}</p>
          <p><strong>Score brut :</strong> {score_brut}/100 — <strong>Score prudent :</strong> {score_prudent}/100 —
             <strong>Couverture de preuves :</strong> {couverture} — <strong>Décision Critic :</strong> {escape(str(decision))}</p>
          <p><strong>Drapeaux :</strong> {escape(flags)}</p>
          <p><strong>Preuves et sources :</strong></p>
          <ul>{liens or '<li>aucune preuve rattachée</li>'}</ul>
        </section>
        """)

    resume = (run or {}).get("resume_json") or {}
    suggestions = resume.get("suggestions_fusion_a_revoir") or []
    lignes_suggestions = "".join(
        f"<li>Nouvelle opportunité {escape(s['nouvelle'])} pourrait être la même que "
        f"{escape(s['existante_suggeree'])} (similarité {s['similarite']}) — à confirmer par un humain</li>"
        for s in suggestions
    )

    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<title>Radar d'opportunités — run {escape(run_id[:8]) if run else '?'}</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; }}
  .dossier {{ border: 1px solid #ddd; border-radius: 8px; padding: 1rem 1.5rem; margin-bottom: 1.5rem; }}
  .statut {{ font-size: 0.75rem; padding: 0.15rem 0.5rem; border-radius: 4px; background: #eee; }}
  .resume {{ background: #f7f7f7; padding: 1rem 1.5rem; border-radius: 8px; margin-bottom: 2rem; }}
  code {{ background: #f0f0f0; padding: 0.1rem 0.3rem; }}
</style>
</head>
<body>
<h1>Radar d'opportunités économiques IA — rapport du matin</h1>
<p>Run <code>{escape(run_id) if run else '?'}</code> — début {escape(heure_paris)} (Europe/Paris) — statut {escape((run or {}).get('statut', '?'))}</p>

<div class="resume">
  <h2>Résumé du run</h2>
  <ul>
    <li>Signaux lus : {resume.get('signaux_lus', '?')} (déjà vus ignorés : {resume.get('signaux_deja_vus_ignores', '?')})</li>
    <li>Opportunités nouvelles : {resume.get('opportunites_nouvelles', '?')} — fusionnées : {resume.get('opportunites_fusionnees', '?')}</li>
    <li>Analyses terminées : {resume.get('analyses_terminees', '?')} — critiques terminées : {resume.get('critiques_terminees', '?')}</li>
    <li>Coût estimé : {(run or {}).get('couts_json', {}).get('total_eur_estime', '?')} €</li>
    <li>Budget atteint : {resume.get('budget_atteint', False)} — temps écoulé : {resume.get('temps_ecoule', False)}</li>
    <li>Sources indisponibles : {escape(', '.join(resume.get('sources_indisponibles', [])) or 'aucune')}</li>
  </ul>
  {"<p><strong>Suggestions de fusion à revoir (jamais fusionnées automatiquement) :</strong></p><ul>" + lignes_suggestions + "</ul>" if suggestions else ""}
</div>

<h2>Top de la nuit</h2>
{"".join(lignes_dossiers) or "<p>Aucune opportunité n'a atteint le filtre cette nuit.</p>"}

</body>
</html>"""
