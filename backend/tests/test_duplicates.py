"""Unit tests for byte-identical hash grouping."""

from app.services.duplicates import group_by_hash


def test_group_by_hash_groups_identical():
    assert group_by_hash([("a", "H1"), ("b", "H1"), ("c", "H2")]) == [["a", "b"]]


def test_group_by_hash_excludes_singletons_and_empty():
    assert group_by_hash([("a", "H1"), ("b", "H2"), ("c", ""), ("d", None)]) == []


def test_group_by_hash_three_identical():
    assert group_by_hash([("a", "H"), ("b", "H"), ("c", "H")]) == [["a", "b", "c"]]


def test_group_by_hash_multiple_groups_preserve_order():
    groups = group_by_hash([("a", "H1"), ("b", "H1"), ("c", "H2"), ("d", "H2"), ("e", "H3")])
    assert groups == [["a", "b"], ["c", "d"]]
