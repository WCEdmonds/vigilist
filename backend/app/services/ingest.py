"""Production ingest pipeline."""

import logging
import os
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Document, Production
from app.services.images import convert_document_images
from app.services.ai import generate_titles_batch
from app.utils.parsers import parse_dat, parse_opt

logger = logging.getLogger(__name__)

# Known DAT field names that map to document columns
FIELD_MAP = {
    "Begin Bates": "bates_begin",
    "End Bates": "bates_end",
    "Page Count": "page_count",
    "Text Link": "text_link",
    "Native Link": "native_link",
}


async def ingest_production(
    db: AsyncSession,
    production_name: str,
    production_root: str,
    description: str = "",
) -> dict:
    """Ingest a full production from disk into the database.

    Returns a summary dict with counts and any validation errors.
    """
    production_root = os.path.abspath(production_root)
    errors: list[str] = []

    # Find DAT and OPT files
    data_dir = os.path.join(production_root, "DATA")
    dat_files = list(Path(data_dir).glob("*.dat")) if os.path.isdir(data_dir) else []
    opt_files = list(Path(data_dir).glob("*.opt")) if os.path.isdir(data_dir) else []

    if not dat_files:
        raise FileNotFoundError(f"No .dat file found in {data_dir}")
    if not opt_files:
        raise FileNotFoundError(f"No .opt file found in {data_dir}")

    dat_path = str(dat_files[0])
    opt_path = str(opt_files[0])

    # Parse files
    dat_records = parse_dat(dat_path)
    opt_pages = parse_opt(opt_path)

    logger.info(f"Parsed {len(dat_records)} documents from DAT, {len(opt_pages)} documents from OPT")

    # Create or get production
    production = Production(name=production_name, description=description)
    db.add(production)
    await db.flush()

    # Set up image output directory
    converted_dir = os.path.join(settings.storage_root, "converted", production_name)
    os.makedirs(converted_dir, exist_ok=True)

    documents = []
    for i, record in enumerate(dat_records):
        bates_begin = record.get("Begin Bates", "").strip()
        bates_end = record.get("End Bates", "").strip()
        page_count_str = record.get("Page Count", "1").strip()
        text_link = record.get("Text Link", "").strip()
        native_link = record.get("Native Link", "").strip()

        if not bates_begin:
            errors.append(f"Row {i+1}: missing Begin Bates")
            continue

        page_count = int(page_count_str) if page_count_str.isdigit() else 1

        # Read extracted text
        text_content = None
        if text_link:
            text_path = os.path.join(production_root, text_link)
            if os.path.exists(text_path):
                with open(text_path, "r", encoding="utf-8-sig", errors="replace") as f:
                    text_content = f.read()
                # Strip null bytes — Postgres rejects 0x00 in text columns
                text_content = text_content.replace("\x00", "")
            else:
                errors.append(f"{bates_begin}: text file not found: {text_link}")

        # Get image paths from OPT and convert to JPEG
        raw_image_paths = opt_pages.get(bates_begin, [])
        if not raw_image_paths:
            errors.append(f"{bates_begin}: no images found in OPT file")

        jpeg_paths = convert_document_images(
            raw_image_paths, production_root, converted_dir
        )

        # Build metadata dict from all DAT fields (for flexibility)
        metadata = {}
        for key, value in record.items():
            if key not in FIELD_MAP and value:
                metadata[key] = value

        doc = Document(
            production_id=production.id,
            bates_begin=bates_begin,
            bates_end=bates_end,
            page_count=page_count,
            metadata_=metadata,
            text_content=text_content,
            native_path=native_link if native_link else None,
            image_paths=jpeg_paths,
        )
        documents.append(doc)

    db.add_all(documents)
    await db.flush()

    # Update tsvector for all new documents
    await db.execute(
        text(
            "UPDATE documents SET text_search_vector = to_tsvector('english', COALESCE(text_content, '')) "
            "WHERE production_id = :pid"
        ),
        {"pid": production.id},
    )

    # Generate AI titles for documents with text content
    if settings.anthropic_api_key:
        logger.info("Generating AI titles for %d documents...", len(documents))
        texts = [(str(doc.id), doc.text_content) for doc in documents]
        titles = await generate_titles_batch(texts)
        for doc in documents:
            title = titles.get(str(doc.id))
            if title:
                doc.title = title
        await db.flush()
        logger.info("AI titles generated for %d documents", sum(1 for t in titles.values() if t))

    await db.commit()

    return {
        "production_id": production.id,
        "production_name": production_name,
        "documents_ingested": len(documents),
        "errors": errors,
        "error_count": len(errors),
    }
