"""Unit 8: unpack once. The explode walk over a fake pypff tree, the object
layout, the Job trigger and the error-attachment document. No GCP calls."""

import json
import os

import pytest

from app.services import pst_explode, pst_reader, storage
from app.services.email_parse import OversizedAttachment, UnreadableAttachment
from tests.test_pst_reader import Att, Folder, Item

PST = "productions/1/raw/acme.pst"
JOB = "11111111-2222-3333-4444-555555555555"


class Store(dict):
    """In-memory object store: put(path, data, content_type)."""

    def __init__(self):
        super().__init__()
        self.types = {}

    def put(self, path, data, content_type=None):
        self[path] = bytes(data)
        self.types[path] = content_type

    def get(self, path):
        return self[path]


def _tree(inbox_items, cal_items=()):
    # Folder indices: 0 "", 1 "Top", 2 "Top/Inbox", 3 "Top/Calendar".
    return Folder("", subs=[Folder("Top", subs=[
        Folder("Inbox", list(inbox_items)), Folder("Calendar", list(cal_items)),
    ])])


PREFIX = pst_explode.explode_prefix(1, JOB, PST)


def _explode(tree, **kw):
    store = Store()
    result = pst_explode.explode(tree, PREFIX, store.put, retry_sleep=0, **kw)
    return store, result


def _json(store, path):
    return json.loads(store[path])


def test_prefix_layout():
    key = pst_explode.container_key(PST)
    assert len(key) == 12
    assert PREFIX == f"productions/1/exploded/{JOB}/{key}"
    assert pst_explode.job_explode_prefix(1, JOB) == f"productions/1/exploded/{JOB}/"
    assert pst_explode.item_object(PREFIX, 2, 7) == f"{PREFIX}/00002/0000007.json"
    assert pst_explode.attachment_object(PREFIX, 2, 7, 1) == f"{PREFIX}/00002/0000007.att1"
    assert pst_explode.manifest_object(PREFIX, 2) == f"{PREFIX}/00002/_manifest.json"


def test_explode_writes_one_json_per_item_plus_manifests_keyed_by_position():
    tree = _tree([Item(subject="a"), Item("IPM.Contact", subject="c"), Item(subject="b")],
                 [Item("IPM.Appointment", subject="k")])
    store, result = _explode(tree)
    items = sorted(p for p in store if p.endswith(".json") and "_manifest" not in p)
    assert items == [f"{PREFIX}/00002/000000{i}.json" for i in range(3)] + [f"{PREFIX}/00003/0000000.json"]
    assert sorted(p for p in store if "_manifest" in p) == [
        f"{PREFIX}/0000{i}/_manifest.json" for i in range(4)]
    m = _json(store, f"{PREFIX}/00002/_manifest.json")
    assert m["folder_path"] == "Top/Inbox"
    assert m["positions"] == {
        "0": {"kind": "email", "ok": True, "error": None},
        "1": {"kind": "contact", "ok": True, "error": None},
        "2": {"kind": "email", "ok": True, "error": None},
    }
    doc = _json(store, f"{PREFIX}/00002/0000001.json")
    assert doc["kind"] == "contact" and doc["folder_path"] == "Top/Inbox"
    assert doc["parsed"]["subject"] == "c" and doc["read_errors"] == []


def test_explode_survey_matches_the_plain_survey():
    tree = _tree([Item(size=10), Item("IPM.Contact", size=5), Item(size=1)],
                 [Item("IPM.Appointment", size=7)])
    _, result = _explode(tree)
    plain = pst_reader.survey(tree)
    assert result.to_json()["folders"] == plain.to_json()["folders"]


def test_attachments_are_sibling_objects_not_inlined():
    item = Item(subject="s", html_body='<img src="cid:logo1">',
                attachments=[Att("notes.txt", b"hello"), Att("logo.png", b"\x89PNG", "<logo1>")])
    store, _ = _explode(_tree([item]))
    doc = _json(store, f"{PREFIX}/00002/0000000.json")
    assert doc["attachments"] == [
        {"name": "notes.txt", "inline": False, "object": f"{PREFIX}/00002/0000000.att1", "error": None},
        {"name": "logo.png", "inline": True, "object": f"{PREFIX}/00002/0000000.att2", "error": None},
    ]
    assert store[f"{PREFIX}/00002/0000000.att1"] == b"hello"
    assert "hello" not in store[f"{PREFIX}/00002/0000000.json"].decode()
    assert doc["parsed"]["inline_names"] == ["logo.png"]


def test_partially_readable_item_keeps_readable_fields_and_records_the_field():
    from tests.test_pst_reader import _item_with

    cls = _item_with(subject=OSError("unable to read subject"))
    store, _ = _explode(_tree([cls(plain_text_body="kept")]))
    doc = _json(store, f"{PREFIX}/00002/0000000.json")
    assert doc["parsed"]["body_text"] == "kept"
    (err,) = doc["read_errors"]
    assert err.startswith("subject: OSError")
    entry = _json(store, f"{PREFIX}/00002/_manifest.json")["positions"]["0"]
    assert entry["ok"] is True and "unable to read subject" in entry["error"]


