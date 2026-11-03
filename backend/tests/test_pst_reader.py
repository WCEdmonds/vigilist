from datetime import datetime

import pytest

from app.services.pst_reader import (
    ContainerSurvey,
    is_recoverable,
    item_type,
    iter_slice,
    survey,
    to_parsed,
)


class Entry:
    def __init__(self, value, entry_type=None):
        self.entry_type = entry_type
        self.data_as_string = value if isinstance(value, str) else None
        self.data_as_integer = value if isinstance(value, int) else None


class RecordSet:
    def __init__(self, props):
        self._props = props

    def get_entry_by_type(self, tag):
        return Entry(self._props[tag], tag) if tag in self._props else None

    @property
    def entries(self):
        return [Entry(v, t) for t, v in self._props.items()]


class Recipients:
    def __init__(self, rows):
        self.record_sets = [RecordSet(r) for r in rows]


class Att:
    """Mirrors pypff.attachment: long_filename, size, read_buffer, record_sets."""

    def __init__(self, name, data, content_id=None, short_name=None, method=None):
        props = {}
        if content_id:
            props[0x3712] = content_id
        if short_name:
            props[0x3704] = short_name
        if method is not None:
            props[0x3705] = method
        self.long_filename = name
        self.size = len(data)
        self._data = data
        self.record_sets = [RecordSet(props)]

    def read_buffer(self, n):
        return self._data[:n]


class Item:
    """Mirrors pypff.message: class/size/recipients live in MAPI record sets."""

    def __init__(self, message_class="IPM.Note", size=None, props=None, recipients=None,
                 attachments=(), **kw):
        p = dict(props or {})
        if message_class is not None:
            p[0x001A] = message_class
        if size is not None:
            p[0x0E08] = size
        self.record_sets = [RecordSet(p)]
        self.recipients = Recipients(recipients) if recipients is not None else None
        self._attachments = list(attachments)
        self.number_of_attachments = len(self._attachments)
        self.__dict__.update(kw)

    def get_attachment(self, i):
        return self._attachments[i]


class Folder:
    def __init__(self, name, items=(), subs=()):
        self.name, self._items, self._subs = name, list(items), list(subs)

    def get_number_of_sub_folders(self):
        return len(self._subs)

    def get_sub_folder(self, i):
        return self._subs[i]

    def get_number_of_sub_messages(self):
        return len(self._items)

    def get_sub_message(self, i):
        return self._items[i]


@pytest.fixture
def fake_pst():
    inbox = Folder("Inbox", [Item(), Item(), Item("IPM.Note.SMIME"), Item("IPM.Schedule.Meeting.Request")])
    cal = Folder("Calendar", [Item("IPM.Appointment"), Item("IPM.Appointment")])
    rec = Folder("Recoverable Items", [Item(), Item("IPM.Contact")])
    return Folder("", subs=[Folder("Top", subs=[inbox, cal, rec])])


@pytest.fixture
def fake_email_item():
    return Item(
        subject="Q3 numbers",
        plain_text_body="see attached",
        html_body='<p>hi <img src="cid:logo1"></p>',
        transport_headers="From: alice@acme.test\r\nMessage-ID: <m1@acme.test>\r\nSubject: x\r\n",
        attachments=[Att("numbers.csv", b"a,b\n"), Att("logo.png", b"\x89PNG", "<logo1>")],
    )


def test_item_type_mapping():
    assert item_type("IPM.Note") == "email"
    assert item_type("IPM.Note.SMIME") == "email"
    assert item_type("IPM.Schedule.Meeting.Request") == "email"
    assert item_type("IPM.Appointment") == "calendar"
    assert item_type("ipm.contact") == "contact"
    assert item_type("IPM.DistList") == "contact"
    assert item_type("IPM.Task") == "task"
    assert item_type("IPM.StickyNote") == "note"
    assert item_type("IPM.Post") == "other"
    assert item_type(None) == "email"


def test_is_recoverable():
    assert is_recoverable("Top/Recoverable Items/Purges")
    assert is_recoverable("Top/deleted items")
    assert not is_recoverable("Top/Inbox")


