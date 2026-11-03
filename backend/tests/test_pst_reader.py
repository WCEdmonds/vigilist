import pytest

from app.services.pst_reader import (
    ContainerSurvey,
    is_recoverable,
    item_type,
    iter_slice,
    survey,
    to_parsed,
)


class Item:
    def __init__(self, message_class="IPM.Note", **kw):
        self.message_class = message_class
        self.__dict__.update(kw)


class Att:
    def __init__(self, name, data, content_id=None):
        self.name, self.data, self.content_id = name, data, content_id


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


def test_to_parsed_renders_calendar_as_text():
    cal = Item("IPM.Appointment", subject="Kickoff", organizer="bob@acme.test",
               start_time="2026-01-01T10:00", location="Room 1")
    p = to_parsed(cal, "calendar")
    assert p.subject == "Kickoff"
    assert "Organizer:" in p.body_text and "Start:" in p.body_text and "Location:" in p.body_text
