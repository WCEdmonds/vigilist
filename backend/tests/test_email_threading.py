"""Unit tests for the pure email-threading engine (SP4b-2). No DB/network."""

from datetime import datetime, timezone

from app.services.email_threading import (
    ThreadMsg,
    compute_thread_assignments,
    normalize_subject,
)


def _dt(day: int) -> datetime:
    return datetime(2026, 7, day, 12, 0, 0, tzinfo=timezone.utc)


def test_normalize_subject_strips_reply_forward_prefixes():
    assert normalize_subject("Re: Q3 numbers") == "q3 numbers"
    assert normalize_subject("FW: Fwd: Re:  Q3   numbers ") == "q3 numbers"
    assert normalize_subject("Q3 numbers") == "q3 numbers"


def test_reply_chain_groups_and_only_leaf_is_inclusive():
    msgs = [
        ThreadMsg("a", message_id="<a>", subject="Hi", date_sent=_dt(1)),
        ThreadMsg("b", message_id="<b>", in_reply_to="<a>", subject="Re: Hi", date_sent=_dt(2)),
        ThreadMsg("c", message_id="<c>", in_reply_to="<b>", subject="Re: Hi", date_sent=_dt(3)),
    ]
    res = compute_thread_assignments(msgs, production_id=1)
    tids = {res[k].thread_id for k in ("a", "b", "c")}
    assert len(tids) == 1  # one thread
    assert res["c"].is_inclusive is True
    assert res["a"].is_inclusive is False
    assert res["b"].is_inclusive is False


def test_branch_marks_both_leaves_inclusive():
    msgs = [
        ThreadMsg("a", message_id="<a>", subject="Hi", date_sent=_dt(1)),
        ThreadMsg("b", message_id="<b>", in_reply_to="<a>", subject="Re: Hi", date_sent=_dt(2)),
        ThreadMsg("c", message_id="<c>", in_reply_to="<a>", subject="Re: Hi", date_sent=_dt(3)),
    ]
    res = compute_thread_assignments(msgs, production_id=1)
    assert len({res[k].thread_id for k in ("a", "b", "c")}) == 1
    assert res["b"].is_inclusive is True
    assert res["c"].is_inclusive is True
    assert res["a"].is_inclusive is False


def test_references_only_linking_still_groups():
    msgs = [
        ThreadMsg("a", message_id="<a>", subject="Hi", date_sent=_dt(1)),
        ThreadMsg("b", message_id="<b>", references="<x> <a>", subject="Re: Hi", date_sent=_dt(2)),
    ]
    res = compute_thread_assignments(msgs, production_id=1)
    assert res["a"].thread_id == res["b"].thread_id
    assert res["b"].is_inclusive is True
    assert res["a"].is_inclusive is False


def test_subject_fallback_groups_headerless_and_latest_is_inclusive():
    msgs = [
        ThreadMsg("a", subject="Re: Budget", date_sent=_dt(1)),
        ThreadMsg("b", subject="Budget", date_sent=_dt(5)),
    ]
    res = compute_thread_assignments(msgs, production_id=1)
    assert res["a"].thread_id == res["b"].thread_id  # same normalized subject
    assert res["b"].is_inclusive is True   # latest by date
    assert res["a"].is_inclusive is False


def test_singleton_is_its_own_inclusive_thread():
    msgs = [ThreadMsg("solo", message_id="<solo>", subject="Unique", date_sent=_dt(1))]
    res = compute_thread_assignments(msgs, production_id=1)
    assert res["solo"].is_inclusive is True


def test_deterministic_regardless_of_input_order():
    base = [
        ThreadMsg("a", message_id="<a>", subject="Hi", date_sent=_dt(1)),
        ThreadMsg("b", message_id="<b>", in_reply_to="<a>", subject="Re: Hi", date_sent=_dt(2)),
        ThreadMsg("c", message_id="<c>", in_reply_to="<b>", subject="Re: Hi", date_sent=_dt(3)),
    ]
    forward = compute_thread_assignments(base, production_id=1)
    reversed_ = compute_thread_assignments(list(reversed(base)), production_id=1)
    assert forward == reversed_


def test_same_message_id_different_production_yields_different_thread_id():
    msgs = [ThreadMsg("a", message_id="<a>", subject="Hi", date_sent=_dt(1))]
    p1 = compute_thread_assignments(msgs, production_id=1)["a"].thread_id
    p2 = compute_thread_assignments(msgs, production_id=2)["a"].thread_id
    assert p1 != p2


def test_subject_fallback_handles_naive_datetimes_without_raising():
    # A tz-naive date_sent must not raise when compared against the tz-aware
    # epoch in the latest-by-date fallback; the later (naive) message wins.
    naive_early = datetime(2026, 7, 1, 12, 0, 0)
    naive_late = datetime(2026, 7, 9, 12, 0, 0)
    msgs = [
        ThreadMsg("a", subject="Budget", date_sent=naive_early),
        ThreadMsg("b", subject="Re: Budget", date_sent=naive_late),
    ]
    res = compute_thread_assignments(msgs, production_id=1)
    assert res["a"].thread_id == res["b"].thread_id
    assert res["b"].is_inclusive is True
    assert res["a"].is_inclusive is False
