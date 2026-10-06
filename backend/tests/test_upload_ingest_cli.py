"""scripts.upload_ingest file collection mirrors the wizard's relative paths."""

import pytest

from scripts.upload_ingest import _collect


def test_collect_dir_keeps_relative_paths_and_file_uses_basename(tmp_path):
    box = tmp_path / "will@votethiru.com"
    (box / "sub").mkdir(parents=True)
    (box / "will-part02.mbox").write_bytes(b"x")
    (box / "will-part01.mbox").write_bytes(b"x")
    (box / "sub" / "a.eml").write_bytes(b"x")
    loose = tmp_path / "loose.eml"
    loose.write_bytes(b"x")

    rels = [rel for _local, rel in _collect([str(box), str(loose)])]
    assert rels == ["loose.eml", "sub/a.eml", "will-part01.mbox", "will-part02.mbox"]


def test_collect_missing_path_exits(tmp_path):
    with pytest.raises(SystemExit):
        _collect([str(tmp_path / "nope")])
