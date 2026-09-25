from app.storage import repo


def test_pause_all_defaut_faux_puis_bascule(engine_test):
    assert repo.lire_pause_all(engine_test) is False
    repo.definir_pause_all(engine_test, True)
    assert repo.lire_pause_all(engine_test) is True
    repo.definir_pause_all(engine_test, False)
    assert repo.lire_pause_all(engine_test) is False
