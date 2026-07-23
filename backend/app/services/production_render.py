"""Render locked production sets into endorsed PDFs (P2-2). DB + storage.

Reads ONLY image_paths renditions (never native/text); redact_in_part pages
are burned via burn_page BEFORE stamping, so redacted pixels cannot reach a
produced PDF. Pure drawing lives in endorse.py.
"""

from __future__ import annotations

import io
import logging
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document, ProductionSet, ProductionSetItem, Redaction
from app.services import storage
from app.services.endorse import page_bates_numbers, slip_sheet, stamp_page
from app.services.redaction_render import burn_page

logger = logging.getLogger(__name__)


def artifact_path(production_id: int, set_id: int, bates_begin: str) -> str:
    return f"productions/{production_id}/production_sets/{set_id}/{bates_begin}.pdf"


def _load_page(raw_path: str) -> Image.Image | None:
    """Same selection rule as documents.py: productions/ prefix -> GCS."""
    try:
        if raw_path.startswith("productions/"):
            data = storage.get_download_bytes(raw_path)
            return Image.open(io.BytesIO(data)).convert("RGB")
        p = Path(raw_path.replace("\\", "/")).resolve()
        return Image.open(p).convert("RGB")
    except Exception:
        logger.warning("Unreadable page image: %s", raw_path)
        return None


async def render_item(db: AsyncSession, ps: ProductionSet,
                      item: ProductionSetItem) -> str:
    doc = await db.get(Document, item.document_id)
    designation = item.designation or ps.designation

    if item.disposition == "withhold":
        pages = [slip_sheet(item.bates_begin, designation)]
    else:
        reds_by_page: dict[int, list] = {}
        if item.disposition == "redact_in_part":
            reds = (await db.execute(
                select(Redaction).where(Redaction.document_id == item.document_id)
            )).scalars().all()
            for r in reds:
                reds_by_page.setdefault(r.page_num, []).append(r)
        bates = page_bates_numbers(item.bates_begin, ps.prefix, ps.padding,
                                   item.pages or 1)
        pages = []
        for idx, raw_path in enumerate(doc.image_paths or [], start=1):
            img = _load_page(raw_path)
            if img is None:
                continue
            if reds_by_page.get(idx):
                img = burn_page(img, reds_by_page[idx])
            # guard drift between lock snapshot and current image count
            bates_text = bates[min(idx, len(bates)) - 1]
            pages.append(stamp_page(img, bates_text, designation))
        if not pages:
            raise RuntimeError(f"No readable page images for {doc.bates_begin}")

    buf = io.BytesIO()
    pages[0].save(buf, format="PDF", save_all=True,
                  append_images=pages[1:], resolution=150.0)
    path = artifact_path(ps.production_id, ps.id, item.bates_begin)
    storage.upload_bytes(buf.getvalue(), path, "application/pdf")
    item.output_path = path
    return path


async def render_batch(db: AsyncSession, set_id: int, document_ids: list) -> int:
    """Worker unit. Commits after each item; marks the set errored on failure
    and returns instead of raising (Cloud Tasks would retry non-2xx forever)."""
    ps = await db.get(ProductionSet, set_id)
    if not ps or ps.status != "locked":
        return 0
    items = (await db.execute(
        select(ProductionSetItem).where(
            ProductionSetItem.production_set_id == set_id,
            ProductionSetItem.document_id.in_(document_ids),
        )
    )).scalars().all()
    rendered = 0
    for item in items:
        if item.output_path:
            continue  # idempotent retry / resume
        try:
            await render_item(db, ps, item)
            await db.commit()
            rendered += 1
        except Exception as exc:
            logger.exception("Render failed for set %s doc %s", set_id, item.document_id)
            ps.render_status = "error"
            ps.render_error = str(exc)
            await db.commit()
            return rendered
    return rendered


async def finalize_if_complete(db: AsyncSession, set_id: int) -> bool:
    ps = await db.get(ProductionSet, set_id)
    if not ps or ps.render_status != "rendering":
        return False
    remaining = (await db.execute(
        select(func.count(ProductionSetItem.id)).where(
            ProductionSetItem.production_set_id == set_id,
            ProductionSetItem.output_path.is_(None),
        )
    )).scalar() or 0
    if remaining:
        return False
    ps.render_status = "rendered"
    ps.render_error = None
    ps.rendered_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await db.commit()
    return True
