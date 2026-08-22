"""一貫性番兵 (src/tuning/consistency_sentinel.py) のテスト。

§10.2f: 同一事象クラスタ内の intent 分裂 = 「少なくとも一方が誤り」の検出器。
番兵専用 (目標関数にしない) — ここでは検出の正しさだけを固定する。
"""

from __future__ import annotations

from typing import Any

import numpy as np

from src.tuning.consistency_sentinel import (
    ConsistencyStats,
    cluster_indices,
    compute_from_rows,
    sentinel_line,
)


def _vec(direction: int, dim: int = 8) -> bytes:
    v = np.zeros(dim, dtype="<f4")
    v[direction] = 1.0
    return v.tobytes()


def _row(intent: str, direction: int, url: str = "https://a.example/x") -> dict[str, Any]:
    return {"intent": intent, "url": url, "vector": _vec(direction), "dim": 8}


class TestClusterIndices:
    def test_identical_vectors_cluster_and_distinct_do_not(self) -> None:
        m = np.vstack(
            [
                np.frombuffer(_vec(0), dtype="<f4"),
                np.frombuffer(_vec(0), dtype="<f4"),
                np.frombuffer(_vec(1), dtype="<f4"),
            ]
        ).astype(np.float32)

        groups = cluster_indices(m, 0.85)

        assert len(groups) == 1
        assert sorted(groups[0]) == [0, 1]

    def test_empty_matrix_returns_no_groups(self) -> None:
        assert cluster_indices(np.zeros((0, 4), dtype=np.float32), 0.85) == []


class TestComputeFromRows:
    def test_split_cluster_is_detected(self) -> None:
        # 同一ベクトル (同一事象) で intent が割れる → 分裂 1/1
        rows = [_row("espionage", 0), _row("financial", 0)]

        stats = compute_from_rows(rows, threshold=0.85)

        assert stats.measured_clusters == 1
        assert stats.split_clusters == 1
        assert stats.rate == 1.0

    def test_consistent_cluster_is_not_split(self) -> None:
        rows = [_row("espionage", 0), _row("espionage", 0)]

        stats = compute_from_rows(rows, threshold=0.85)

        assert stats.measured_clusters == 1
        assert stats.split_clusters == 0

    def test_single_host_stratum_counts_template_repeats(self) -> None:
        # 単一ホストの連報で割れる = 分類器不安定性の本命信号 (§10.2f)
        rows = [
            _row("disruption", 0, url="https://sputnik.example/1"),
            _row("influence", 0, url="https://www.sputnik.example/2"),  # www は同一 host 扱い
            _row("espionage", 1, url="https://a.example/1"),
            _row("financial", 1, url="https://b.example/1"),  # 複数ホストの分裂
        ]

        stats = compute_from_rows(rows, threshold=0.85)

        assert stats.measured_clusters == 2
        assert stats.split_clusters == 2
        assert stats.single_host_measured == 1
        assert stats.single_host_split == 1

    def test_dim_mismatch_rows_are_skipped(self) -> None:
        bad = {"intent": "espionage", "url": "", "vector": _vec(0, dim=4), "dim": 8}

        stats = compute_from_rows([bad], threshold=0.85)

        assert stats.articles == 0


class TestPairWindow:
    def test_pairs_beyond_window_are_not_same_event(self) -> None:
        # 同一ベクトルでも 14 日を超えて離れていれば別事象 (蓄積 90 日 ≠ 事象定義)
        rows = [
            {**_row("espionage", 0), "created_at": "2026-08-01T00:00:00+00:00"},
            {**_row("financial", 0), "created_at": "2026-08-20T00:00:00+00:00"},
        ]

        stats = compute_from_rows(rows, threshold=0.85, pair_window_days=14)

        assert stats.measured_clusters == 0

    def test_pairs_within_window_still_cluster(self) -> None:
        rows = [
            {**_row("espionage", 0), "created_at": "2026-08-01T00:00:00+00:00"},
            {**_row("financial", 0), "created_at": "2026-08-05T00:00:00+00:00"},
        ]

        stats = compute_from_rows(rows, threshold=0.85, pair_window_days=14)

        assert stats.measured_clusters == 1
        assert stats.split_clusters == 1


class TestWilsonInterval:
    def test_zero_total_is_zero_interval(self) -> None:
        from src.tuning.consistency_sentinel import wilson_interval

        assert wilson_interval(0, 0) == (0.0, 0.0)

    def test_small_sample_has_wide_interval(self) -> None:
        # 3/4 の点推定 75% は CI が ~30-95% に開く — 数字単独で出してはいけない量
        from src.tuning.consistency_sentinel import wilson_interval

        lo, hi = wilson_interval(3, 4)
        assert hi - lo > 0.4


class TestSentinelLine:
    def test_none_reports_insufficient_sample(self) -> None:
        assert "標本不足" in sentinel_line(None)

    def test_few_clusters_suppress_the_number(self) -> None:
        # クラスタ 14 件 (< 30) では点推定を出さない — 週次ドリフトの錯覚防止 (§10.2e)
        stats = ConsistencyStats(
            articles=1296,
            measured_clusters=14,
            split_clusters=3,
            single_host_measured=4,
            single_host_split=3,
        )

        line = sentinel_line(stats)

        assert "標本不足" in line
        assert "21%" not in line

    def test_line_contains_rates_ci_and_anti_goodhart_note(self) -> None:
        stats = ConsistencyStats(
            articles=1000,
            measured_clusters=100,
            split_clusters=30,
            single_host_measured=20,
            single_host_split=10,
        )

        line = sentinel_line(stats)

        assert "30%" in line
        assert "CI" in line
        assert "50%" in line
        assert "目標関数にしない" in line

    def test_single_host_stratum_suppressed_when_thin(self) -> None:
        stats = ConsistencyStats(
            articles=1000,
            measured_clusters=100,
            split_clusters=30,
            single_host_measured=4,
            single_host_split=3,
        )

        line = sentinel_line(stats)

        assert "単一ホスト層は標本不足" in line
        assert "75%" not in line
