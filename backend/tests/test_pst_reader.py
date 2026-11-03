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
    def __init__(self, value):
        self.data_as_string = value if isinstance(value, str) else None
        self.data_as_integer = value if isinstance(value, int) else None


class RecordSet:
    def __init__(self, props):
        self._props = props

    def get_entry_by_type(self, tag):
        return Entry(self._props[tag]) if tag in self._props else None


class Recipients:
    def __init__(self, rows):
        self.record_sets = [RecordSet(r) for r in rows]


class Att:
    """Mirrors pypff.attachment: long_filename, size, read_buffer, record_sets."""

    def __init__(self, name, data, content_id=None, short_name=None):
        props = {}
        if content_id:
            props[0x3712] = content_id
        if short_name:
            props[0x3704] = short_name
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
