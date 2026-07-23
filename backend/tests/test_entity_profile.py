from app.services.entity_profile import is_overview_stale


class E:
    def __init__(self, overview=None, gen_count=None, count=0):
        self.overview = overview
        self.overview_mention_count = gen_count
        self.mention_count = count


def test_no_overview_is_stale():
    assert is_overview_stale(E(overview=None, count=3))


def test_fresh_overview_not_stale():
    assert not is_overview_stale(E(overview="x", gen_count=10, count=12))


def test_growth_by_ratio_is_stale():
    assert is_overview_stale(E(overview="x", gen_count=10, count=15))  # 1.5x


def test_growth_by_absolute_is_stale():
    assert is_overview_stale(E(overview="x", gen_count=100, count=110))  # +10
