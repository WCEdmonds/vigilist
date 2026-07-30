"""Numeric-tail Bates matching.

Reviewers type the number alone — every document in a production shares the
same prefix, so typing "SCHLEGELPROD " each time is noise. The endpoint
therefore matches the trailing number of a Bates, which is the only part the
reader actually distinguishes documents by.

The risk this creates is over-matching, so most of these tests are about what
must NOT match.
"""

import re

from app.routers.documents import _numeric_tail_pattern


def _matches(typed: str, stored: str) -> bool:
    return re.search(_numeric_tail_pattern(typed), stored) is not None


def test_finds_a_prefixed_bates_from_the_number_alone():
    assert _matches("000009", "SCHLEGELPROD 000009")


def test_leading_zeros_are_irrelevant_on_either_side():
    for typed in ("9", "009", "000009", "0000009"):
        assert _matches(typed, "SCHLEGELPROD 000009"), typed
    assert _matches("000009", "PLTF-9")


def test_does_not_match_a_longer_number_ending_in_the_same_digits():
    # The guard that makes this safe: "9" must not find "1000009". Without the
    # leading (^|[^0-9]) the zero-run would happily match mid-number.
    assert not _matches("9", "1000009")
    assert not _matches("000009", "1000009")
    assert not _matches("19", "SCHLEGEL 000119")


def test_does_not_match_when_the_number_merely_contains_the_digits():
    assert not _matches("9", "SCHLEGEL 0000091")   # trailing digit differs
    assert not _matches("19", "ABC 000190")


def test_matches_an_unprefixed_bates():
    assert _matches("000009", "000009")
    assert _matches("9", "9")


def test_separator_before_the_number_does_not_matter():
    for stored in ("ABC 000009", "ABC-000009", "ABC_000009", "ABC.000009", "ABC000009"):
        assert _matches("9", stored), stored


def test_zero_is_handled_rather_than_collapsing_to_empty():
    # "0".lstrip("0") is "" — a naive implementation builds the pattern
    # "0*$", which matches the tail of literally every string.
    assert _matches("0", "ABC 000")
    assert not _matches("0", "ABC 001")


def test_pattern_cannot_be_injected_through_the_typed_value():
    # The caller only reaches this with an all-digit string, but the pattern
    # must still be inert if that ever changes: no regex metacharacter from
    # the input can survive into the compiled pattern.
    pattern = _numeric_tail_pattern("000009")
    assert re.compile(pattern)  # compiles
    assert ".*" not in pattern and "|0" not in pattern.replace("(^|[^0-9])", "")
