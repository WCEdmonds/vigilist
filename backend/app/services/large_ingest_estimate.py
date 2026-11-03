"""Time and cost estimate for loading a surveyed large container.

Shown to the uploader before any paid work starts. Every constant is a rough,
documented assumption; the load estimate is meant to catch an order-of-magnitude
surprise (the $100 rule), not to bill to the cent.
"""

from __future__ import annotations

from app.services.pst_reader import ContainerSurvey

# Worker compute per item: ~1.7 s of a 2 vCPU / 4 GiB Cloud Run instance
# (~$0.000058/s). Provisional until the U1 throughput spike replaces it.
COMPUTE_USD_PER_ITEM = 0.0001
# Items per second one worker request sustains. Provisional (U1 spike).
ITEMS_PER_SECOND_PER_WORKER = 2.0
# Concurrent chunk requests across the worker fleet (instances x concurrency).
# Provisional (Task 12 sizes the worker).
PARALLEL_WORKERS = 20
# Share of a message's bytes that ends up as extracted text to embed.
TEXT_BYTES_FRACTION = 0.15
# Voyage voyage-3: $0.06 per 1M tokens at ~4 bytes of text per token.
VOYAGE_USD_PER_TEXT_BYTE = 0.06 / 1_000_000 / 4
# Share of a message's bytes that are attachments.
ATTACHMENT_BYTES_FRACTION = 0.8
# Spec assumption: 10% of attachment bytes are images over the tiny-image
# threshold and so are sent to OCR.
OCR_IMAGE_BYTES_FRACTION = 0.10
# Average size of one OCR'd image (one Vision request each).
AVG_OCR_IMAGE_BYTES = 200_000
# Cloud Vision text detection: $1.50 per 1,000 images.
VISION_USD_PER_IMAGE = 1.50 / 1000
# The user's rule: check before a load costs more than this.
BUDGET_USD = 100.0


def selected_totals(survey: ContainerSurvey, types: set[str], include_recoverable: bool) -> tuple[int, int]:
    """(items, bytes) the selection covers."""
    items = size = 0
    for f in survey.folders:
        if f.recoverable and not include_recoverable:
            continue
        for t in types:
            items += int(f.counts.get(t, 0))
            size += int(f.bytes.get(t, 0))
    return items, size


def estimate_load(survey: ContainerSurvey, types: set[str], include_recoverable: bool) -> dict:
    items, size = selected_totals(survey, set(types), include_recoverable)
    compute = items * COMPUTE_USD_PER_ITEM
    embeddings = size * TEXT_BYTES_FRACTION * VOYAGE_USD_PER_TEXT_BYTE
    ocr_images = size * ATTACHMENT_BYTES_FRACTION * OCR_IMAGE_BYTES_FRACTION / AVG_OCR_IMAGE_BYTES
    ocr = ocr_images * VISION_USD_PER_IMAGE
    usd = round(compute + embeddings + ocr, 2)
    minutes = round(items / (ITEMS_PER_SECOND_PER_WORKER * PARALLEL_WORKERS) / 60, 1)
    return {
        "items": items,
        "bytes": size,
        "minutes": minutes,
        "usd": usd,
        "storage_gb": round(size / 1e9, 2),
        "over_budget": usd > BUDGET_USD,
    }
