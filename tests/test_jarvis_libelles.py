"""V2.7 : la copie des libellés collée dans le workflow Jarvis (`jarvis-radar-recap`) doit suivre les référentiels du radar."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from app import referentiels

RACINE = Path(__file__).resolve().parents[1]
SCRIPT = RACINE / "scripts" / "generer_libelles_jarvis.py"


def _generateur():
    spec = importlib.util.spec_from_file_location("generer_libelles_jarvis", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_la_copie_des_libelles_est_a_jour():
    """Échoue si un référentiel a changé sans que `python scripts/generer_libelles_jarvis.py` ait été relancé (puis le workflow reposé)."""
    gen = _generateur()
    assert gen.SORTIE.exists(), "libelles.json absent : lancer scripts/generer_libelles_jarvis.py"
    assert gen.SORTIE.read_text(encoding="utf-8") == gen.texte(gen.construire()), \
        "libelles.json périmé : relancer scripts/generer_libelles_jarvis.py, committer, puis reposer le workflow Jarvis"


def test_la_copie_couvre_tous_les_codes_et_les_taches():
    donnees = json.loads(_generateur().SORTIE.read_text(encoding="utf-8"))
    assert set(donnees["secteurs"]) == set(referentiels.secteurs_tpe().par_code())
    assert set(donnees["taches"]) == set(referentiels.taches().par_id())
    assert set(donnees["declencheurs"]) == {d.id for d in referentiels.declencheurs().declencheurs}
    assert all(libelle.strip() for libelle in donnees["secteurs"].values())
    assert donnees["rayon_km"] == referentiels.zone().rayon_km


def test_toute_obligation_a_une_source_https_dans_la_copie():
    donnees = json.loads(_generateur().SORTIE.read_text(encoding="utf-8"))
    assert all(d["source_url"].startswith("https://") for d in donnees["declencheurs"].values())
