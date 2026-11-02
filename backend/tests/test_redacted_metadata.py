"""Which email header fields a redaction covers (drives load-file blanking
and the attachment-names conflict)."""

from types import SimpleNamespace

from app.services.redacted_metadata import blanked_fields

REGIONS = {
    "from": {"page": 1, "x": 10.0, "y": 8.0, "w": 80.0, "h": 2.0},
    "subject": {"page": 1, "x": 10.0, "y": 14.0, "w": 80.0, "h": 2.0},
    "attachments": {"page": 1, "x": 10.0, "y": 17.0, "w": 80.0, "h": 2.0},
}


def _red(page=1, x=0.0, y=0.0, w=10.0, h=10.0):
    return SimpleNamespace(page_num=page, x_pct=x, y_pct=y, w_pct=w, h_pct=h)


def test_overlapping_box_blanks_its_field():
    assert blanked_fields(REGIONS, [_red(x=40, y=14.5, w=20, h=1)]) == {"subject"}


def test_box_only_touching_an_edge_blanks_nothing():
    assert blanked_fields(REGIONS, [_red(x=40, y=16.0, w=20, h=1.0)]) == set()


def test_box_on_another_page_blanks_nothing():
    assert blanked_fields(REGIONS, [_red(page=2, x=40, y=14.5, w=20, h=1)]) == set()


def test_tall_box_blanks_every_line_it_crosses():
    assert blanked_fields(REGIONS, [_red(x=0, y=7, w=100, h=12)]) == {"from", "subject", "attachments"}


def test_no_regions():
    assert blanked_fields(None, [_red()]) == set()


def test_field_spanning_pages_blanks_when_any_part_is_covered():
    regions = {"to": [{"page": 1, "x": 10.0, "y": 80.0, "w": 80.0, "h": 15.0},
                      {"page": 2, "x": 10.0, "y": 5.0, "w": 80.0, "h": 10.0}]}
    assert blanked_fields(regions, [_red(page=2, x=20, y=6, w=10, h=2)]) == {"to"}
    assert blanked_fields(regions, [_red(page=3, x=20, y=6, w=10, h=2)]) == set()
