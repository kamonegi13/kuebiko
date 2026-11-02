"""backfill_summary_embeddings の --ids-file の読み取り。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from backfill_summary_embeddings import read_ids  # noqa: E402


def test_read_ids_strips_blanks_and_dedupes_in_order(tmp_path: Path) -> None:
    # Arrange
    f = tmp_path / "ids.txt"
    f.write_text("a1\n\n  b2 \na1\nc3\n", encoding="utf-8")

    # Act
    ids = read_ids(f)

    # Assert
    assert ids == ["a1", "b2", "c3"]


def test_read_ids_empty_file(tmp_path: Path) -> None:
    f = tmp_path / "ids.txt"
    f.write_text("\n\n", encoding="utf-8")
    assert read_ids(f) == []