def test_unreadable_and_oversized_attachments_are_recorded_with_no_object(monkeypatch):
    from tests.test_pst_reader import SizeFails

    monkeypatch.setattr(pst_reader, "MAX_ATTACHMENT_BYTES", 3)
    item = Item(subject="s", attachments=[SizeFails("bad.bin", b"", method=7), Att("big.zip", b"xxxx")])
    store, _ = _explode(_tree([item]))
    bad, big = _json(store, f"{PREFIX}/00002/0000000.json")["attachments"]
    assert bad["object"] is None and "failed to retrieve data size" in bad["error"]
    assert big["object"] is None and big["oversized"] == {"size": 4, "limit": 3}
    assert not any(p.endswith((".att1", ".att2")) for p in store)


def test_method5_attachment_is_stored_as_an_embedded_eml():
    from app.services.email_parse import _parse_eml_bytes
    from tests.test_pst_reader import Embedded

    inner = Item(subject="Inner", plain_text_body="inner body",
                 transport_headers="From: bob@acme.test\r\n")
    store, _ = _explode(_tree([Item(subject="Outer", attachments=[Embedded("fwd", inner)])]))
    (att,) = _json(store, f"{PREFIX}/00002/0000000.json")["attachments"]
    assert att["name"] == "Inner.eml"
    nested = _parse_eml_bytes(store[att["object"]])
    assert nested.subject == "Inner" and "inner body" in nested.body_text


def test_wholly_unreadable_item_is_in_the_manifest_only(monkeypatch):
    real = pst_reader.to_parsed

    def broken(item, kind, _depth=0):
        if getattr(item, "subject", "") == "bad":
            raise OSError("item table corrupt")
        return real(item, kind, _depth)

    monkeypatch.setattr(pst_reader, "to_parsed", broken)
    store, result = _explode(_tree([Item(subject="good"), Item(subject="bad")]))
    assert f"{PREFIX}/00002/0000001.json" not in store
    entry = _json(store, f"{PREFIX}/00002/_manifest.json")["positions"]["1"]
    assert entry["ok"] is False and entry["kind"] == "email"
    assert "item table corrupt" in entry["error"]
    # Still counted at its position, so plans and controls do not shift.
    assert result.folders[2].counts["email"] == 2


def test_item_that_cannot_be_opened_is_recorded_at_its_position():
    class BadFolder(Folder):
        def get_sub_message(self, i):
            if i == 1:
                raise OSError("unable to open item")
            return super().get_sub_message(i)

    tree = Folder("", subs=[BadFolder("Inbox", [Item(subject="a"), Item(), Item(subject="c")])])
    store, result = _explode(tree)
    entry = _json(store, f"{PREFIX}/00001/_manifest.json")["positions"]["1"]
    assert entry == {"kind": "other", "ok": False, "error": "item could not be opened: OSError: unable to open item"}
    assert result.folders[1].runs == [["email", 1], ["other", 1], ["email", 1]]


