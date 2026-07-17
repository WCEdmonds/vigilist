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


def test_build_proposed_mapping_alias_and_unmapped_without_ai():
    headers = ["Custodian", "Widget Code"]
    rows = [{"Custodian": "Smith", "Widget Code": "X1"}]
    proposed = fm.build_proposed_mapping(headers, rows, use_ai=False)
    by_name = {p["source_name"]: p for p in proposed}
    assert by_name["Custodian"]["target"] == "custodian"
    assert by_name["Custodian"]["source"] == "alias"
    assert by_name["Custodian"]["confidence"] == 1.0
    assert by_name["Widget Code"]["target"] is None
    assert by_name["Widget Code"]["source"] == "unmapped"
    assert by_name["Custodian"]["samples"] == ["Smith"]


def test_propose_ai_mapping_uses_client(monkeypatch):
    captured = {}

    class _FakeContent:
        def __init__(self, data):
            self.text = data

    class _FakeMsg:
        def __init__(self, data):
            self.content = [_FakeContent(data)]

    class _FakeMessages:
        def create(self, **kwargs):
            captured["prompt"] = kwargs
            import json
            return _FakeMsg(json.dumps({"Widget Code": "file_type"}))

    class _FakeClient:
        messages = _FakeMessages()

    out = fm.propose_ai_mapping([{"name": "Widget Code", "samples": ["X1"]}], client=_FakeClient())
    assert out == {"Widget Code": "file_type"}


def test_propose_ai_mapping_falls_back_to_empty_on_error(monkeypatch):
    monkeypatch.setattr("app.services.field_mapping._default_client", lambda: None)
    assert fm.propose_ai_mapping([{"name": "X", "samples": ["1"]}]) == {}


def test_propose_ai_mapping_exception_client_returns_empty():
    class _BoomClient:
        class messages:
            @staticmethod
            def create(**kwargs):
                raise RuntimeError("no api key")
    assert fm.propose_ai_mapping([{"name": "X", "samples": ["1"]}], client=_BoomClient()) == {}


def test_propose_ai_mapping_filters_invalid_targets():
    import json

    class _FakeContent:
        def __init__(self, txt):
            self.text = txt

    class _FakeMsg:
        def __init__(self, txt):
            self.content = [_FakeContent(txt)]

    class _FakeMessages:
        def create(self, **kwargs):
            return _FakeMsg(json.dumps({"Col A": "hallucinated_field", "Col B": None}))

    class _FakeClient:
        messages = _FakeMessages()

    out = fm.propose_ai_mapping(
        [{"name": "Col A", "samples": []}, {"name": "Col B", "samples": []}],
        client=_FakeClient(),
    )
    assert out == {}


def test_match_aliases_family_thread_inclusive():
    m = fm.match_aliases(["Group Identifier", "Conversation Index", "Inclusive Email"])
    assert m["family_id"] == "Group Identifier"
    assert m["thread_id"] == "Conversation Index"
    assert m["is_inclusive"] == "Inclusive Email"


def test_build_proposed_mapping_ai_path(monkeypatch):
    import json

    class _FakeContent:
        def __init__(self, txt):
            self.text = txt

    class _FakeMsg:
        def __init__(self, txt):
            self.content = [_FakeContent(txt)]

    class _FakeMessages:
        def create(self, **kwargs):
            return _FakeMsg(json.dumps({"Widget Code": "file_type"}))

    class _FakeClient:
        messages = _FakeMessages()

    monkeypatch.setattr("app.services.field_mapping._default_client", lambda: _FakeClient())

    headers = ["Custodian", "Widget Code"]
    rows = [{"Custodian": "Smith", "Widget Code": "application/pdf"}]
    proposed = fm.build_proposed_mapping(headers, rows, use_ai=True)
    by_name = {p["source_name"]: p for p in proposed}

    assert by_name["Custodian"]["source"] == "alias"
    assert by_name["Custodian"]["target"] == "custodian"
    assert by_name["Widget Code"]["source"] == "ai"
    assert by_name["Widget Code"]["target"] == "file_type"
