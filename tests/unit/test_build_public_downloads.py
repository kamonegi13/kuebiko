"""公開サイトの配布物 (scripts/build_public_downloads.py) の組み込み。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from build_public_downloads import PAGES_FILE_LIMIT, DownloadsError, build  # noqa: E402


def test_copies_pdfs_into_downloads(tmp_path: Path) -> None:
    src, out = tmp_path / "src", tmp_path / "out"
    src.mkdir()
    (src / "hunt-operations.pdf").write_bytes(b"%PDF-1.7 a")
    (src / "cyber-estimate.pdf").write_bytes(b"%PDF-1.7 b")

    names = build(src, out)

    assert names == ["cyber-estimate.pdf", "hunt-operations.pdf"]
    assert (out / "downloads" / "hunt-operations.pdf").read_bytes() == b"%PDF-1.7 a"


def test_missing_source_dir_is_noop(tmp_path: Path) -> None:
    assert build(tmp_path / "none", tmp_path / "out") == []
    assert not (tmp_path / "out" / "downloads").exists()


def test_ignores_hidden_files(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / ".DS_Store").write_bytes(b"x")
    (src / "a.pdf").write_bytes(b"%PDF")
    assert build(src, tmp_path / "out") == ["a.pdf"]


@pytest.mark.parametrize("name", ["米陸軍ハント作戦.pdf", "notes.txt", "has space.pdf", "-x.pdf"])
def test_rejects_unsafe_names_without_copying_anything(tmp_path: Path, name: str) -> None:
    src, out = tmp_path / "src", tmp_path / "out"
    src.mkdir()
    (src / "ok.pdf").write_bytes(b"%PDF")
    (src / name).write_bytes(b"x")

    with pytest.raises(DownloadsError):
        build(src, out)
    assert not (out / "downloads").exists()


def test_rejects_file_over_pages_limit(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    with (src / "big.pdf").open("wb") as fh:
        fh.truncate(PAGES_FILE_LIMIT + 1)

    with pytest.raises(DownloadsError, match="25 MiB"):
        build(src, tmp_path / "out")
