from app.routers.intelligence import clamp_limit


def test_clamp_limit_bounds():
    assert clamp_limit(5) == 5
    assert clamp_limit(0) == 1
    assert clamp_limit(-3) == 1
    assert clamp_limit(999) == 20
