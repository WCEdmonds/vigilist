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
