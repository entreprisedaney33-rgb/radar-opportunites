"""Interface interne minimale (§6) : liste des runs, dossier détaillé,
export JSON/CSV, décision humaine, bouton pause. Protégée par mot de passe
HTTP Basic partagé (RADAR_UI_PASSWORD) — pas de compte par personne en V1,
volontairement simple. Aucun endpoint public : tout passe par
`_verifier_auth`, y compris les exports.
"""
from __future__ import annotations

import csv
import hmac
import io
import json
from functools import wraps
from html import escape

from flask import Flask, Response, request

from app.config import get_settings
from app.storage import repo
from app.storage.db import get_engine, migrer

app = Flask(__name__)


def _verifier_auth() -> bool:
    mot_de_passe = get_settings().ui_password
    if not mot_de_passe:
        return False  # pas de mot de passe configuré -> accès refusé, jamais ouvert par défaut
    auth = request.authorization
    return bool(auth and hmac.compare_digest(auth.password or "", mot_de_passe))


def _proteger(vue):
    @wraps(vue)
    def enveloppe(*args, **kwargs):
        if not _verifier_auth():
            return Response(
                "Authentification requise.", 401,
                {"WWW-Authenticate": 'Basic realm="Radar interne"'},
            )
        return vue(*args, **kwargs)
    return enveloppe


@app.route("/")
@_proteger
def accueil():
    engine = get_engine()
    with engine.connect() as cx:
        from sqlalchemy import select
        from app.storage.schema import runs

        lignes = cx.execute(select(runs).order_by(runs.c.debut.desc()).limit(30)).mappings().all()
    items = "".join(
        f'<li><a href="/runs/{escape(r["id"])}">{escape(r["debut"].isoformat())}</a> '
        f'— {escape(r["mode"])} — {escape(r["statut"])}</li>'
        for r in lignes
    )
    pause_actuel = repo.lire_pause_all(engine)
    return f"""<!DOCTYPE html><html lang="fr"><meta charset="utf-8">
    <title>Radar — runs</title>
    <body style="font-family: system-ui, sans-serif; max-width: 700px; margin: 2rem auto;">
    <h1>Radar d'opportunités — runs récents</h1>
    <form method="post" action="/pause">
      <button name="valeur" value="{'0' if pause_actuel else '1'}">
        {'Lever la pause' if pause_actuel else 'PAUSE_ALL : tout arrêter'}
      </button>
      <span>(état actuel : {'EN PAUSE' if pause_actuel else 'actif'})</span>
    </form>
    <ul>{items or '<li>aucun run</li>'}</ul>
    </body></html>"""


@app.route("/pause", methods=["POST"])
@_proteger
def basculer_pause():
    valeur = request.form.get("valeur") == "1"
    repo.definir_pause_all(get_engine(), valeur)
    return Response(status=302, headers={"Location": "/"})


@app.route("/runs/<run_id>")
@_proteger
def voir_run(run_id: str):
    from app.reports.html_report import generer_rapport_html

    return generer_rapport_html(get_engine(), run_id)


@app.route("/runs/<run_id>/export.json")
@_proteger
def exporter_json(run_id: str):
    engine = get_engine()
    top = repo.lister_opportunites_par_run(engine, run_id)
    return Response(json.dumps(top, default=str, ensure_ascii=False, indent=2), mimetype="application/json")


@app.route("/runs/<run_id>/export.csv")
@_proteger
def exporter_csv(run_id: str):
    engine = get_engine()
    top = repo.lister_opportunites_par_run(engine, run_id)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["titre", "acheteur", "secteur", "statut", "score_brut", "score_prudent", "couverture_preuves", "decision_critic"])
    for entree in top:
        opp, score = entree["opportunite"], entree["dernier_score"]
        if not opp:
            continue
        writer.writerow([
            opp["titre"], opp["acheteur"], opp["secteur"], opp["statut"],
            score["score_brut"] if score else "", score["score_prudent"] if score else "",
            score["couverture_preuves"] if score else "", score["decision_critic"] if score else "",
        ])
    return Response(buffer.getvalue(), mimetype="text/csv")


@app.route("/opportunites/<opportunity_id>/decision", methods=["POST"])
@_proteger
def enregistrer_decision(opportunity_id: str):
    action = request.form.get("action")
    if action not in {"rejeter", "a_verifier", "selectionner"}:
        return Response("action invalide", 400)
    auteur = request.form.get("auteur", "humain:inconnu")
    justification = request.form.get("justification")
    repo.inserer_decision(get_engine(), opportunity_id=opportunity_id, auteur=auteur, action=action, justification=justification)
    if action == "selectionner":
        repo.maj_statut_opportunite(get_engine(), opportunity_id, "selectionne")
    return Response(status=204)


if __name__ == "__main__":
    migrer(get_engine())
    app.run(host="0.0.0.0", port=8000)