def test_survey_is_deterministic_and_counts_by_type(fake_pst):
    s1, s2 = survey(fake_pst), survey(fake_pst)
    assert [f.path for f in s1.folders] == [f.path for f in s2.folders]
    assert s1.totals() == {"email": 5, "calendar": 2, "contact": 1, "task": 0, "note": 0, "other": 0}
    assert [f.recoverable for f in s1.folders if f.path.endswith("Recoverable Items")] == [True]
    assert [f.index for f in s1.folders] == list(range(len(s1.folders)))


def test_survey_json_roundtrip(fake_pst):
    s = survey(fake_pst)
    s.storage_path = "a/b.pst"
    assert ContainerSurvey.from_json(s.to_json()) == s


def test_iter_slice_yields_exact_range(fake_pst):
    s = survey(fake_pst)
    inbox = next(f.index for f in s.folders if f.path.endswith("Inbox"))
    items = list(iter_slice(fake_pst, [[inbox, 1, 3]]))
    assert [(f.index, i) for _, f, i, _ in items] == [(inbox, 1), (inbox, 2)]
    assert [seq for seq, *_ in items] == [0, 1]


def test_to_parsed_message_maps_email_fields(fake_email_item):
    p = to_parsed(fake_email_item, "email")
    assert p.subject == "Q3 numbers" and p.from_ == "alice@acme.test"
    assert p.message_id == "<m1@acme.test>"
    assert ("numbers.csv", b"a,b\n") in p.attachments
    assert p.inline_names == {"logo.png"}


def test_to_parsed_message_tolerates_missing_fields():
    p = to_parsed(Item(), "email")
    assert p.subject == "" and p.body_text == "" and p.date_sent is None


def test_survey_reads_class_and_size_from_mapi_properties():
    f = Folder("Inbox", [Item("IPM.Appointment", size=500), Item("IPM.Note", size=100),
                         Item("IPM.Note", size=50)])
    s = survey(Folder("", subs=[f]))
    inbox = next(x for x in s.folders if x.path == "Inbox")
    assert inbox.counts["calendar"] == 1 and inbox.counts["email"] == 2
    assert inbox.bytes["email"] == 150 and inbox.bytes["calendar"] == 500


def test_attachment_name_falls_back_to_short_filename_then_default():
    item = Item(attachments=[Att("", b"x", short_name="SHORT~1.TXT"), Att("", b"y")])
    names = [n for n, _ in to_parsed(item, "email").attachments]
    assert names == ["SHORT~1.TXT", "attachment"]


def test_headerless_email_falls_back_to_sender_and_recipients_table():
    item = Item(
        subject="Hi", sender_name="Alice Example",
        delivery_time=datetime(2026, 1, 2, 3, 4, 5),
        recipients=[
            {0x3001: "Bob", 0x39FE: "bob@acme.test", 0x0C15: 1},
            {0x3001: "Carol", 0x3003: "carol@acme.test", 0x0C15: 2},
            {0x3001: "dave@acme.test", 0x39FE: "dave@acme.test", 0x0C15: 3},
        ],
    )
    p = to_parsed(item, "email")
    assert p.from_ == "Alice Example"
    assert p.to == "Bob <bob@acme.test>"
    assert p.cc == "Carol <carol@acme.test>"
    assert p.bcc == "dave@acme.test"
    assert p.date_sent == "2026-01-02T03:04:05"


def test_to_parsed_renders_calendar_as_text():
    cal = Item(
        "IPM.Appointment", subject="Kickoff", sender_name="Bob Example",
        client_submit_time=datetime(2026, 1, 1, 10, 0),
        recipients=[{0x3001: "Carol", 0x39FE: "carol@acme.test", 0x0C15: 1}],
    )
    p = to_parsed(cal, "calendar")
    assert p.subject == "Kickoff"
    assert "Organizer: Bob Example" in p.body_text
    assert "Attendees: Carol <carol@acme.test>" in p.body_text
    assert "Sent: 2026-01-01T10:00:00" in p.body_text
    assert "Start:" not in p.body_text and "Location:" not in p.body_text


def test_contact_renders_only_non_empty_mapi_fields():
    c = Item("IPM.Contact", subject="Dana", props={0x3001: "Dana Example", 0x3A16: "ACME"})
    body = to_parsed(c, "contact").body_text
    assert body == "Name: Dana Example\nCompany: ACME"


