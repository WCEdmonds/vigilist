"""AI review of the entity merge docket.

The docket fills with pairs the deterministic matcher finds but will not
auto-merge, because string similarity cannot separate the safe cases from the
unsafe ones — these two score identically:

    Tate Streling / Tate Sterling   0.93   OCR corruption, safe to merge
    Elie Richards / Leslie Richards 0.93   plausibly two people, not safe

The distinguishing information is not in the strings. This pass gathers the
evidence a reviewer would gather by hand and returns one of three verdicts per
pair, so obvious duplicates clear themselves and genuine ambiguity arrives
annotated rather than bare.

Mirrors services/timeline_review.py: one batched structured-output call,
confidence gate, audit snapshots, connection released before the model call.
"""

import asyncio as _asyncio
import json
import logging
import uuid as _uuid

from sqlalchemy import func, select

from app.config import settings
from app.models import Document, Entity, EntityMention, EntityMergeSuggestion
from app.services.audit import log_action
from app.services.entity_merge import merge_entities
from app.services.timeline_review import REVIEW_MODEL, _retryable_errors

logger = logging.getLogger(__name__)

# User decision (2026-07-30). Above this a `merge` verdict is applied without
# asking; below it the pair stays in the docket with the model's reasoning.
CONFIDENCE_GATE = 0.85

_MAX_ATTEMPTS = 3
_MAX_PAIRS = 300          # one call's worth; the docket is reviewed in passes
_SNIPPETS_PER_ENTITY = 2


class MergeReviewError(Exception):
    """Model call failed or returned unusable output. Nothing is applied."""


REVIEW_SYSTEM_PROMPT = """You are resolving duplicate person and organization records extracted from OCR'd legal discovery documents.

Each pair below was flagged as similar by string distance. String distance cannot decide these: "Tate Streling"/"Tate Sterling" and "Elie Richards"/"Leslie Richards" score identically, but the first is an OCR corruption and the second is probably two people. Judge each pair on the evidence, not the score.

The question that usually decides it: **is the difference a plausible OCR error?**
- "Viles"/"Wiles" — v/w confusion. Plausible.
- "Streling"/"Sterling" — adjacent transposition. Plausible.
- "Rawford"/"Crawford" — dropped leading character. Plausible.
- "Elie"/"Leslie" — three characters differing at the head of a given name. NOT a plausible single OCR error. Two people unless the evidence says otherwise.

Weigh:
- A lopsided mention count is strong evidence of OCR: a spelling appearing twice beside one appearing forty times is usually the corruption.
- A shared email address means the same person.
- DIFFERENT email addresses mean different people. This outweighs any string similarity.
- Differing stated roles argue for different identities.

Be conservative about differences in GIVEN names (Elie/Leslie, Joan/John, Erin/Erik) — those are different people absent corroboration. A corrupted SURNAME with a matching given name is the safe class.

IMPORTANT: both names appearing in the same document is NOT evidence they are different people. OCR produces the correct spelling and the corruption inside a single document routinely. Treat co-occurrence as neutral.

Return one verdict per pair:
- "merge" — the same entity. Give the id of the record to KEEP as `keep_id` (normally the one with more mentions and the better-formed spelling).
- "distinct" — different entities. The suggestion is dismissed.
- "unclear" — you cannot tell from this evidence. Say what you would need. A human will decide.

Every verdict carries a one-sentence reason and a confidence from 0 to 1. Only "merge" verdicts at confidence 0.85 or above are applied, so do not pad with low-confidence guesses — "unclear" is a useful answer, not a failure.

NAME CORRECTIONS. Separately from the verdict, you may correct a record whose stored name is itself a corruption, by adding entries to `corrections`. This is independent of the verdict: two records can be genuinely distinct and one still be misnamed, and two records can merge while the survivor still carries a mangled name.

The corrected name MUST appear verbatim in that record's snippets. A correction whose name does not occur in the snippets is discarded — so read the name out of the document text rather than reconstructing what you think it should be. If the snippets show "BEFORE THE HONORABLE PAMELA K. ALBAN", "Pamela K. Alban" is a valid correction; if they only show "Pamela Neill", it is not, however confident you are about the real name.

Do not correct casing or punctuation alone. Correct a name only when the stored one is wrong — clipped, transposed, misread, or a different person's name entirely."""


