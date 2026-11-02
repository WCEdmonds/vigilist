"""Produced text for redacted documents: the words under boxes are removed."""

from types import SimpleNamespace

from app.services.redacted_text import (
    REDACTED,
    REDACTED_PAGE,
    covered_fraction,
    page_text,
    produced_text,
)


def _w(t, x, y=10.0, w=8.0, h=2.0):
    return {"t": t, "x": x, "y": y, "w": w, "h": h, "c": 1.0}


def _r(x, y, w, h, page=1):
    return SimpleNamespace(page_num=page, x_pct=x, y_pct=y, w_pct=w, h_pct=h)


LINE = [_w("Pay", 5), _w("account", 15), _w("4417", 25), _w("1234", 35), _w("today", 45)]


def test_word_under_box_is_removed_and_runs_collapse():
    assert page_text(LINE, [_r(24, 9, 20, 4)]) == f"Pay account {REDACTED} today"


def test_threshold():
    word = _w("4417", 20, w=10.0)
    assert covered_fraction(word, _r(20, 9, 2.5, 4)) == 0.25
    assert page_text([word], [_r(20, 9, 2.5, 4)]) == "4417"
    assert page_text([word], [_r(20, 9, 3.5, 4)]) == REDACTED


def test_partial_line_box_removes_only_covered_words():
    assert page_text(LINE, [_r(14, 9, 10, 4)]) == f"Pay {REDACTED} 4417 1234 today"


def test_lines_group_by_vertical_position():
    words = [_w("second", 5, y=20.1), _w("first", 5, y=10.0), _w("line", 15, y=10.3), _w("too", 18, y=20.0)]
    assert page_text(words, []) == "first line\nsecond too"


def test_whole_page_box():
    assert page_text(LINE, [_r(0, 0, 100, 100)]) == REDACTED_PAGE


def test_produced_text_joins_pages_and_applies_per_page_boxes():
    sidecars = [{"words": LINE}, {"words": [_w("Page", 5), _w("two", 15)]}]
    out = produced_text(sidecars, {2: [_r(14, 9, 10, 4, page=2)]})
    assert out == f"Pay account 4417 1234 today\fPage {REDACTED}"


def test_missing_sidecar_withholds_all_text():
    assert produced_text([{"words": LINE}, None], {}) is None