def test_survey_records_type_positions_as_runs(fake_pst):
    s = survey(fake_pst)
    runs = {f.path: f.runs for f in s.folders}
    assert runs["Top/Inbox"] == [["email", 4]]
    assert runs["Top/Calendar"] == [["calendar", 2]]
    assert runs["Top/Recoverable Items"] == [["email", 1], ["contact", 1]]
    assert runs["Top"] == []
    assert ContainerSurvey.from_json(s.to_json()).folders[-1].runs == [["email", 1], ["contact", 1]]


# --- Unit 8: per-field read errors, attachment methods, bare items -----------

from app.services.email_parse import (  # noqa: E402
    OversizedAttachment,
    UnreadableAttachment,
    _parse_eml_bytes,
)


def _raises(exc):
    def get(self):
        raise exc

    return property(get)


def _item_with(**raising):
    """An Item subclass whose named attributes raise on access."""
    return type("RaisingItem", (Item,), {k: _raises(v) for k, v in raising.items()})


def test_partial_item_keeps_readable_fields_and_records_the_failed_field():
    cls = _item_with(subject=OSError("libpff_message_get_subject: unable to read"))
    item = cls(plain_text_body="still here",
               transport_headers="From: alice@acme.test\r\nSubject: From headers\r\n")
    p = to_parsed(item, "email")
    assert p.body_text == "still here" and p.from_ == "alice@acme.test"
    assert p.subject == "From headers"  # header fallback still applies
    (err,) = p.read_errors
    assert err.startswith("subject: OSError") and "unable to read" in err


def test_missing_html_body_is_not_a_read_error():
    cls = _item_with(html_body=OSError("libpff_message_get_html_body: missing value data"))
    p = to_parsed(cls(subject="s", plain_text_body="b"), "email")
    assert p.read_errors == [] and p.body_html == "" and p.body_text == "b"


def test_other_html_body_error_is_recorded():
    cls = _item_with(html_body=OSError("corrupt block"))
    p = to_parsed(cls(subject="s"), "email")
    assert [e.split(":")[0] for e in p.read_errors] == ["html_body"]


def test_every_field_failure_is_recorded_for_non_email_kinds():
    cls = _item_with(plain_text_body=OSError("bad body"), sender_name=OSError("bad sender"))
    p = to_parsed(cls("IPM.Appointment", subject="Kickoff"), "calendar")
    assert p.subject == "Kickoff"
    assert sorted(e.split(":")[0] for e in p.read_errors) == ["plain_text_body", "sender_name"]


def test_headers_and_recipients_failures_are_recorded():
    class BadTable:
        @property
        def record_sets(self):
            raise OSError("bad recipients table")

    cls = _item_with(transport_headers=OSError("bad headers"))
    item = cls(subject="s", sender_name="Alice")
    item.recipients = BadTable()
    p = to_parsed(item, "email")
    assert p.from_ == "Alice"
    assert sorted(e.split(":")[0] for e in p.read_errors) == ["recipients", "transport_headers"]


class SizeFails(Att):
    @property
    def size(self):
        raise OSError("libpff_attachment_get_data_size: failed to retrieve data size")

    @size.setter
    def size(self, v):
        pass


def test_unreadable_attachment_becomes_an_error_marker_not_a_drop():
    item = Item(subject="s", attachments=[Att("ok.txt", b"fine"), SizeFails("bad.bin", b"", method=7)])
    p = to_parsed(item, "email")
    assert [n for n, _ in p.attachments] == ["ok.txt", "bad.bin"]
    bad = p.attachments[1][1]
    assert isinstance(bad, UnreadableAttachment) and bytes(bad) == b""
    assert "failed to retrieve data size" in bad.error
    (err,) = p.read_errors
    assert err.startswith("attachment 2 (bad.bin)") and "failed to retrieve data size" in err


def test_attachment_that_cannot_be_opened_is_an_error_marker():
    class Broken(Item):
        def get_attachment(self, i):
            raise OSError("unable to open attachment")

    item = Broken(subject="s", attachments=[Att("x", b"")])
    ((name, data),) = to_parsed(item, "email").attachments
    assert name == "attachment 1" and isinstance(data, UnreadableAttachment)


class Embedded(Att):
    """Method-5 attachment exposing its embedded message as sub item 0."""

    def __init__(self, name, inner):
        super().__init__(name, b"RAW-MAPI-BLOB", method=5)
        self._inner = inner
        self.number_of_sub_items = 1 if inner is not None else 0

    def get_sub_item(self, i):
        if self._inner is None:
            raise OSError("no sub item")
        return self._inner

    def read_buffer(self, n):
        if self._inner is not None:
            raise AssertionError("an embedded message must not be read as raw bytes")
        return super().read_buffer(n)


