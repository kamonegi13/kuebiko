"""較正格子 §13 対処: 恒久資産エクスポート (§13-3) とシャドー台帳 (§13-8) のテスト。"""

from __future__ import annotations

import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.storage.run_history import RunHistoryRepository
from src.tuning.asset_export import export_tuning_assets
from src.tuning.shadow_registry import ShadowEntry, shadow_status_lines

_NOW = datetime(2026, 8, 22, 0, 10, 0, tzinfo=UTC)


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "assets.db")


class TestAssetExport:
    def test_exports_labels_and_goldset_with_rotation(
        self, repo: RunHistoryRepository, tmp_path: Path
    ) -> None:
        repo.record_tuning_label(
            dedup_key="k1",
            field="subject_actor",
            label_value="qilin",
            source="E1",
            strength="strong",
            provenance="{}",
            snapshot='{"title": "t"}',
        )
        goldset = tmp_path / "goldset.jsonl"
        goldset.write_text('{"article_id": "g1"}\n', encoding="utf-8")
        backup_dir = tmp_path / "backups"

        out = export_tuning_assets(
            repo, backup_dir=backup_dir, goldset_path=goldset, now=_NOW, keep=2
        )
        assert out is not None and out.exists()
        with tarfile.open(out) as tar:
            names = set(tar.getnames())
        assert {"tuning_labels.jsonl", "goldset.jsonl", "panel_verdicts.jsonl"} <= names

        # rotation: keep=2 で 3 世代目を作ると最古が消える
        for d in (1, 2):
            export_tuning_assets(
                repo,
                backup_dir=backup_dir,
                goldset_path=goldset,
                now=_NOW + timedelta(days=d),
                keep=2,
            )
        exports = sorted(backup_dir.glob("tuning_assets_*.tar.gz"))
        assert len(exports) == 2
        assert out.name not in {p.name for p in exports}  # 最古 (初回) が rotate out

    def test_broken_repo_fails_open(self, tmp_path: Path) -> None:
        class _Broken:
            def export_tuning_labels(self) -> list[dict[str, str]]:
                raise RuntimeError("db down")

        assert (
            export_tuning_assets(
                _Broken(), backup_dir=tmp_path / "b", goldset_path=tmp_path / "none", now=_NOW
            )
            is None
        )


class TestShadowRegistry:
    def test_pending_shadow_shows_progress_and_deadline_escalates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        entry = ShadowEntry(
            name="test-part",
            started=_NOW.date() - timedelta(weeks=3),
            is_cutover="TEST_CUTOVER_FLAG",
            note="n",
            deadline_weeks=8,
        )
        monkeypatch.setattr("src.tuning.shadow_registry.SHADOW_REGISTRY", (entry,))
        monkeypatch.delenv("TEST_CUTOVER_FLAG", raising=False)
        lines = shadow_status_lines(now=_NOW)
        assert len(lines) == 1 and "3/8 週" in lines[0]

        # 期限超過 → 3 択の強制起票文
        late = shadow_status_lines(now=_NOW + timedelta(weeks=6))
        assert "期限超過" in late[0] and "cutover / 破棄 / 延長" in late[0]

        # cutover 済み (flag=1) は台帳から消える
        monkeypatch.setenv("TEST_CUTOVER_FLAG", "1")
        assert shadow_status_lines(now=_NOW) == []
