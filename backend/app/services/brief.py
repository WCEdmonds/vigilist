"""Production Brief generation: an AI-written orientation for a production.

Themes are NOT model output — they are the live cluster rows, passed in only
as context. The model contributes overview, key players, date range, and
notable documents. Parsing is defensive: any malformed response yields None
and the pipeline records the brief stage as failed.
"""

import json
import logging
import re
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Document, DocumentCluster, DocumentClusterAssignment, Production

logger = logging.getLogger(__name__)

BRIEF_MODEL = "claude-sonnet-4-6"
SAMPLES_PER_THEME = 2
SNIPPET_CHARS = 400


def _get_client():
    if not settings.anthropic_api_key:
        return None
    import anthropic

    return anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)


def build_brief_prompt(
    case_context: str | None,
    doc_count: int,
    date_hint: str | None,
    themes: list[dict],
    samples: list[dict],
) -> str:
    context_block = (
        f"Case description from counsel:\n{case_context}"
        if case_context
        else "No case description was provided."
    )
    theme_lines = "\n".join(
        f"- {t['label']} ({t['doc_count']} documents)" for t in themes
    ) or "- (no themes detected)"
    sample_lines = "\n\n".join(
        f"[{s['bates']}] {s.get('title') or 'Untitled'}\n{(s.get('snippet') or '')[:SNIPPET_CHARS]}"
        for s in samples
    ) or "(no samples available)"
    bates_line = f"Bates range: {date_hint}\n" if date_hint else ""

    return f"""You are briefing a legal team on a newly received document production.

{context_block}

Production facts:
- {doc_count} documents
{bates_line}- Detected themes:
{theme_lines}

Representative documents:
{sample_lines}

Write a JSON object with exactly these keys:
- "overview": 2-4 sentences orienting a reviewer — what this production contains and what stands out. Plain prose, no hedging boilerplate.
- "key_players": array of up to 6 people/organizations that recur (empty array if unclear).
- "date_range": human-readable date span of the documents if evident from their content, else null.
- "notable_documents": array of up to 4 objects {{"bates": "...", "reason": "..."}} drawn ONLY from the representative documents above.

Respond with ONLY the JSON object."""


def parse_brief_response(raw: str) -> dict | None:
    if not raw:
        return None
    text = raw.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("overview"):
        return None
    return {
        "overview": str(data["overview"]),
        "key_players": [str(p) for p in data.get("key_players") or []][:6],
        "date_range": data.get("date_range") or None,
        "notable_documents": [
            {"bates": str(d.get("bates", "")), "reason": str(d.get("reason", ""))}
            for d in (data.get("notable_documents") or [])
            if isinstance(d, dict)
        ][:4],
    }


async def generate_brief(db: AsyncSession, production_id: int) -> dict | None:
    """Build inputs from the DB, call the model once, return the brief dict."""
    client = _get_client()
    if client is None:
        logger.warning("Brief generation skipped: no Anthropic API key")
        return None

    prod = await db.get(Production, production_id)
    if prod is None:
        return None

    doc_count = (
        await db.execute(
            select(func.count(Document.id)).where(Document.production_id == production_id)
        )
    ).scalar() or 0

    bates = (
        await db.execute(
            select(func.min(Document.bates_begin), func.max(Document.bates_end)).where(
                Document.production_id == production_id
            )
        )
    ).one()
    date_hint = f"{bates[0]} .. {bates[1]}" if bates[0] else None

    clusters = (
        (
            await db.execute(
                select(DocumentCluster)
                .where(DocumentCluster.production_id == production_id)
                .order_by(DocumentCluster.doc_count.desc())
            )
        )
        .scalars()
        .all()
    )
    themes = [{"label": c.label or "Unlabeled", "doc_count": c.doc_count} for c in clusters]

    samples: list[dict] = []
    for c in clusters[:8]:
        rows = (
            await db.execute(
                select(Document.bates_begin, Document.title, Document.text_content)
                .join(
                    DocumentClusterAssignment,
                    DocumentClusterAssignment.document_id == Document.id,
                )
                .where(DocumentClusterAssignment.cluster_id == c.id)
                .order_by(Document.bates_begin)
                .limit(SAMPLES_PER_THEME)
            )
        ).all()
        samples.extend(
            {"bates": r[0], "title": r[1], "snippet": r[2] or ""} for r in rows
        )
    if not samples:
        rows = (
            await db.execute(
                select(Document.bates_begin, Document.title, Document.text_content)
                .where(Document.production_id == production_id)
                .order_by(Document.bates_begin)
                .limit(6)
            )
        ).all()
        samples = [{"bates": r[0], "title": r[1], "snippet": r[2] or ""} for r in rows]

    prompt = build_brief_prompt(prod.case_context, doc_count, date_hint, themes, samples)
    try:
        response = await client.messages.create(
            model=BRIEF_MODEL,
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text if response.content else ""
    except Exception:
        logger.exception("Brief model call failed for production %s", production_id)
        return None

    brief = parse_brief_response(raw)
    if brief is None:
        logger.warning("Brief response unparseable for production %s", production_id)
        return None
    brief["generated_at"] = datetime.now(timezone.utc).isoformat()
    brief["model"] = BRIEF_MODEL
    return brief