def test_method5_attachment_is_read_as_an_embedded_message():
    inner = Item(subject="Inner note", plain_text_body="inner body",
                 transport_headers="From: bob@acme.test\r\nTo: carol@acme.test\r\n",
                 attachments=[Att("deep.txt", b"deep data")])
    item = Item(subject="Outer", attachments=[Embedded("fwd", inner)])
    p = to_parsed(item, "email")
    ((name, data),) = p.attachments
    assert name == "Inner note.eml"
    nested = _parse_eml_bytes(bytes(data))
    assert nested.subject == "Inner note" and nested.from_ == "bob@acme.test"
    assert nested.to == "carol@acme.test" and "inner body" in nested.body_text
    assert ("deep.txt", b"deep data") in nested.attachments
    assert p.read_errors == []


def test_method5_that_cannot_be_opened_keeps_raw_bytes_and_records_it():
    item = Item(subject="Outer", attachments=[Embedded("fwd.msg", None)])
    p = to_parsed(item, "email")
    ((name, data),) = p.attachments
    assert name == "fwd.msg" and data == b"RAW-MAPI-BLOB"
    (err,) = p.read_errors
    assert err.startswith("attachment 1 (fwd.msg)") and "embedded message" in err


class OleFails(Att):
    def read_buffer(self, n):
        raise OSError("unable to read OLE stream")


def test_ole_attachment_that_cannot_be_read_is_an_error_marker():
    item = Item(subject="s", attachments=[OleFails("chart.bin", b"xx", method=6)])
    p = to_parsed(item, "email")
    ((name, data),) = p.attachments
    assert name == "chart.bin" and isinstance(data, UnreadableAttachment)
    assert "OLE" in data.error and "unable to read OLE stream" in data.error
    assert len(p.read_errors) == 1


def test_ole_attachment_that_reads_keeps_its_bytes():
    item = Item(subject="s", attachments=[Att("chart.bin", b"ole-bytes", method=6)])
    assert to_parsed(item, "email").attachments == [("chart.bin", b"ole-bytes")]


def test_oversized_attachment_still_uses_the_cap_marker(monkeypatch):
    import app.services.pst_reader as pr

    monkeypatch.setattr(pr, "MAX_ATTACHMENT_BYTES", 3)
    ((_, data),) = to_parsed(Item(attachments=[Att("big.zip", b"xxxx")]), "email").attachments
    assert isinstance(data, OversizedAttachment) and data.size == 4


class BareItem:
    """pypff.item that is not a message: record sets only, no message accessors."""

    def __init__(self, props):
        self.record_sets = [RecordSet(props)]


def test_bare_item_is_rendered_from_its_record_properties():
    from app.services.pst_reader import item_kind

    bare = BareItem({0x001A: "IPM.Microsoft.ScheduleData.FreeBusy", 0x0037: "Busy times",
                     0x3001: "Scheduler", 0x0E08: 1234})
    assert item_kind(bare) == "other"
    p = to_parsed(bare, "other")
    assert p.subject == "Busy times"
    assert "Message class: IPM.Microsoft.ScheduleData.FreeBusy" in p.body_text
    assert "Scheduler" in p.body_text
    assert p.read_errors == []


def test_inline_cid_match_is_word_bounded():
    item = Item(html_body='<img src="cid:logo10">',
                attachments=[Att("a.png", b"a", "<logo1>"), Att("b.png", b"b", "<logo10>")])
    assert to_parsed(item, "email").inline_names == {"b.png"}


def test_open_pst_opens_a_local_file(monkeypatch):
    import sys
    import types

    import app.services.pst_reader as pr

    opened = []

    class FakeFile:
        def open(self, path):
            opened.append(path)

        def get_root_folder(self):
            return Folder("")

        def close(self):
            opened.append("closed")

    monkeypatch.setitem(sys.modules, "pypff", types.SimpleNamespace(file=FakeFile))
    with pr.open_pst("/tmp/x.pst", "productions/1/raw/x.pst") as handle:
        assert handle.storage_path == "productions/1/raw/x.pst"
    assert opened == ["/tmp/x.pst", "closed"]
