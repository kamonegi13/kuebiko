"""評価資産エクスポート (ラベル凍結資産 + goldset) のテスト。"""

from __future__ import annotations

import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.storage.asset_export import export_eval_assets
from src.storage.run_history import RunHistoryRepository

_NOW = datetime(2026, 8, 22, 0, 10, 0, tzinfo=UTC)


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "assets.db")


def _seed_label(repo: RunHistoryRepository) -> None:
    """凍結資産のラベル 1 行を直接投入する (producer は撤収済で API を持たない)。"""
    with repo._connect() as conn:  # noqa: SLF001 — テスト専用の直接投入
        conn.execute(
            "INSERT INTO tuning_labels"
            " (dedup_key, article_id, field, label_value, source, strength,"
            "  arrived_at, provenance, snapshot)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("k1", "a1", "subject_actor", "qilin", "E1", "strong", _NOW.isoformat(), "{}", None),
        )


class TestEvalAssetExport:
    def test_exports_labels_and_goldset_with_rotation(
        self, repo: RunHistoryRepository, tmp_path: Path
    ) -> None:
        # Arrange
        _seed_label(repo)
        goldset = tmp_path / "goldset.jsonl"
        goldset.write_text('{"article_id": "g1"}\n', encoding="utf-8")
        backup_dir = tmp_path / "backups"

        # Act
        out = export_eval_assets(
            repo, backup_dir=backup_dir, goldset_path=goldset, now=_NOW, keep=2
        )

        # Assert
        assert out is not None and out.exists()
        with tarfile.open(out) as tar:
            names = set(tar.getnames())
        assert {"tuning_labels.jsonl", "tuning_evals.jsonl", "goldset.jsonl"} <= names
        # 撤収したパネル系は dump 対象から外れている
        assert "panel_verdicts.jsonl" not in names

    def test_rotation_drops_oldest_generation(
        self, repo: RunHistoryRepository, tmp_path: Path
    ) -> None:
        # Arrange
        _seed_label(repo)
        goldset = tmp_path / "goldset.jsonl"
        goldset.write_text('{"article_id": "g1"}\n', encoding="utf-8")
        backup_dir = tmp_path / "backups"
        first = export_eval_assets(
            repo, backup_dir=backup_dir, goldset_path=goldset, now=_NOW, keep=2
        )
        assert first is not None

        # Act — keep=2 で 3 世代目まで作る
        for d in (1, 2):
            export_eval_assets(
                repo,
                backup_dir=backup_dir,
                goldset_path=goldset,
                now=_NOW + timedelta(days=d),
                keep=2,
            )

        # Assert
        exports = sorted(backup_dir.glob("eval_assets_*.tar.gz"))
        assert len(exports) == 2
        assert first.name not in {p.name for p in exports}

    def test_broken_repo_fails_open(self, tmp_path: Path) -> None:
        # Arrange
        class _Broken:
            def export_tuning_labels(self) -> list[dict[str, str]]:
                raise RuntimeError("db down")

        # Act / Assert — 資産退避の失敗は None (衛生バッチを止めない)
        assert (
            export_eval_assets(
                _Broken(), backup_dir=tmp_path / "b", goldset_path=tmp_path / "none", now=_NOW
            )
            is None
        )
