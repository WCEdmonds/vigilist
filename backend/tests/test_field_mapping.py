"""Unit tests for alias-based column mapping (no DB, no network)."""

from app.services import field_mapping as fm


def test_canonical_fields_include_metadata_and_structural():
    for f in ["bates_begin", "custodian", "date_sent", "file_hash_md5",
              "email_to", "file_name", "source_path"]:
        assert f in fm.CANONICAL_FIELDS


def test_match_aliases_is_insensitive_to_case_space_underscore():
    headers = ["BEGDOC", "Cust", "Date Sent", "MD5 Hash", "Email_To", "FileName"]
    m = fm.match_aliases(headers)
    assert m["bates_begin"] == "BEGDOC"
    assert m["custodian"] == "Cust"
    assert m["date_sent"] == "Date Sent"
    assert m["file_hash_md5"] == "MD5 Hash"
    assert m["email_to"] == "Email_To"
    assert m["file_name"] == "FileName"


def test_match_aliases_ignores_unknown_headers():
    m = fm.match_aliases(["Wingding", "Custodian"])
    assert m == {"custodian": "Custodian"}


def test_match_aliases_first_wins_on_duplicate_target():
    m = fm.match_aliases(["Begin Bates", "BEGDOC"])
    assert m["bates_begin"] == "Begin Bates"
