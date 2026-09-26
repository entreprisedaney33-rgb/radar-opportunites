"""Sous-étape 3.10 (AMELIORATIONS.md), point 5 : disjoncteur Reddit PAR
PASSAGE dans `app.pipeline.orchestrator._collecter` -- 2 réponses 429
consécutives d'un adaptateur de recherche Reddit
(`app.adapters.reddit_recherche.AdaptateurRechercheReddit`) le mettent en
pause pour le reste de CET appel (les adaptateurs Reddit suivants sont
ignorés sans être tentés), journalisé (`ResumeRun.reddit_mis_en_pause`).
Aucun réseau : `AdaptateurRechercheReddit.collecter` est directement
remplacé par un double qui lève `TropDeRequetes` ou renvoie des signaux,
selon le scénario."""
from __future__ import annotations

from app.adapters.http import TropDeRequetes
from app.adapters.reddit_recherche import AdaptateurRechercheReddit
from app.pipeline.orchestrator import ResumeRun, _collecter


def _reddit(sub: str) -> AdaptateurRechercheReddit:
    return AdaptateurRechercheReddit(sub, "manually_en", "manually")


def test_deux_429_consecutifs_mettent_reddit_en_pause_pour_le_reste_du_passage(monkeypatch):
    appels: list[str] = []

    def _toujours_429(self, n, *, engine=None):
        appels.append(self.id_source)
        raise TropDeRequetes("429 persistant")

    monkeypatch.setattr(AdaptateurRechercheReddit, "collecter", _toujours_429)

    a1, a2, a3 = _reddit("smallbusiness"), _reddit("Entrepreneur"), _reddit("freelance")
    resume = ResumeRun()

    resultat = _collecter(None, [(a1, 5, None), (a2, 5, None), (a3, 5, None)], 40, 40, resume)

    assert resultat == []
    # a3 jamais tenté : Reddit déjà en pause après les 2 premiers 429 consécutifs.
    assert appels == [a1.id_source, a2.id_source]
    assert resume.reddit_mis_en_pause is True
    assert resume.sources_indisponibles == [a1.id_source, a2.id_source]


def test_un_seul_429_ne_met_pas_reddit_en_pause(monkeypatch):
    def _toujours_429(self, n, *, engine=None):
        raise TropDeRequetes("429 persistant")

    monkeypatch.setattr(AdaptateurRechercheReddit, "collecter", _toujours_429)

    a1 = _reddit("smallbusiness")
    resume = ResumeRun()

    _collecter(None, [(a1, 5, None)], 40, 40, resume)

    assert resume.reddit_mis_en_pause is False


def test_un_succes_entre_deux_remet_le_compteur_de_429_a_zero(monkeypatch):
    """2 429, PAS consécutifs (un succès entre les deux) : Reddit ne doit
    jamais être mis en pause -- le compteur se remet à zéro à chaque
    réponse Reddit qui réussit."""
    sequence = iter([
        (TropDeRequetes, None),
        (None, []),  # succès (liste vide de signaux, peu importe)
        (TropDeRequetes, None),
    ])

    def _sequence(self, n, *, engine=None):
        exc, valeur = next(sequence)
        if exc is not None:
            raise exc("429 persistant")
        return valeur

    monkeypatch.setattr(AdaptateurRechercheReddit, "collecter", _sequence)

    a1, a2, a3 = _reddit("smallbusiness"), _reddit("Entrepreneur"), _reddit("freelance")
    resume = ResumeRun()

    _collecter(None, [(a1, 5, None), (a2, 5, None), (a3, 5, None)], 40, 40, resume)

    assert resume.reddit_mis_en_pause is False
    assert resume.sources_indisponibles == [a1.id_source, a3.id_source]


def test_429_sur_un_adaptateur_non_reddit_n_active_jamais_le_disjoncteur(monkeypatch):
    """Le disjoncteur ne compte que les 429 d'adaptateurs Reddit -- une autre
    source qui échouerait de la même façon (générique, `Exception`) ne doit
    jamais mettre Reddit en pause ni compter dans son compteur."""

    class _AutreSourceEnPanne:
        id_source = "autre_source"

        def collecter(self, n, *, engine=None):
            raise RuntimeError("panne générique, sans rapport avec Reddit")

    def _un_seul_429_puis_ok(self, n, *, engine=None):
        raise TropDeRequetes("429 persistant")

    monkeypatch.setattr(AdaptateurRechercheReddit, "collecter", _un_seul_429_puis_ok)

    autre = _AutreSourceEnPanne()
    a1 = _reddit("smallbusiness")
    resume = ResumeRun()

    _collecter(None, [(autre, 5, None), (a1, 5, None)], 40, 40, resume)

    assert resume.reddit_mis_en_pause is False
    assert resume.sources_indisponibles == ["autre_source", a1.id_source]
