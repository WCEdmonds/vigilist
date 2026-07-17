"""Unit tests for smart load-file parsing (no DB, no network)."""

import os
import tempfile

from app.utils.loadfile import detect_delimiter, detect_encoding, parse_loadfile

THORN = "þ"   # þ  field wrapper
DC4 = "\x14"       # field separator (Concordance)


def _write(tmp_path, name, text, encoding="utf-8-sig"):
    p = os.path.join(tmp_path, name)
    with open(p, "w", encoding=encoding, newline="") as f:
        f.write(text)
    return p


def test_detect_encoding_bom():
    assert detect_encoding("x".encode("utf-8-sig")) == "utf-8-sig"
    assert detect_encoding("x".encode("utf-16")) in ("utf-16", "utf-16-le", "utf-16-be")
    assert detect_encoding("plain ascii".encode("utf-8")) == "utf-8"


def test_detect_delimiter():
    assert detect_delimiter(f"{THORN}A{THORN}{DC4}{THORN}B{THORN}") == DC4
    assert detect_delimiter("A,B,C") == ","
    assert detect_delimiter("A\tB\tC") == "\t"
    assert detect_delimiter("A|B|C") == "|"


def test_parse_concordance_dat():
    with tempfile.TemporaryDirectory() as tmp:
        header = DC4.join(f"{THORN}{h}{THORN}" for h in ["Begin Bates", "Custodian"])
        row = DC4.join(f"{THORN}{v}{THORN}" for v in ["ABC-1", "Smith, J"])
        path = _write(tmp, "load.dat", f"{header}\r\n{row}\r\n")
        parsed = parse_loadfile(path)
        assert parsed.headers == ["Begin Bates", "Custodian"]
        assert parsed.total_rows == 1
        assert parsed.sample_rows[0] == {"Begin Bates": "ABC-1", "Custodian": "Smith, J"}


def test_parse_csv():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(tmp, "load.csv", "BegBates,Custodian\r\nABC-1,Jones\r\n", encoding="utf-8")
        parsed = parse_loadfile(path)
        assert parsed.headers == ["BegBates", "Custodian"]
        assert parsed.sample_rows[0]["Custodian"] == "Jones"


# ---------------------------------------------------------------------------
# Fix 1: round-trip tests for all brief-specified formats
# ---------------------------------------------------------------------------

def test_parse_utf16():
    """UTF-16 BOM file must parse without corruption."""
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(tmp, "load_utf16.csv", "Name,Value\r\nAlpha,Beta\r\n", encoding="utf-16")
        parsed = parse_loadfile(path)
        assert parsed.encoding == "utf-16"
        assert parsed.headers == ["Name", "Value"]
        assert parsed.total_rows == 1
        assert parsed.sample_rows[0] == {"Name": "Alpha", "Value": "Beta"}


def test_parse_cp1252():
    """cp1252 file with byte 0x96 (en-dash in cp1252, invalid in UTF-8) must survive round-trip."""
    with tempfile.TemporaryDirectory() as tmp:
        # Write raw bytes: 0x96 is the en-dash in cp1252 and is not valid UTF-8.
        # In Python's cp1252 codec, the Unicode en-dash U+2013 (–) encodes to byte 0x96.
        raw_bytes = "Name,Note\r\nSmith,en–dash\r\n".encode("cp1252")
        p = os.path.join(tmp, "load_cp1252.csv")
        with open(p, "wb") as f:
            f.write(raw_bytes)
        parsed = parse_loadfile(p)
        assert parsed.encoding == "cp1252"
        assert parsed.headers == ["Name", "Note"]
        assert parsed.total_rows == 1
        # The en-dash (U+2013) must survive: decoded from cp1252 byte 0x96
        assert "–" in parsed.sample_rows[0]["Note"]


def test_detect_encoding_cp1252_fallback():
    """detect_encoding falls back to cp1252 for bytes that are not valid UTF-8."""
    assert detect_encoding(b"\x96 Name") == "cp1252"


def test_parse_tab_delimited():
    """Tab-delimited file parses correctly."""
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(tmp, "load.tsv", "BegBates\tCustodian\r\nABC-1\tJones\r\n", encoding="utf-8")
        parsed = parse_loadfile(path)
        assert parsed.delimiter == "\t"
        assert parsed.headers == ["BegBates", "Custodian"]
        assert parsed.total_rows == 1
        assert parsed.sample_rows[0] == {"BegBates": "ABC-1", "Custodian": "Jones"}


def test_parse_pipe_delimited():
    """Pipe-delimited file parses correctly."""
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(tmp, "load.psv", "BegBates|Custodian\r\nABC-1|Jones\r\n", encoding="utf-8")
        parsed = parse_loadfile(path)
        assert parsed.delimiter == "|"
        assert parsed.headers == ["BegBates", "Custodian"]
        assert parsed.total_rows == 1
        assert parsed.sample_rows[0] == {"BegBates": "ABC-1", "Custodian": "Jones"}


def test_parse_header_only():
    """Header-only file (no data rows) returns populated headers and empty sample/total."""
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(tmp, "headers_only.csv", "BegBates,Custodian\r\n", encoding="utf-8")
        parsed = parse_loadfile(path)
        assert parsed.headers == ["BegBates", "Custodian"]
        assert parsed.sample_rows == []
        assert parsed.total_rows == 0


# ---------------------------------------------------------------------------
# Fix 2: detect_delimiter guard tests
# ---------------------------------------------------------------------------

def test_detect_delimiter_single_column():
    """Single-column header (no delimiter chars) must return the documented default (comma)."""
    # No tab, pipe, or DC4 in the line — all counts are 0
    result = detect_delimiter("BegBates")
    assert result == ","


def test_detect_delimiter_no_count_zero_wins():
    """A delimiter whose count is 0 must not beat a delimiter whose count is >0."""
    # Tab appears twice; comma/pipe do not appear at all
    result = detect_delimiter("A\tB\tC")
    assert result == "\t"
