"""Assemble the deliverable package for a rendered production set (P2-3/P2-5).

Zip layout is volume-first: VOL001/PDFS|IMAGES, VOL001/NATIVES, VOL001/TEXT
per volume (size-capped by ps.volume_max_mb; one volume when NULL), with
DATA/{prefix}.dat|.opt and manifest.json at the root. Load-file paths
include the volume directory.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document, ProductionSet, ProductionSetItem
from app.services import storage
from app.services.loadfile_export import (
    check_continuity,
    dat_bytes,
    manifest_dict,
    opt_bytes,
    opt_bytes_paged,
)

logger = logging.getLogger(__name__)


def package_path_for(ps: ProductionSet) -> str:
    return (f"productions/{ps.production_id}/production_sets/{ps.id}/"
            f"package/{ps.prefix}_production.zip")


def _fetch_bytes(path: str) -> bytes:
    """productions/ prefix -> GCS; anything else is a local dev path."""
    if path.startswith("productions/"):
        return storage.get_download_bytes(path)
    with open(Path(path.replace("\\", "/")), "rb") as f:
        return f.read()


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


async def build_dat_rows(db: AsyncSession, ps: ProductionSet, items,
                         doc_paths: dict | None = None) -> list[dict]:
    """doc_paths (optional): document_id -> {"textpath", "nativelink"} as
    written into the package (volume-prefixed); defaults keep the plain
    layout for direct callers/tests."""
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
        info = (doc_paths or {}).get(item.document_id, {})
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
            "TEXTPATH": info.get(
                "textpath",
                f".\\TEXT\\{item.bates_begin}.txt" if has_text else ""),
            "NATIVELINK": info.get("nativelink", ""),
        })
    return rows


def compute_manifest(ps: ProductionSet, items,
                     artifact_hashes: dict | None = None,
                     volumes: list | None = None) -> dict:
    counts = {"documents": len(items), "pages": sum(i.pages or 0 for i in items),
              "produce": 0, "redact_in_part": 0, "withhold": 0}
    for i in items:
        if i.disposition in counts:
            counts[i.disposition] += 1
    counts["native"] = sum(1 for i in items if getattr(i, "produce_native", False))
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
               "image_format": getattr(ps, "image_format", "pdf"),
               "locked_at": ps.locked_at.isoformat() if ps.locked_at else None,
               "rendered_at": ps.rendered_at.isoformat() if ps.rendered_at else None}
    manifest = manifest_dict(ps_info, counts, bates_range, errors, artifacts)
    manifest["volumes"] = volumes or []
    return manifest


async def package_set(db: AsyncSession, set_id: int) -> None:
    """Packaging job body. Trigger endpoint already set package_status='packaging'.
    Failures land in package_status='error'; never raises (worker returns 200)."""
    from app.services.endorse import page_bates_numbers
    from app.services.production_render import tiff_page_path

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
        tiff_mode = getattr(ps, "image_format", "pdf") == "tiff"
        max_bytes = (ps.volume_max_mb * 1024 * 1024) if ps.volume_max_mb else None

        tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        tmp_path = tmp.name
        tmp.close()
        try:
            hashes: dict = {}
            doc_paths: dict = {}
            opt_docs: list = []    # pdf mode: (bates, vol, path, pages)
            opt_paged: list = []   # tiff mode: (vol, [(page_bates, path)])
            volumes: list[dict] = []
            vol_num = 0
            cur_bytes = 0

            def next_volume() -> str:
                nonlocal vol_num, cur_bytes
                vol_num += 1
                cur_bytes = 0
                label = f"VOL{vol_num:03d}"
                volumes.append({"label": label, "documents": 0, "bytes": 0})
                return label

            vol = next_volume()
            with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for item in items:
                    if not item.output_path:
                        raise RuntimeError(
                            f"Missing rendered artifact for {item.bates_begin}")
                    doc = docs.get(item.document_id)

                    # -- gather this document's bytes --
                    if tiff_mode:
                        page_names = page_bates_numbers(
                            item.bates_begin, ps.prefix, ps.padding, item.pages or 1)
                        page_blobs = []
                        h = hashlib.sha256()
                        for page_bates in page_names:
                            try:
                                blob = storage.get_download_bytes(tiff_page_path(ps, page_bates))
                            except Exception as exc:
                                raise RuntimeError(
                                    f"Could not fetch artifact for {item.bates_begin}: {exc}")
                            page_blobs.append((page_bates, blob))
                            h.update(blob)
                        art_bytes = sum(len(b) for _, b in page_blobs)
                        digest = h.hexdigest()
                        pdf = None
                    else:
                        try:
                            pdf = storage.get_download_bytes(item.output_path)
                        except Exception as exc:
                            raise RuntimeError(
                                f"Could not fetch artifact for {item.bates_begin}: {exc}")
                        art_bytes = len(pdf)
                        digest = hashlib.sha256(pdf).hexdigest()

                    native_blob = native_name = None
                    if (getattr(item, "produce_native", False) and doc is not None
                            and doc.native_path):
                        try:
                            native_blob = _fetch_bytes(doc.native_path)
                        except Exception as exc:
                            raise RuntimeError(
                                f"Could not fetch native for {item.bates_begin}: {exc}")
                        ext = os.path.splitext(doc.native_path)[1] or ""
                        native_name = f"{item.bates_begin}{ext}"

                    text_blob = None
                    if (item.disposition == "produce" and doc is not None
                            and doc.text_content):
                        text_blob = doc.text_content.encode("utf-8")

                    doc_size = (art_bytes + len(native_blob or b"")
                                + len(text_blob or b""))
                    if max_bytes and cur_bytes > 0 and cur_bytes + doc_size > max_bytes:
                        vol = next_volume()
                    cur_bytes += doc_size
                    volumes[-1]["documents"] += 1
                    volumes[-1]["bytes"] += doc_size

                    # -- write under the volume directory --
                    info: dict = {}
                    if tiff_mode:
                        pages_paths = []
                        for page_bates, blob in page_blobs:
                            zf.writestr(f"{vol}/IMAGES/{page_bates}.tif", blob)
                            pages_paths.append(
                                (page_bates, f".\\{vol}\\IMAGES\\{page_bates}.tif"))
                        opt_paged.append((vol, pages_paths))
                    else:
                        zf.writestr(f"{vol}/PDFS/{item.bates_begin}.pdf", pdf)
                        opt_docs.append(
                            (item.bates_begin, vol,
                             f".\\{vol}\\PDFS\\{item.bates_begin}.pdf",
                             item.pages or 0))
                    if native_blob is not None:
                        zf.writestr(f"{vol}/NATIVES/{native_name}", native_blob)
                        info["nativelink"] = f".\\{vol}\\NATIVES\\{native_name}"
                    if text_blob is not None:
                        zf.writestr(f"{vol}/TEXT/{item.bates_begin}.txt", text_blob)
                        info["textpath"] = f".\\{vol}\\TEXT\\{item.bates_begin}.txt"
                    doc_paths[item.document_id] = info
                    hashes[item.bates_begin] = {"sha256": digest, "bytes": art_bytes}

                dat_rows = await build_dat_rows(db, ps, items, doc_paths)
                zf.writestr(f"DATA/{ps.prefix}.dat", dat_bytes(dat_rows))
                if tiff_mode:
                    zf.writestr(f"DATA/{ps.prefix}.opt", opt_bytes_paged(opt_paged))
                else:
                    zf.writestr(f"DATA/{ps.prefix}.opt", opt_bytes(opt_docs))
                manifest = compute_manifest(ps, items, hashes, volumes=volumes)
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
