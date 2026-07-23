"""Round-trip tests: our DAT/OPT writers vs our own import parsers (P2-3)."""

from app.services.loadfile_export import (
    DAT_COLUMNS,
    check_continuity,
    dat_bytes,
    manifest_dict,
    opt_bytes,
)
from app.utils.parsers import parse_dat, parse_opt


def test_dat_round_trips_through_importer(tmp_path):
    rows = [{c: f"v {c}" for c in DAT_COLUMNS}]
    rows[0]["BEGBATES"] = "SMITH000001"
    p = tmp_path / "out.dat"
    p.write_bytes(dat_bytes(rows))
    parsed = parse_dat(str(p))
    assert len(parsed) == 1
    assert parsed[0]["BEGBATES"] == "SMITH000001"
    assert set(parsed[0]) == set(DAT_COLUMNS)
    assert parsed[0]["CUSTODIAN"] == "v CUSTODIAN"


def test_dat_bytes_format():
    data = dat_bytes([{"BEGBATES": "A1"}])
    assert data.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM
    text = data.decode("utf-8-sig")
    lines = text.split("\r\n")
    assert lines[0].startswith("þBEGBATESþ\x14")
    assert "þA1þ" in lines[1]
    assert text.endswith("\r\n")


def test_dat_missing_keys_become_empty():
    data = dat_bytes([{"BEGBATES": "A1"}])
    row = data.decode("utf-8-sig").split("\r\n")[1]
    assert len(row.split("\x14")) == len(DAT_COLUMNS)
    assert "þþ" in row  # empty wrapped field


def test_dat_strips_control_chars():
    data = dat_bytes([{"SUBJECT": "bad\x14value\r\nhereþ!"}])
    row = data.decode("utf-8-sig").split("\r\n")[1]
    assert len(row.split("\x14")) == len(DAT_COLUMNS)


def test_opt_round_trips_through_importer(tmp_path):
    entries = [("SMITH000001", "VOL001", ".\\PDFS\\SMITH000001.pdf", 3),
               ("SMITH000004", "VOL001", ".\\PDFS\\SMITH000004.pdf", 1)]
    p = tmp_path / "out.opt"
    p.write_bytes(opt_bytes(entries))
    parsed = parse_opt(str(p))
    assert parsed == {
        "SMITH000001": ["./PDFS/SMITH000001.pdf"],
        "SMITH000004": ["./PDFS/SMITH000004.pdf"],
    }


def test_opt_line_shape():
    data = opt_bytes([("A1", "VOL001", ".\\PDFS\\A1.pdf", 5)])
    assert data == b"A1,VOL001,.\\PDFS\\A1.pdf,Y,,,5\r\n"


def test_continuity_clean():
    items = [("P000001", "P000003", 3), ("P000004", "P000004", 1)]
    assert check_continuity(items, "P", 1) == []


def test_continuity_catches_gap_overlap_end_and_start():
    assert check_continuity([("P000002", "P000002", 1)], "P", 1)      # wrong start
    assert check_continuity([("P000001", "P000003", 2)], "P", 1)      # wrong end
    items = [("P000001", "P000002", 2), ("P000005", "P000005", 1)]
    assert check_continuity(items, "P", 1)                            # gap
    items = [("P000001", "P000002", 2), ("P000002", "P000002", 1)]
    assert check_continuity(items, "P", 1)                            # overlap
    assert check_continuity([], "P", 1) == ["production set has no members"]


def test_manifest_dict_shape():
    m = manifest_dict({"id": 1}, {"documents": 2}, {"begin": "A1", "end": "A2"},
                      [], [{"bates_begin": "A1"}])
    assert m["continuity"] == {"ok": True, "errors": []}
    assert m["production_set"] == {"id": 1}
    assert "generated_at" in m
    m2 = manifest_dict({}, {}, {}, ["gap"], [])
    assert m2["continuity"]["ok"] is False


def test_opt_paged_round_trips_through_importer(tmp_path):
    from app.services.loadfile_export import opt_bytes_paged

    docs = [("VOL001", [("P000001", ".\\VOL001\\IMAGES\\P000001.tif"),
                        ("P000002", ".\\VOL001\\IMAGES\\P000002.tif")]),
            ("VOL001", [("P000003", ".\\VOL001\\IMAGES\\P000003.tif")])]
    p = tmp_path / "paged.opt"
    p.write_bytes(opt_bytes_paged(docs))
    parsed = parse_opt(str(p))
    assert parsed == {
        "P000001": ["./VOL001/IMAGES/P000001.tif", "./VOL001/IMAGES/P000002.tif"],
        "P000003": ["./VOL001/IMAGES/P000003.tif"],
    }