def test_upload_is_retried_then_a_persistent_failure_fails_the_explode():
    calls = {"n": 0}
    store = Store()

    def flaky(path, data, content_type=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("503 backend error")
        store.put(path, data, content_type)

    pst_explode.explode(_tree([Item(subject="a")]), PREFIX, flaky, retry_sleep=0)
    assert f"{PREFIX}/00002/0000000.json" in store

    def down(path, data, content_type=None):
        raise OSError("storage down")

    with pytest.raises(OSError, match="storage down"):
        pst_explode.explode(_tree([Item(subject="a")]), PREFIX, down, retry_sleep=0)


def test_unpack_container_downloads_explodes_and_removes_the_copy(monkeypatch, tmp_path):
    import contextlib

    tree = _tree([Item(subject="a")])
    downloaded, opened = [], []

    def fake_download(remote, local):
        downloaded.append((remote, local))
        open(local, "wb").write(b"PST")

    @contextlib.contextmanager
    def fake_open(local, storage_path=""):
        opened.append((local, storage_path))
        yield tree

    store = Store()
    monkeypatch.setattr(storage, "download_sliced", fake_download)
    monkeypatch.setattr(pst_reader, "open_pst", fake_open)
    monkeypatch.setattr(storage, "upload_bytes", lambda data, path, content_type=None: store.put(path, data))
    out = pst_explode.unpack_container(1, JOB, PST, tmp_dir=str(tmp_path))
    ((remote, local),) = downloaded
    assert remote == PST and opened == [(local, PST)]
    assert not os.path.exists(local)
    assert out["storage_path"] == PST and out["folders"][2]["counts"]["email"] == 1
    assert f"{PREFIX}/00002/0000000.json" in store


def test_download_sliced_uses_threaded_chunked_download(monkeypatch, tmp_path):
    import types

    calls = []

    class Blob:
        size = None

        def reload(self):
            self.size = 10

    blob = Blob()
    bucket = types.SimpleNamespace(blob=lambda p: blob)
    monkeypatch.setattr(storage, "get_bucket", lambda: bucket)
    import google.cloud.storage.transfer_manager as tm

    monkeypatch.setattr(tm, "download_chunks_concurrently",
                        lambda b, filename, **kw: calls.append((b, filename, kw)))
    storage.download_sliced("productions/1/raw/acme.pst", str(tmp_path / "x.pst"))
    ((b, filename, kw),) = calls
    assert b is blob and blob.size == 10
    assert kw["worker_type"] == tm.THREAD and kw["chunk_size"] == storage.SLICED_CHUNK_BYTES


# --- trigger -----------------------------------------------------------------


class FakeResp:
    def __init__(self, status=200, text="{}"):
        self.status_code = status
        self.text = text


def test_trigger_runs_the_job_with_arg_overrides(monkeypatch):
    from app.config import settings
    from app.services import tasks

    posts = []

    class Session:
        def post(self, url, json=None, timeout=None):
            posts.append((url, json))
            return FakeResp()

    monkeypatch.setattr(tasks, "is_configured", lambda: True)
    monkeypatch.setattr(settings, "gcp_project_id", "proj")
    monkeypatch.setattr(settings, "gcp_location", "us-central1")
    monkeypatch.setattr(pst_explode, "_authorized_session", lambda: Session())
    pst_explode.trigger_explode_job(JOB, PST)
    ((url, body),) = posts
    assert url == ("https://run.googleapis.com/v2/projects/proj/locations/us-central1/"
                   "jobs/vigilist-pst-explode:run")
    (override,) = body["overrides"]["containerOverrides"]
    assert override["args"] == ["-m", "app.jobs.explode_pst", "--job-id", JOB, "--storage-path", PST]
    assert body["overrides"]["taskCount"] == 1


def test_trigger_raises_on_an_admin_api_error(monkeypatch):
    from app.services import tasks

    class Session:
        def post(self, url, json=None, timeout=None):
            return FakeResp(403, '{"error": "permission denied"}')

    monkeypatch.setattr(tasks, "is_configured", lambda: True)
    monkeypatch.setattr(pst_explode, "_authorized_session", lambda: Session())
    with pytest.raises(RuntimeError, match="403"):
        pst_explode.trigger_explode_job(JOB, PST)


def test_trigger_requires_cloud_tasks(monkeypatch):
    from app.services import tasks

    monkeypatch.setattr(tasks, "is_configured", lambda: False)
    with pytest.raises(RuntimeError, match="requires Cloud Tasks"):
        pst_explode.trigger_explode_job(JOB, PST)


# --- documents for unreadable attachments; .eml attachment text --------------


def test_unreadable_attachment_becomes_an_error_document():
    from app.services.email_parse import ParsedMessage
    from app.services.extractors import ExtractResult
    from app.services.ingest_native import build_email_documents

    parsed = ParsedMessage(subject="s", attachments=[
        ("ok.txt", b"hello"), ("bad.bin", UnreadableAttachment("attachment could not be read: OSError: x"))])
    docs = build_email_documents(parsed, "ACME 000001 -0001", 1, "acme.pst", None, b"m",
                                 extract_fn=lambda n, d, ocr_fn=None: ExtractResult("hello", "text", "ok"),
                                 store_fn=lambda d, p: None)
    bad = docs[2]
    assert bad.bates_begin == "ACME 000001 -0001 .0002" and bad.file_name == "bad.bin"
    assert bad.extraction_status == "error" and bad.native_path is None
    assert "could not be read" in bad.extraction_error
    assert bad.family_id == "ACME 000001 -0001"


def test_eml_attachment_gets_text():
    from app.services.extractors import extract_attachment
    from app.services.pst_reader import to_eml_bytes
    from app.services.email_parse import ParsedMessage

    data = to_eml_bytes(ParsedMessage(from_="bob@acme.test", subject="Inner", body_text="inner body"))
    res = extract_attachment("Inner.eml", data, inline=False, ocr_fn=None)
    assert res.extraction_status == "ok"
    assert "Inner" in res.text and "inner body" in res.text and "bob@acme.test" in res.text


def test_markers_are_bytes():
    assert bytes(OversizedAttachment(5, 3)) == b"" and bytes(UnreadableAttachment("x")) == b""


def test_get_bucket_initializes_firebase_when_no_app_exists(monkeypatch):
    """The explode Job never imports the auth router (which initializes the
    Firebase app for the API), so Storage access must initialize it."""
    import firebase_admin
    from firebase_admin import storage as fb_storage

    inits = []
    monkeypatch.setattr(firebase_admin, "_apps", {})
    monkeypatch.setattr(firebase_admin, "initialize_app", lambda *a, **k: inits.append(k) or object())
    monkeypatch.setattr(fb_storage, "bucket", lambda name: ("bucket", name))
    assert storage.get_bucket()[0] == "bucket"
    assert len(inits) == 1
