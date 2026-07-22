"""Pure tests for privilege/QC domain logic (P1-4/5). No DB/network."""

from datetime import datetime, timedelta, timezone

from app.services.privilege import (
    DISPOSITIONS,
    effective_disposition,
    log_description,
    qc_status,
)

_T0 = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)


# --- effective_disposition -------------------------------------------------

def test_disposition_matrix_derived():
    assert effective_disposition(True, True, None) == "redact_in_part"
    assert effective_disposition(True, False, None) == "withhold"
    assert effective_disposition(False, True, None) == "redact_in_part"
    assert effective_disposition(False, False, None) is None


def test_disposition_override_wins():
    assert effective_disposition(True, True, "withhold") == "withhold"
    assert effective_disposition(False, False, "withhold") == "withhold"
    assert effective_disposition(True, False, "produce") == "produce"


def test_disposition_invalid_override_falls_back_to_derived():
    assert effective_disposition(True, False, "bogus") == "withhold"


def test_dispositions_constant():
    assert DISPOSITIONS == {"withhold", "redact_in_part", "produce"}


# --- qc_status -------------------------------------------------------------

def test_qc_no_redactions_not_applicable():
    assert qc_status(0, None, None) == "not_applicable"
    # even a stale decision doesn't resurrect QC on a now-unredacted doc
    assert qc_status(0, ("approved", _T0, 2), None) == "not_applicable"


def test_qc_no_decision_pending():
    assert qc_status(2, None, _T0) == "pending"


def test_qc_fresh_decision_stands():
    decided = _T0 + timedelta(hours=1)
    assert qc_status(2, ("approved", decided, 2), _T0) == "approved"
    assert qc_status(2, ("rejected", decided, 2), _T0) == "rejected"


def test_qc_edit_after_decision_invalidates():
    decided = _T0
    changed = _T0 + timedelta(minutes=5)
    assert qc_status(2, ("approved", decided, 2), changed) == "pending"


def test_qc_change_at_same_instant_invalidates():
    assert qc_status(2, ("approved", _T0, 2), _T0) == "pending"


def test_qc_delete_after_decision_invalidates_via_count():
    decided = _T0 + timedelta(hours=1)
    # counts differ (3 at decision, 2 now) though timestamps look fresh
    assert qc_status(2, ("approved", decided, 3), _T0) == "pending"


# --- log_description -------------------------------------------------------

def test_description_manual_wins_verbatim():
    out = log_description("a@x.com", "b@y.com", _T0, "eml",
                          ["Attorney-Client"], "withhold", "Hand-crafted text.")
    assert out == "Hand-crafted text."


def test_description_email_template():
    out = log_description("alice@firm.com", "bob@client.com", _T0, "eml",
                          ["Attorney-Client", "WORK PRODUCT"], "withhold", None)
    assert out == ("Email from alice@firm.com to bob@client.com dated 2026-07-22 "
                   "withheld on the basis of Attorney-Client, WORK PRODUCT.")


def test_description_redact_in_part_wording():
    out = log_description("alice@firm.com", None, _T0, "eml", ["PII"],
                          "redact_in_part", None)
    assert out == ("Email from alice@firm.com dated 2026-07-22 "
                   "produced in redacted form on the basis of PII.")


def test_description_non_email_degrades_gracefully():
    out = log_description(None, None, None, "docx", ["Attorney-Client"],
                          "withhold", None)
    assert out == "DOCX document withheld on the basis of Attorney-Client."


def test_description_no_fields_at_all():
    out = log_description(None, None, None, None, [], None, None)
    assert out == "Document."
