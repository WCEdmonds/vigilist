"""Unit 8: chunk-side reader over exploded objects (fake Storage)."""

from datetime import datetime

import pytest

from app.services import exploded_reader, pst_explode, pst_reader, storage
from app.services.email_parse import OversizedAttachment, UnreadableAttachment
from tests.test_pst_explode import JOB, PST, Store
from tests.test_pst_reader import Att, BareItem, Embedded, Folder, Item, SizeFails, _item_with

PID = 1


def _tree(items, other=()):
    # Folder indices: 0 "", 1 "Top", 2 "Top/Inbox", 3 "Top/Deleted Items".
    return Folder("", subs=[Folder("Top", subs=[
        Folder("Inbox", list(items)), Folder("Deleted Items", list(other))])])


@pytest.fixture
def store(monkeypatch):
    s = Store()
    s.reads = []

    def get(path):
        s.reads.append(path)
        if path not in s:
            raise FileNotFoundError(path)
        return s[path]

    monkeypatch.setattr(storage, "get_download_bytes", get)
    return s


def _explode(store, tree):
    pst_explode.explode(tree, pst_explode.explode_prefix(PID, JOB, PST), store.put, retry_sleep=0)


def _items(slices):
    return list(exploded_reader.iter_slice(JOB, PID, PST, slices))


SAMPLES = [
    Item(subject="Q3 numbers", plain_text_body="see attached",
         html_body='<p>hi <img src="cid:logo1"></p>',
         transport_headers="From: alice@acme.test\r\nMessage-ID: <m1@acme.test>\r\nSubject: x\r\n"
                           "References: <a@acme.test>\r\n  <b@acme.test>\r\n",
         attachments=[Att("numbers.csv", b"a,b\n"), Att("logo.png", b"\x89PNG", "<logo1>"),
                      Att("empty.txt", b"")]),
    Item(subject="Hi", sender_name="Alice Example", delivery_time=datetime(2026, 1, 2, 3, 4, 5),
         recipients=[{0x3001: "Bob", 0x39FE: "bob@acme.test", 0x0C15: 1},
                     {0x3001: "Carol", 0x3003: "carol@acme.test", 0x0C15: 2}]),
    Item("IPM.Appointment", subject="Kickoff", sender_name="Bob Example",
         client_submit_time=datetime(2026, 1, 1, 10, 0)),
    Item("IPM.Contact", subject="Dana", props={0x3001: "Dana Example", 0x3A16: "ACME"}),
    BareItem({0x001A: "IPM.Microsoft.FreeBusy", 0x0037: "Busy", 0x3001: "Scheduler"}),
    _item_with(subject=OSError("bad subject"))(plain_text_body="partial"),
    Item(subject="files", attachments=[SizeFails("bad.bin", b"", method=7),
                                       Embedded("fwd", Item(subject="Inner", plain_text_body="x"))]),
]


def test_parity_with_pst_reader_for_the_same_items(store):
    _explode(store, _tree(SAMPLES))
    got = _items([[2, 0, len(SAMPLES)]])
    assert len(got) == len(SAMPLES)
    for (seq, info, pos, item), fake in zip(got, SAMPLES):
        kind = pst_reader.item_kind(fake)
        assert exploded_reader.item_kind(item) == kind
        assert exploded_reader.to_parsed(item, kind) == pst_reader.to_parsed(fake, kind)
        assert info.path == "Top/Inbox" and info.index == 2 and not info.recoverable


def test_markers_survive_the_round_trip(store, monkeypatch):
    monkeypatch.setattr(pst_reader, "MAX_ATTACHMENT_BYTES", 3)
    _explode(store, _tree([Item(attachments=[SizeFails("bad.bin", b"", method=7),
                                             Att("big.zip", b"xxxx")])]))
    ((_, _, _, item),) = _items([[2, 0, 1]])
    (_, bad), (_, big) = exploded_reader.to_parsed(item, "email").attachments
    assert isinstance(bad, UnreadableAttachment) and "data size" in bad.error
    assert isinstance(big, OversizedAttachment) and (big.size, big.limit) == (4, 3)


def test_iter_slice_numbers_and_folders(store):
    _explode(store, _tree([Item(subject=f"m{i}") for i in range(5)], [Item(subject="d0")]))
    got = _items([[2, 1, 3], [3, 0, 1]])
    assert [(seq, info.index, pos) for seq, info, pos, _ in got] == [(0, 2, 1), (1, 2, 2), (2, 3, 0)]
    assert got[2][1].recoverable and got[2][1].path == "Top/Deleted Items"
    assert [i.subject for *_, i in got] == ["m1", "m2", "d0"]


def test_kind_filtering_reads_only_the_manifest(store):
    _explode(store, _tree([Item("IPM.Contact"), Item(subject="e")]))
    store.reads.clear()
    got = _items([[2, 0, 2]])
    assert [exploded_reader.item_kind(i) for *_, i in got] == ["contact", "email"]
    assert store.reads == [pst_explode.manifest_object(pst_explode.explode_prefix(PID, JOB, PST), 2)]


def test_wholly_unreadable_item_raises_with_its_reason(store, monkeypatch):
    real = pst_reader.to_parsed

    def broken(item, kind, _depth=0):
        if getattr(item, "subject", "") == "bad":
            raise OSError("item table corrupt")
        return real(item, kind, _depth)

    monkeypatch.setattr(pst_reader, "to_parsed", broken)
    _explode(store, _tree([Item(subject="bad")]))
    ((_, _, _, item),) = _items([[2, 0, 1]])
    assert item.subject == ""
    with pytest.raises(exploded_reader.ExplodedItemError, match="item table corrupt"):
        exploded_reader.to_parsed(item, "email")


def test_missing_manifest_fails_loudly(store):
    with pytest.raises(FileNotFoundError):
        _items([[2, 0, 1]])


def test_read_subject_is_one_get(store):
    _explode(store, _tree([Item(subject="a"), Item(subject="Poison 2")]))
    store.reads.clear()
    assert exploded_reader.read_subject(JOB, PID, PST, 2, 1) == "Poison 2"
    assert store.reads == [pst_explode.item_object(pst_explode.explode_prefix(PID, JOB, PST), 2, 1)]