MERGE_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "suggestion_id": {"type": "integer"},
                    "verdict": {"type": "string", "enum": ["merge", "distinct", "unclear"]},
                    "keep_id": {"type": "string"},
                    "reason": {"type": "string"},
                    "confidence": {"type": "number"},
                    # Optional name corrections, independent of the verdict:
                    # two records can be distinct and one still be misnamed.
                    "corrections": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "entity_id": {"type": "string"},
                                "corrected_name": {"type": "string"},
                                "reason": {"type": "string"},
                                "confidence": {"type": "number"},
                            },
                            "required": ["entity_id", "corrected_name", "reason",
                                         "confidence"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["suggestion_id", "verdict", "reason", "confidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}


async def gather_pair_evidence(db, production_id: int) -> list[dict]:
    """Evidence for every pending suggestion in a production.

    Co-occurrence is included but labelled neutral: in an OCR'd corpus both
    spellings routinely appear in the same document, so it must never be read
    as evidence of distinctness.
    """
    rows = (await db.execute(
        select(EntityMergeSuggestion)
        .where(EntityMergeSuggestion.production_id == production_id,
               EntityMergeSuggestion.status == "pending")
        .order_by(EntityMergeSuggestion.score.desc())
        .limit(_MAX_PAIRS)
    )).scalars().all()
    if not rows:
        return []

    entity_ids = {r.entity_a_id for r in rows} | {r.entity_b_id for r in rows}
    entities = {
        e.id: e for e in (await db.execute(
            select(Entity).where(Entity.id.in_(entity_ids)))).scalars().all()
    }

    doc_counts = dict((await db.execute(
        select(EntityMention.entity_id, func.count(func.distinct(EntityMention.document_id)))
        .where(EntityMention.entity_id.in_(entity_ids))
        .group_by(EntityMention.entity_id)
    )).all())

    # Documents each entity appears in, for the neutral co-occurrence figure.
    doc_sets: dict = {}
    for ent_id, doc_id in (await db.execute(
        select(EntityMention.entity_id, EntityMention.document_id)
        .where(EntityMention.entity_id.in_(entity_ids)).distinct()
    )).all():
        doc_sets.setdefault(ent_id, set()).add(doc_id)

    snippets: dict = {}
    for ent_id, snippet, bates in (await db.execute(
        select(EntityMention.entity_id, EntityMention.context_snippet, Document.bates_begin)
        .join(Document, EntityMention.document_id == Document.id)
        .where(EntityMention.entity_id.in_(entity_ids),
               EntityMention.context_snippet.isnot(None))
    )).all():
        bucket = snippets.setdefault(ent_id, [])
        if len(bucket) < _SNIPPETS_PER_ENTITY:
            bucket.append(f"{bates}: {(snippet or '')[:180]}")

    def side(entity) -> dict:
        attrs = entity.attributes if isinstance(entity.attributes, dict) else {}
        return {
            "id": str(entity.id),
            "name": entity.canonical_name,
            "aliases": list(entity.aliases or [])[:5],
            "mentions": entity.mention_count,
            "documents": doc_counts.get(entity.id, 0),
            "emails": [e for e in (attrs.get("emails") or []) if e][:3],
            "role": attrs.get("role"),
            "snippets": snippets.get(entity.id, []),
        }

    out = []
    for s in rows:
        a, b = entities.get(s.entity_a_id), entities.get(s.entity_b_id)
        if a is None or b is None:
            continue  # entity deleted since the suggestion was raised
        out.append({
            "suggestion_id": s.id,
            "score": round(s.score, 3),
            "type": a.entity_type,
            "a": side(a),
            "b": side(b),
            "shared_documents": len(doc_sets.get(a.id, set()) & doc_sets.get(b.id, set())),
        })
    return out


def build_user_content(evidence: list[dict]) -> str:
    return (
        f"{len(evidence)} candidate pairs from one legal matter.\n\n"
        "`shared_documents` is how many documents both spellings appear in. "
        "It is NEUTRAL — OCR routinely produces both spellings in one document.\n\n"
        + json.dumps(evidence, indent=1)
    )


def parse_verdicts(raw: str) -> list[dict]:
    """Verdicts from the model's JSON. Malformed output yields none, so the
    caller leaves the whole docket untouched rather than acting on guesses."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("Merge review returned unparseable JSON")
        return []
    if not isinstance(data, dict):
        return []
    out = []
    for v in data.get("verdicts") or []:
        if not isinstance(v, dict):
            continue
        if v.get("verdict") not in ("merge", "distinct", "unclear"):
            continue
        try:
            sid = int(v["suggestion_id"])
            conf = float(v["confidence"])
        except (KeyError, TypeError, ValueError):
            continue
        corrections = []
        for c in v.get("corrections") or []:
            if not isinstance(c, dict):
                continue
            name = str(c.get("corrected_name") or "").strip()
            ent_id = str(c.get("entity_id") or "").strip()
            if not name or not ent_id or len(name) > 500:
                continue
            try:
                c_conf = float(c["confidence"])
            except (KeyError, TypeError, ValueError):
                continue
            corrections.append({
                "entity_id": ent_id,
                "corrected_name": name,
                "reason": str(c.get("reason") or "")[:500],
                "confidence": c_conf,
            })

        out.append({
            "suggestion_id": sid,
            "verdict": v["verdict"],
            "keep_id": str(v.get("keep_id") or "") or None,
            "reason": str(v.get("reason") or "")[:1000],
            "confidence": conf,
            "corrections": corrections,
        })
    return out


def _normalize_for_grounding(text: str) -> str:
    """Case- and whitespace-insensitive form for the attestation check."""
    return " ".join((text or "").split()).casefold()


async def is_name_attested(db, entity_id, name: str) -> bool:
    """True when `name` occurs verbatim in one of the entity's snippets.

    The guard that separates OCR *correction* from name *invention*. A model
    can be confident about a real-world fact — that a judge is called Alban —
    without that name existing anywhere in this matter's documents. Applying
    such a name would relabel a person across the record on the strength of
    outside knowledge. So a correction must be attested in the text.
    """
    target = _normalize_for_grounding(name)
    if not target:
        return False
    rows = (await db.execute(
        select(EntityMention.context_snippet)
        .where(EntityMention.entity_id == entity_id,
               EntityMention.context_snippet.isnot(None))
        .limit(500)
    )).scalars().all()
    return any(target in _normalize_for_grounding(s) for s in rows)


async def apply_verdicts(db, production_id: int, verdicts: list[dict], actor) -> dict:
    """Apply verdicts to the docket. Caller owns the transaction.

    Note what is deliberately absent: any guard on shared_documents. An earlier
    draft blocked merging co-occurring entities as a safety measure, which
    would have rejected exactly the OCR duplicates this exists to resolve.
    """
    merged = dismissed = annotated = skipped = renamed = 0
    reasons: list[str] = []

    async def apply_corrections(v) -> None:
        """Rename entities the model found misnamed. Independent of the
        verdict — a distinct pair can still contain a mangled name."""
        nonlocal renamed
        for c in v.get("corrections") or []:
            if c["confidence"] < CONFIDENCE_GATE:
                reasons.append(f"rename {c['corrected_name']!r}: below gate")
                continue
            try:
                ent = await db.get(Entity, _uuid.UUID(c["entity_id"]))
            except (ValueError, AttributeError):
                ent = None
            if ent is None or ent.production_id != production_id:
                continue
            new_name = c["corrected_name"].strip()
            if not new_name or new_name == ent.canonical_name:
                continue
            if not await is_name_attested(db, ent.id, new_name):
                # Never apply a name the documents do not contain.
                reasons.append(f"rename {new_name!r}: not attested in snippets")
                continue

            old = ent.canonical_name
            # Keep the old spelling as an alias, matching the manual rename
            # endpoint: it stays searchable and matchable instead of vanishing.
            aliases = list(ent.aliases or [])
            if old and old not in aliases:
                aliases.append(old)
            ent.aliases = aliases
            ent.canonical_name = new_name
            await log_action(
                db, actor, "entity_renamed", "entity", str(ent.id),
                production_id=production_id,
                details={"old_name": old, "new_name": new_name,
                         "source": "ai_merge_review", "reason": c["reason"]},
            )
            renamed += 1

    for v in verdicts:
        s = await db.get(EntityMergeSuggestion, v["suggestion_id"])
        if s is None or s.production_id != production_id:
            skipped += 1
            continue
        if s.status != "pending":
            # Resolved by a human between dispatch and now — theirs wins.
            skipped += 1
            continue

        s.score = max(0.0, min(1.0, v["confidence"]))
        s.rationale = f"AI review: {v['reason']}"

        # Before the verdict: a correction on the losing side would be lost
        # once that entity is merged away, and merge_entities carries aliases
        # (including the old spelling) onto the survivor anyway.
        await apply_corrections(v)

        if v["verdict"] == "distinct":
            s.status = "rejected"
            s.resolved_by = actor.id
            s.resolved_at = func.now()
            dismissed += 1
            continue

        if v["verdict"] != "merge" or v["confidence"] < CONFIDENCE_GATE:
            annotated += 1
            if v["verdict"] == "merge":
                reasons.append(f"#{s.id}: merge below {CONFIDENCE_GATE} confidence")
            continue

        a = await db.get(Entity, s.entity_a_id)
        b = await db.get(Entity, s.entity_b_id)
        if a is None or b is None:
            skipped += 1
            continue

        # The model names the record to keep; fall back to mention count.
        keep, drop = (a, b) if a.mention_count >= b.mention_count else (b, a)
        if v["keep_id"] == str(b.id):
            keep, drop = b, a
        elif v["keep_id"] == str(a.id):
            keep, drop = a, b

        try:
            await merge_entities(db, keep, drop, actor.id)
        except ValueError as e:
            skipped += 1
            reasons.append(f"#{s.id}: {e}")
            continue

        s.status = "accepted"
        s.resolved_by = actor.id
        s.resolved_at = func.now()
        merged += 1

    return {"merged": merged, "dismissed": dismissed, "annotated": annotated,
            "renamed": renamed, "skipped": skipped, "skip_reasons": reasons[:20]}


async def _call_model(user_content: str) -> tuple[str, str, dict]:
    """One structured-output call. Raises MergeReviewError when exhausted."""
    if not settings.anthropic_api_key:
        raise MergeReviewError("No Anthropic API key configured")
    import anthropic  # lazy: keep the SDK off the startup/alembic path

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    retryable = _retryable_errors()
    last_err: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            async with client.messages.stream(
                model=REVIEW_MODEL,
                max_tokens=64000,
                thinking={"type": "adaptive"},
                system=REVIEW_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_content}],
                output_config={"format": {"type": "json_schema",
                                          "schema": MERGE_REVIEW_SCHEMA}},
                extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
                extra_body={"fallbacks": [{"model": "claude-opus-4-8"}]},
            ) as stream:
                response = await stream.get_final_message()
            raw = next((b.text for b in response.content if b.type == "text"), "")
            usage = {"input_tokens": response.usage.input_tokens,
                     "output_tokens": response.usage.output_tokens,
                     "served_by": response.model}
            return raw, response.stop_reason, usage
        except retryable as e:
            last_err = e
            status = getattr(e, "status_code", None)
            if status is not None and status not in (408, 429) and status < 500:
                raise MergeReviewError(f"Merge review failed with status {status}") from e
            logger.warning("Merge review attempt %d/%d failed: %s",
                           attempt + 1, _MAX_ATTEMPTS, e)
            if attempt < _MAX_ATTEMPTS - 1:
                await _asyncio.sleep(2 * (attempt + 1))
    raise MergeReviewError(f"Merge review failed after {_MAX_ATTEMPTS} attempts: {last_err}")


async def run_merge_review(db, production_id: int, actor) -> dict:
    """Review the pending merge docket in one call and apply the verdicts."""
    evidence = await gather_pair_evidence(db, production_id)
    if not evidence:
        return {"status": "empty", "merged": 0, "dismissed": 0, "annotated": 0,
                "renamed": 0, "skipped": 0, "skip_reasons": []}

    # The call takes minutes. End the read transaction so the connection goes
    # back to the pool — held across the call it sits idle past Neon's kill
    # window and dies (#87, #88). expire_on_commit=False keeps rows usable.
    await db.commit()

    raw, stop_reason, usage = await _call_model(build_user_content(evidence))
    if stop_reason == "max_tokens":
        raise MergeReviewError("Merge review response truncated — nothing applied")
    if stop_reason == "refusal":
        raise MergeReviewError("Merge review refused by the model — nothing applied")

    verdicts = parse_verdicts(raw)
    if not verdicts:
        raise MergeReviewError("Merge review returned no usable verdicts")

    result = await apply_verdicts(db, production_id, verdicts, actor)
    result["status"] = "ok"
    result["pairs_reviewed"] = len(evidence)
    result["usage"] = usage
    return result
