"""TAR validation (P3-3): recall/precision vs a blind control set + elusion.

The classifier under validation is a ReviewProject's ai_decision output.
Human truth comes from tags applied to frozen P3-2 samples, independent of
the machine. Uncoded, conflicted, and machine-undecided documents are
excluded from the confusion matrix and REPORTED — visible honesty over
silent inclusion.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DocumentTag, Sample
from app.models_review import AIReviewResult
from app.services.sampling_stats import wilson_ci

MACHINE_POSITIVE = {"relevant", "key_document"}
MACHINE_NEGATIVE = {"not_relevant"}


def _ci_dict(positives: int, n: int, confidence: int) -> dict:
    rate, low, high = wilson_ci(positives, n, confidence)
    return {"rate": rate, "low": low, "high": high, "n": n, "positives": positives}


async def _tag_members(db: AsyncSession, tag_id: int | None, ids: list) -> set:
    if not tag_id or not ids:
        return set()
    rows = (await db.execute(
        select(DocumentTag.document_id).where(
            DocumentTag.tag_id == tag_id,
            DocumentTag.document_id.in_(ids),
        )
    )).all()
    idset = set(ids)
    return {str(r[0]) for r in rows if str(r[0]) in idset}


async def build_validation(
    db: AsyncSession,
    production_id: int,
    project_id: int,
    control_sample: Sample,
    responsive_tag_id: int,
    nonresponsive_tag_id: int | None,
    elusion_sample: Sample | None,
    confidence: int,
) -> dict:
    control_ids = [str(d) for d in (control_sample.document_ids or [])]
    notes: list[str] = []

    dec_rows = (await db.execute(
        select(AIReviewResult.document_id, AIReviewResult.ai_decision).where(
            AIReviewResult.project_id == project_id,
            AIReviewResult.document_id.in_(control_ids),
        )
    )).all()
    machine = {str(d): dec for d, dec in dec_rows}

    human_pos = await _tag_members(db, responsive_tag_id, control_ids)
    human_neg = (await _tag_members(db, nonresponsive_tag_id, control_ids)
                 if nonresponsive_tag_id else set())

    conflicted = human_pos & human_neg
    coded_pos = human_pos - conflicted
    coded_neg = human_neg - conflicted
    coded = coded_pos | coded_neg
    uncoded = [d for d in control_ids if d not in coded and d not in conflicted]

    tp = fp = fn = tn = 0
    machine_undecided = 0
    for d in coded:
        dec = machine.get(d)
        if dec in MACHINE_POSITIVE:
            if d in coded_pos:
                tp += 1
            else:
                fp += 1
        elif dec in MACHINE_NEGATIVE:
            if d in coded_pos:
                fn += 1
            else:
                tn += 1
        else:
            machine_undecided += 1

    if uncoded:
        notes.append(f"{len(uncoded)} control documents are uncoded and were excluded")
    if conflicted:
        notes.append(f"{len(conflicted)} control documents carry both tags and were excluded")
    if machine_undecided:
        notes.append(f"{machine_undecided} coded documents lack a machine decision and were excluded from the matrix")
    if not nonresponsive_tag_id:
        notes.append("no non-responsive tag provided; uncoded may include documents never reviewed")
    if (tp + fn) == 0:
        notes.append("no human-positive documents with machine decisions; recall undefined")

    control = {
        "sample_id": control_sample.id,
        "n": len(control_ids),
        "coded": len(coded),
        "uncoded": len(uncoded),
        "conflicted": len(conflicted),
        "machine_undecided": machine_undecided,
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "richness": _ci_dict(len(coded_pos), len(coded), confidence) if coded else None,
        "recall": _ci_dict(tp, tp + fn, confidence) if (tp + fn) else None,
        "precision": _ci_dict(tp, tp + fp, confidence) if (tp + fp) else None,
        "notes": notes,
    }

    elusion = None
    if elusion_sample is not None:
        e_ids = [str(d) for d in (elusion_sample.document_ids or [])]
        e_pos = await _tag_members(db, responsive_tag_id, e_ids)
        null_set_size = (await db.execute(
            select(func.count(AIReviewResult.id)).where(
                AIReviewResult.project_id == project_id,
                AIReviewResult.ai_decision == "not_relevant",
            )
        )).scalar() or 0
        rate, low, high = wilson_ci(len(e_pos), len(e_ids), confidence)
        elusion = {
            "sample_id": elusion_sample.id,
            "n": len(e_ids),
            "positives": len(e_pos),
            "rate": rate, "low": low, "high": high,
            "null_set_size": null_set_size,
            "estimated_missed_low": int(low * null_set_size),
            "estimated_missed_high": int(high * null_set_size),
        }

    return {
        "confidence": confidence,
        "project_id": project_id,
        "control": control,
        "elusion": elusion,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
