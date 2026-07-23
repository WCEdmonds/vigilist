"""Finding I1: mbox/pst/ost coherence between the email filter (search +
document-list filtering) and the `.potx` presentation grouping in
``get_file_type``.

Regression guard: mbox/pst-derived messages carry a native_path ending in
``.mbox``/``.pst``/``.ost`` (the container extension, since individual
messages inside these containers don't have their own file extension in the
same sense .eml/.msg do). Before this fix those files matched neither the
"email" filter dropdown nor the "email" display category and fell through to
"other" — invisible to anyone filtering a production by file type.
"""

from app.schemas import get_file_type
from app.services.search import FILE_TYPE_EXTENSIONS as SEARCH_FILE_TYPE_EXTENSIONS
from app.routers.documents import FILE_TYPE_EXTENSIONS as DOCUMENTS_FILE_TYPE_EXTENSIONS


def test_mbox_native_path_matches_search_email_filter():
    exts = SEARCH_FILE_TYPE_EXTENSIONS["email"]
    assert ".mbox" in exts
    assert ".pst" in exts
    assert ".ost" in exts
    # Legacy extensions still present.
    assert ".msg" in exts
    assert ".eml" in exts


def test_mbox_native_path_matches_documents_filter_email_bucket():
    """documents.py keeps its own copy of FILE_TYPE_EXTENSIONS (used by the
    document-list file_type filter, a separate code path from search) — it
    must stay in sync with search.py's mapping or mbox/pst files disappear
    from the document-list filter while still matching in search."""
    exts = DOCUMENTS_FILE_TYPE_EXTENSIONS["email"]
    assert ".mbox" in exts
    assert ".pst" in exts
    assert ".ost" in exts


def test_get_file_type_groups_mbox_pst_ost_as_email():
    assert get_file_type("archive.mbox", 0) == "email"
    assert get_file_type("mailbox.pst", 0) == "email"
    assert get_file_type("mailbox.ost", 0) == "email"


def test_get_file_type_groups_potx_as_presentation():
    assert get_file_type("template.potx", 0) == "presentation"
