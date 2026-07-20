import fitz  # PyMuPDF

from app.services.ingest_pdf import derive_bates_prefix, iter_pdf_pages


def test_prefix_uppercases_first_token():
    assert derive_bates_prefix("Smith Loose Docs") == "SMITH"


def test_prefix_strips_punctuation():
    assert derive_bates_prefix("smith-jones, llp") == "SMITHJONES"


def test_prefix_truncates_to_12_chars():
    assert derive_bates_prefix("Supercalifragilistic Matter") == "SUPERCALIFRA"


def test_prefix_falls_back_to_doc_when_empty():
    assert derive_bates_prefix("!!! ???") == "DOC"
    assert derive_bates_prefix("") == "DOC"


def test_bates_stub_detection():
    from app.services.ingest_pdf import looks_like_bates_stub

    # Non-descriptive control/Bates stubs → eligible for AI retitling
    assert looks_like_bates_stub("SI001291")
    assert looks_like_bates_stub("SI001292")
    assert looks_like_bates_stub("ABC-000123")
    assert looks_like_bates_stub("0001234")
    assert looks_like_bates_stub("PROD_004567")

    # Human-meaningful filenames → preserved as-is
    assert not looks_like_bates_stub(
        "Jackson v. Bunch SI Responses to Discovery Requests (2024-10-04)"
    )
    assert not looks_like_bates_stub("10-16-24 JACKSON DEPO")
    assert not looks_like_bates_stub("Milton Jackson Interrogatory Responses")
    assert not looks_like_bates_stub("Complaint and Jury Demand")
    assert not looks_like_bates_stub("")


def _born_digital_pdf(text: str) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


def _blank_two_page_pdf() -> bytes:
    doc = fitz.open()
    doc.new_page()
    doc.new_page()
    data = doc.tobytes()
    doc.close()
    return data


def test_born_digital_uses_embedded_text_and_skips_ocr():
    ocr_calls = []

    def fake_ocr(jpeg_bytes: bytes) -> str:
        ocr_calls.append(jpeg_bytes)
        return "SHOULD-NOT-BE-USED"

    pages = list(iter_pdf_pages(_born_digital_pdf("Hello discovery"), ocr_fn=fake_ocr))

    assert len(pages) == 1
    page_num, jpeg, text = pages[0]
    assert page_num == 1
    assert jpeg[:3] == b"\xff\xd8\xff"  # JPEG magic bytes
    assert "Hello discovery" in text
    assert ocr_calls == []  # OCR not invoked for born-digital text


def test_scanned_page_falls_back_to_ocr():
    def fake_ocr(jpeg_bytes: bytes) -> str:
        return "OCR-RECOVERED-TEXT"

    pages = list(iter_pdf_pages(_blank_two_page_pdf(), ocr_fn=fake_ocr))

    assert len(pages) == 2
    combined = "\n\n".join(text for _, _, text in pages)
    assert combined.count("OCR-RECOVERED-TEXT") == 2


def test_pages_rendered_for_every_page():
    pages = list(iter_pdf_pages(_blank_two_page_pdf(), ocr_fn=lambda b: ""))
    assert [p[0] for p in pages] == [1, 2]  # page numbers, every page yielded


from app.services import ingest_pdf as pdf_mod
from app.services.ingest_pdf import list_pdf_sources, process_pdf_record


def test_list_pdf_sources_sorts_and_keeps_relative_path(monkeypatch):
    raw = [
        "productions/7/raw/B/second.pdf",
        "productions/7/raw/A/first.PDF",
        "productions/7/raw/notes.txt",
        "productions/7/raw/A/skip.opt",
    ]
    monkeypatch.setattr(pdf_mod, "list_files", lambda prefix: raw)

    items = list_pdf_sources(7)

    # Only PDFs, case-insensitive, sorted by storage path
    assert [i["storage_path"] for i in items] == [
        "productions/7/raw/A/first.PDF",
        "productions/7/raw/B/second.pdf",
    ]
    assert items[0]["relative_path"] == "A/first.PDF"
    assert items[0]["filename"] == "first.PDF"


def test_job_errors_update_sql_binds_both_params():
    """Regression: the errors UPDATE must bind both :errs and :jid.

    The old ``errors = :errs::jsonb`` form was mis-parsed by SQLAlchemy —
    the ``::jsonb`` cast swallowed the ``:errs`` bind, so a literal ``:errs``
    reached asyncpg and every batch died with ``PostgresSyntaxError: syntax
    error at or near ":"``. Cloud Tasks then retried each batch up to 100×,
    inflating skipped_files and stalling the job forever.
    """
    from sqlalchemy import text
    from sqlalchemy.dialects import postgresql

    from app.services.ingest import _UPDATE_JOB_ERRORS_SQL

    compiled = str(text(_UPDATE_JOB_ERRORS_SQL).compile(dialect=postgresql.asyncpg.dialect()))

    # Both named params must be converted to positional asyncpg placeholders;
    # no literal ":errs"/":jid" may survive into the SQL sent to Postgres.
    assert ":errs" not in compiled
    assert ":jid" not in compiled


def test_process_pdf_record_assembles_document(monkeypatch):
    item = {
        "storage_path": "productions/7/raw/A/first.pdf",
        "relative_path": "A/first.pdf",
        "filename": "first.pdf",
    }

    monkeypatch.setattr(pdf_mod, "get_download_bytes", lambda path: b"%PDF-fake")
    monkeypatch.setattr(
        pdf_mod,
        "iter_pdf_pages",
        lambda pdf_bytes, ocr_fn, dpi=pdf_mod.RENDER_DPI: iter(
            [
                (1, b"\xff\xd8jpeg1", "extracted text"),
                (2, b"\xff\xd8jpeg2", ""),
            ]
        ),
    )
    uploaded = []
    monkeypatch.setattr(
        pdf_mod,
        "upload_bytes",
        lambda data, remote, content_type=None: uploaded.append(remote) or remote,
    )

    errors: list[str] = []
    doc = process_pdf_record(
        production_id=7,
        item=item,
        global_index=0,
        prefix="SMITH",
        errors=errors,
    )

    assert doc.bates_begin == "SMITH 000001"
    assert doc.bates_end == "SMITH 000001"
    assert doc.page_count == 2
    assert doc.title == "first"
    assert doc.text_content == "extracted text"
    assert doc.metadata_["File Name"] == "first.pdf"
    assert doc.metadata_["Folder"] == "A"
    assert doc.native_path == "productions/7/raw/A/first.pdf"
    assert len(doc.image_paths) == 2
    assert len(uploaded) == 2
    assert errors == []


def test_inline_ingest_fails_job_when_no_sources_found(monkeypatch):
    """Regression: zero PDF sources must fail the job, not strand it.

    With total_files == 0 the batch loop never runs, so nothing finalizes
    the job — it sat in "processing" forever while the UI showed
    "0 / 0 total". Seen live when the browser uploaded to a different
    storage bucket than the backend was configured to read.
    """
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from app.services.ingest import ingest_from_storage

    monkeypatch.setattr(pdf_mod, "list_pdf_sources", lambda production_id: [])

    job = MagicMock()
    job.source_format = "generic_pdf"
    job.status = "processing"
    job.errors = []
    job.completed_at = None

    db = MagicMock()
    db.get = AsyncMock(return_value=job)
    db.commit = AsyncMock()

    asyncio.run(ingest_from_storage(db, "job-1", 7, "SMITH_PROD001"))

    assert job.status == "failed"
    assert any("No ingestable files" in e for e in job.errors)
    assert job.completed_at is not None
    db.commit.assert_awaited()
