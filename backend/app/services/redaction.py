"""Pure redaction validation + reason codes (P1-1). No DB/network."""

from __future__ import annotations

REDACTION_REASON_CODES = frozenset({
    "attorney_client",
    "work_product",
    "pii",
    "phi",
    "confidential",
    "trade_secret",
    "non_responsive",
    "other",
})


def is_valid_reason_code(code: str) -> bool:
    return code in REDACTION_REASON_CODES


def validate_rect(
    page_num: int,
    x_pct: float,
    y_pct: float,
    w_pct: float,
    h_pct: float,
    page_count: int,
) -> str | None:
    """Return an error message if the rectangle is invalid, else None."""
    if page_num < 1 or page_num > page_count:
        return f"page_num must be between 1 and {page_count}"
    if not (0.0 <= x_pct <= 100.0):
        return "x_pct must be between 0 and 100"
    if not (0.0 <= y_pct <= 100.0):
        return "y_pct must be between 0 and 100"
    if w_pct <= 0.0 or h_pct <= 0.0:
        return "w_pct and h_pct must be greater than 0"
    if x_pct + w_pct > 100.0:
        return "x_pct + w_pct must not exceed 100"
    if y_pct + h_pct > 100.0:
        return "y_pct + h_pct must not exceed 100"
    return None
