"""Unit tests for the pure redaction validation service (P1-1). No DB/network."""

from app.services.redaction import (
    REDACTION_REASON_CODES,
    is_valid_reason_code,
    validate_rect,
)


def test_reason_codes_are_the_defined_set():
    assert REDACTION_REASON_CODES == frozenset({
        "attorney_client", "work_product", "pii", "phi",
        "confidential", "trade_secret", "non_responsive", "other",
    })


def test_is_valid_reason_code():
    assert is_valid_reason_code("attorney_client") is True
    assert is_valid_reason_code("pii") is True
    assert is_valid_reason_code("bogus") is False
    assert is_valid_reason_code("") is False


def test_validate_rect_accepts_a_valid_box():
    assert validate_rect(1, 10.0, 20.0, 30.0, 40.0, page_count=5) is None
    # Exactly filling the page is allowed.
    assert validate_rect(2, 0.0, 0.0, 100.0, 100.0, page_count=5) is None


def test_validate_rect_rejects_bad_page_num():
    assert validate_rect(0, 10.0, 10.0, 10.0, 10.0, page_count=5) is not None
    assert validate_rect(6, 10.0, 10.0, 10.0, 10.0, page_count=5) is not None


def test_validate_rect_rejects_out_of_range_origin():
    assert validate_rect(1, -1.0, 10.0, 10.0, 10.0, page_count=5) is not None
    assert validate_rect(1, 10.0, 101.0, 10.0, 10.0, page_count=5) is not None


def test_validate_rect_rejects_nonpositive_size():
    assert validate_rect(1, 10.0, 10.0, 0.0, 10.0, page_count=5) is not None
    assert validate_rect(1, 10.0, 10.0, 10.0, -5.0, page_count=5) is not None


def test_validate_rect_rejects_box_exceeding_page():
    assert validate_rect(1, 80.0, 10.0, 30.0, 10.0, page_count=5) is not None  # x+w=110
    assert validate_rect(1, 10.0, 95.0, 10.0, 20.0, page_count=5) is not None  # y+h=115
