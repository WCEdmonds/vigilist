"""Assemble the deliverable package for a rendered production set (P2-3)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import zipfile
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document, ProductionSet, ProductionSetItem
from app.services import storage
from app.services.loadfile_export import (
    check_continuity,
    dat_bytes,
    manifest_dict,
    opt_bytes,
)

logger = logging.getLogger(__name__)


def package_path_for(ps: ProductionSet) -> str:
    return (f"productions/{ps.production_id}/production_sets/{ps.id}/"
            f"package/{ps.prefix}_production.zip")


def _volume(ps: ProductionSet) -> str:
    return f"VOL{ps.id:03d}"


async def _docs_by_id(db: AsyncSession, items) -> dict:
    docs = (await db.execute(
        select(Document).where(Document.id.in_([i.document_id for i in items]))
    )).scalars().all()
    return {d.id: d for d in docs}


def _family_ranges(items, docs) -> dict:
    """family_id -> [min begin, max end]. Items arrive in sort_order and lock
    ordering keeps families contiguous, so first/last occurrence bound the range."""
    ranges: dict = {}
    for item in items:
        doc = docs.get(item.document_id)
        fam = doc.family_id if doc is not None else None
        if not fam:
            continue
        if fam not in ranges:
            ranges[fam] = [item.bates_begin, item.bates_end]
        else:
            ranges[fam][1] = item.bates_end
    return ranges


async def build_dat_rows(db: AsyncSession, ps: ProductionSet, items) -> list[dict]:
    docs = await _docs_by_id(db, items)
    fam_ranges = _family_ranges(items, docs)
    rows = []
    for item in items:
        doc = docs.get(item.document_id)
        withheld = item.disposition == "withhold"
        fam = doc.family_id if doc is not None else None
        if fam and fam in fam_ranges:
            beg_att, end_att = fam_ranges[fam]
        else:
            beg_att, end_att = item.bates_begin, item.bates_end
        has_text = (item.disposition == "produce" and doc is not None
                    and bool(doc.text_content))
        rows.append({
            "BEGBATES": item.bates_begin,
            "ENDBATES": item.bates_end,
            "BEGATTACH": beg_att,
            "ENDATTACH": end_att,
            "CUSTODIAN": getattr(doc, "custodian", None),
            "FROM": getattr(doc, "email_from", None),
            "TO": getattr(doc, "email_to", None),
            "CC": getattr(doc, "email_cc", None),
            "DATESENT": doc.date_sent.date().isoformat()
                        if doc is not None and doc.date_sent else "",
            "DATERECEIVED": doc.date_received.date().isoformat()
                            if doc is not None and doc.date_received else "",
            # Privilege safety: withheld rows carry log-equivalent metadata only.
            "SUBJECT": "" if withheld else getattr(doc, "email_subject", None),
            "FILENAME": "" if withheld else getattr(doc, "file_name", None),
            "FILETYPE": getattr(doc, "file_type", None),
            "MD5HASH": getattr(doc, "file_hash_md5", None),
            "SHA256HASH": getattr(doc, "file_hash_sha256", None),
            "PAGECOUNT": item.pages,
            "REDACTED": "Y" if item.disposition == "redact_in_part" else "N",
            "WITHHELD": "Y" if withheld else "N",
            "CONFIDENTIALITY": item.designation or ps.designation or "",
            "TEXTPATH": f".\\TEXT\\{item.bates_begin}.txt" if has_text else "",
        })
    return rows


def compute_manifest(ps: ProductionSet, items,
                     artifact_hashes: dict | None = None) -> dict:
    counts = {"documents": len(items), "pages": sum(i.pages or 0 for i in items),
              "produce": 0, "redact_in_part": 0, "withhold": 0}
    for i in items:
        if i.disposition in counts:
            counts[i.disposition] += 1
    bates_range = ({"begin": items[0].bates_begin, "end": items[-1].bates_end}
                   if items else {"begin": None, "end": None})
    errors = check_continuity(
        [(i.bates_begin, i.bates_end, i.pages or 0) for i in items],
        ps.prefix, ps.start_number)
    artifacts = []
    for i in items:
        entry = {"bates_begin": i.bates_begin, "path": i.output_path}
        if artifact_hashes and i.bates_begin in artifact_hashes:
            entry.update(artifact_hashes[i.bates_begin])
        artifacts.append(entry)
    ps_info = {"id": ps.id, "name": ps.name, "prefix": ps.prefix,
               "designation": ps.designation,
               "locked_at": ps.locked_at.isoformat() if ps.locked_at else None,
               "rendered_at": ps.rendered_at.isoformat() if ps.rendered_at else None}
    return manifest_dict(ps_info, counts, bates_range, errors, artifacts)


async def package_set(db: AsyncSession, set_id: int) -> None:
    """Packaging job body. Trigger endpoint already set package_status='packaging'.
    Failures land in package_status='error'; never raises (worker returns 200)."""
    ps = await db.get(ProductionSet, set_id)
    if not ps:
        return
    try:
        if ps.status != "locked" or ps.render_status != "rendered":
            raise RuntimeError("Production set is not rendered")
        items = (await db.execute(
            select(ProductionSetItem)
            .where(ProductionSetItem.production_set_id == set_id)
            .order_by(ProductionSetItem.sort_order)
        )).scalars().all()
        if not items:
            raise RuntimeError("Production set has no members")
        docs = await _docs_by_id(db, items)
        dat_rows = await build_dat_rows(db, ps, items)
        volume = _volume(ps)
        opt_entries = [(i.bates_begin, volume, f".\\PDFS\\{i.bates_begin}.pdf",
                        i.pages or 0) for i in items]

        tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        tmp_path = tmp.name
        tmp.close()
        try:
            hashes: dict = {}
            with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr(f"DATA/{ps.prefix}.dat", dat_bytes(dat_rows))
                zf.writestr(f"DATA/{ps.prefix}.opt", opt_bytes(opt_entries))
                for item in items:
                    if not item.output_path:
                        raise RuntimeError(
                            f"Missing rendered artifact for {item.bates_begin}")
                    try:
                        pdf = storage.get_download_bytes(item.output_path)
                    except Exception as exc:
                        raise RuntimeError(
                            f"Could not fetch artifact for {item.bates_begin}: {exc}")
                    hashes[item.bates_begin] = {
                        "sha256": hashlib.sha256(pdf).hexdigest(),
                        "bytes": len(pdf),
                    }
                    zf.writestr(f"PDFS/{item.bates_begin}.pdf", pdf)
                    doc = docs.get(item.document_id)
                    if (item.disposition == "produce" and doc is not None
                            and doc.text_content):
                        zf.writestr(f"TEXT/{item.bates_begin}.txt",
                                    doc.text_content.encode("utf-8"))
                manifest = compute_manifest(ps, items, hashes)
                zf.writestr("manifest.json",
                            json.dumps(manifest, indent=2).encode("utf-8"))
            path = package_path_for(ps)
            storage.upload_file(tmp_path, path, "application/zip")
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        ps.package_status = "packaged"
        ps.package_error = None
        ps.package_path = path
        ps.packaged_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await db.commit()
    except Exception as exc:
        logger.exception("Packaging failed for set %s", set_id)
        ps.package_status = "error"
        ps.package_error = str(exc)
        await db.commit()
