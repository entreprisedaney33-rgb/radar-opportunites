import pytest


@pytest.fixture
def client_web(tmp_path, monkeypatch):
    chemin = tmp_path / "web_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{chemin}")
    monkeypatch.setenv("RADAR_UI_PASSWORD", "secret-test")

    from app import config as cfg
    from app.storage import db as dbmod

    cfg.get_settings.cache_clear()
    dbmod.get_engine.cache_clear()
    dbmod.migrer(dbmod.get_engine())

    from app.web.server import app

    with app.test_client() as client:
        yield client

    cfg.get_settings.cache_clear()
    dbmod.get_engine.cache_clear()


def test_accueil_refuse_sans_authentification(client_web):
    resp = client_web.get("/")
    assert resp.status_code == 401


def test_accueil_accepte_avec_bon_mot_de_passe(client_web):
    resp = client_web.get("/", headers={"Authorization": "Basic " + _basic("peu-importe", "secret-test")})
    assert resp.status_code == 200
    assert "runs" in resp.get_data(as_text=True).lower()


def test_pause_bascule_via_le_bouton(client_web):
    from app.storage.db import get_engine
    from app.storage import repo

    auth = {"Authorization": "Basic " + _basic("x", "secret-test")}
    assert repo.lire_pause_all(get_engine()) is False
    resp = client_web.post("/pause", data={"valeur": "1"}, headers=auth)
    assert resp.status_code == 302
    assert repo.lire_pause_all(get_engine()) is True


def _basic(user: str, password: str) -> str:
    import base64

    return base64.b64encode(f"{user}:{password}".encode()).decode()
