"""Pure privilege/QC domain logic (P1-4/5). No DB/network.

Effective disposition and QC status are always computed from current state —
never stored — so they cannot go stale.
"""

from __future__ import annotations

from datetime import datetime

DISPOSITIONS = frozenset({"withhold", "redact_in_part", "produce"})

_DISPOSITION_PHRASES = {
    "withhold": "withheld",
    "redact_in_part": "produced in redacted form",
}


def effective_disposition(
    has_privilege_tag: bool, has_redactions: bool, override: str | None
) -> str | None:
    """Override wins when valid; else derived. None = ordinary produce."""
    if override in DISPOSITIONS:
        return override
    if has_redactions:
        return "redact_in_part"
    if has_privilege_tag:
        return "withhold"
    return None


def qc_status(
    redaction_count: int,
    latest_decision: tuple[str, datetime, int] | None,
    latest_redaction_change_at: datetime | None,
) -> str:
    """latest_decision = (decision, decided_at, redaction_count_at_decision).

    A decision stands only while the redactions it approved are unchanged:
    the count snapshot catches deletions, the timestamp catches adds/edits.
    """
    if redaction_count == 0:
        return "not_applicable"
    if latest_decision is None:
        return "pending"
    decision, decided_at, count_at_decision = latest_decision
    if count_at_decision != redaction_count:
        return "pending"
    if latest_redaction_change_at is not None and latest_redaction_change_at >= decided_at:
        return "pending"
    return decision


def log_description(
    email_from: str | None,
    email_to: str | None,
    date_sent: datetime | None,
    file_type: str | None,
    basis: list[str],
    disposition: str | None,
    manual: str | None,
) -> str:
    """Deterministic template from safe metadata only. Manual wins verbatim.

    NEVER include text_content, summary, or title here — the log is read by
    opposing counsel and must not reveal privileged substance.
    """
    if manual:
        return manual
    if email_from or email_to:
        kind = "Email"
    elif file_type:
        kind = f"{file_type.upper()} document"
    else:
        kind = "Document"
    parts = [kind]
    if email_from:
        parts.append(f"from {email_from}")
    if email_to:
        parts.append(f"to {email_to}")
    if date_sent:
        parts.append(f"dated {date_sent.date().isoformat()}")
    phrase = _DISPOSITION_PHRASES.get(disposition or "")
    if phrase:
        parts.append(phrase)
    if basis:
        parts.append("on the basis of " + ", ".join(basis))
    return " ".join(parts) + "."
