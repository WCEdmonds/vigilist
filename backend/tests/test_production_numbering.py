"""Pure tests for production-set ordering + Bates numbering (P2-1). No DB."""

from datetime import datetime, timezone

from app.services.production_numbering import (
    SORT_KEYS,
    MemberInfo,
    assign_bates,
    format_bates,
    order_members,
    pages_for,
)

_T = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _m(cn, family_id=None, custodian=None, doc_date=None):
    return MemberInfo(document_id=cn, control_number=cn, family_id=family_id,
                      custodian=custodian, doc_date=doc_date)


# --- format_bates -----------------------------------------------------------

def test_format_bates_pads():
    assert format_bates("SMITH", 1, 6) == "SMITH000001"
    assert format_bates("SMITH", 999999, 6) == "SMITH999999"


def test_format_bates_overflow_grows_never_truncates():
    assert format_bates("SMITH", 1000000, 6) == "SMITH1000000"


def test_sort_keys_constant():
    assert SORT_KEYS == {"control_number", "custodian_date"}


# --- pages_for --------------------------------------------------------------

def test_pages_for_withhold_is_one_slipsheet_page():
    assert pages_for("withhold", 10) == 1


def test_pages_for_other_dispositions_use_page_count():
    assert pages_for("redact_in_part", 10) == 10
    assert pages_for("produce", 10) == 10


def test_pages_for_floors_at_one():
    assert pages_for("produce", 0) == 1


# --- order_members ----------------------------------------------------------

def test_order_control_number():
    ms = [_m("C-3"), _m("C-1"), _m("C-2")]
    out = [m.control_number for m in order_members(ms, "control_number")]
    assert out == ["C-1", "C-2", "C-3"]


def test_order_families_contiguous_parent_first():
    # C-1 and C-5 share a family; the lower control number (the parent,
    # ingested first) heads the group, and C-5 rides with it ahead of C-3.
    ms = [_m("C-5", family_id="F1"), _m("C-1", family_id="F1"), _m("C-3")]
    out = [m.control_number for m in order_members(ms, "control_number")]
    assert out == ["C-1", "C-5", "C-3"]


def test_order_custodian_date():
    a = _m("C-2", custodian="Alice", doc_date=_T)
    b = _m("C-1", custodian="Bob", doc_date=_T)
    c = _m("C-3", custodian="Alice",
           doc_date=datetime(2026, 6, 1, tzinfo=timezone.utc))
    out = [m.control_number for m in order_members([a, b, c], "custodian_date")]
    assert out == ["C-3", "C-2", "C-1"]


def test_order_custodian_date_missing_fields_deterministic():
    # Missing custodian sorts first (empty string); missing date sorts after
    # dated docs for the same custodian; control number breaks all ties.
    a = _m("C-1", custodian=None, doc_date=None)
    b = _m("C-2", custodian="Alice", doc_date=None)
    c = _m("C-4", custodian="Alice", doc_date=_T)
    out = [m.control_number for m in order_members([a, b, c], "custodian_date")]
    assert out == ["C-1", "C-4", "C-2"]


def test_order_unknown_sort_key_raises():
    try:
        order_members([_m("C-1")], "bogus")
        assert False, "expected ValueError"
    except ValueError:
        pass


# --- assign_bates -----------------------------------------------------------

def test_assign_bates_gap_free_across_mixed_page_counts():
    out = assign_bates([("a", 3), ("b", 1), ("c", 2)], "SMITH", 6, 1)
    assert out == [
        ("a", 1, "SMITH000001", "SMITH000003"),
        ("b", 2, "SMITH000004", "SMITH000004"),
        ("c", 3, "SMITH000005", "SMITH000006"),
    ]


def test_assign_bates_start_number_offset():
    out = assign_bates([("a", 2)], "VOL", 4, 100)
    assert out == [("a", 1, "VOL0100", "VOL0101")]
