"""§13-1 残対処 + 不変条件14: E1 供給ドリフト番兵のテスト。

閉ループ (突合キー = LLM 出力) の縮退は「新規 0 件」が正常に見える —
供給量の週次比較で崩落を警告できることを固定する。

不変条件14 (§5 ソース独立性、2026-08-22) 適用後は分母が「独立ラベルのみ」
(strength <> 'self_source') に層別される — baseline (過去週) も同じ基準で
再計算されないと、独立性強制の直後に旧分母 (自己突合込み) と新分母 (独立のみ) を
比較して偽の崩落警報が出てしまう。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.storage.run_history import RunHistoryRepository
from src.tuning.supply_sentinel import supply_drift_lines

# 月曜 00:00 に揃えた基準時刻 (週バケツ境界を明示)
_NOW = datetime(2026, 8, 24, 9, 0, 0, tzinfo=UTC)  # 月曜


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "sentinel.db")


def _seed_week(
    repo: RunHistoryRepository, weeks_ago: int, count: int, *, strength: str = "strong"
) -> None:
    """N 週前の週に count 件の E1 主体ラベルを置く。"""
    base = _NOW - timedelta(weeks=weeks_ago)
    for i in range(count):
        repo.record_tuning_label(
            dedup_key=f"w{weeks_ago}-{strength}-{i}",
            field="subject_actor",
            label_value="qilin",
            source="E1",
            strength=strength,
            provenance="{}",
            article_id=f"a-{weeks_ago}-{strength}-{i}",
            arrived_at=base + timedelta(hours=i),
        )


class TestSupplyDrift:
    def test_healthy_supply_reports_status_without_warning(
        self, repo: RunHistoryRepository
    ) -> None:
        for w in range(1, 6):
            _seed_week(repo, w, 8)
        lines = supply_drift_lines(repo, now=_NOW)
        assert len(lines) == 1
        assert "直近完全週 8 件" in lines[0]

    def test_collapse_is_warned(self, repo: RunHistoryRepository) -> None:
        _seed_week(repo, 1, 2)  # 直近完全週が崩落
        for w in range(2, 6):
            _seed_week(repo, w, 10)
        lines = supply_drift_lines(repo, now=_NOW)
        assert len(lines) == 2
        assert "崩落疑い" in lines[1]

    def test_thin_baseline_does_not_warn(self, repo: RunHistoryRepository) -> None:
        """元々供給が薄い (中央値 < 3) 週はノイズなので判定しない。"""
        _seed_week(repo, 1, 0)
        for w in range(2, 6):
            _seed_week(repo, w, 2)
        lines = supply_drift_lines(repo, now=_NOW)
        assert len(lines) == 1  # 警告なし

    def test_broken_repo_fails_open(self) -> None:
        class _Broken:
            def list_tuning_labels(self, **kw: object) -> list[dict[str, str]]:
                raise RuntimeError("db down")

        assert supply_drift_lines(_Broken(), now=_NOW) == []

    def test_status_line_notes_independence_basis(self, repo: RunHistoryRepository) -> None:
        for w in range(1, 6):
            _seed_week(repo, w, 8)
        lines = supply_drift_lines(repo, now=_NOW)
        assert "不変条件 14" in lines[0]


class TestSourceIndependenceStratification:
    """不変条件14 (§5、2026-08-22): 分母を独立ラベルのみに層別する。"""

    def test_self_source_labels_are_excluded_from_latest_week(
        self, repo: RunHistoryRepository
    ) -> None:
        # 独立ラベルは薄いが、自己突合を足せば健全に見えてしまうケース
        _seed_week(repo, 1, 1, strength="strong")
        _seed_week(repo, 1, 20, strength="self_source")
        for w in range(2, 6):
            _seed_week(repo, w, 1, strength="strong")
        lines = supply_drift_lines(repo, now=_NOW)
        assert "直近完全週 1 件" in lines[0]  # 20 件の自己突合は数えない

    def test_self_source_labels_are_excluded_from_baseline_too(
        self, repo: RunHistoryRepository
    ) -> None:
        """baseline (過去週) も同じ基準で再計算しないと強制直後に偽の崩落警報が出る。"""
        _seed_week(repo, 1, 8, strength="strong")
        # 過去週は独立ラベルが薄いが、自己突合を大量に足せば旧分母では健全に見える
        for w in range(2, 6):
            _seed_week(repo, w, 1, strength="strong")
            _seed_week(repo, w, 50, strength="self_source")
        lines = supply_drift_lines(repo, now=_NOW)
        # 独立ラベルだけで見た中央値は 1 件 → 直近完全週 8 件は崩落ではない
        assert "崩落疑い" not in "\n".join(lines)
