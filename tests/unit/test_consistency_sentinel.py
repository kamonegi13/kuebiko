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


class TestSentinelLine:
    def test_none_reports_insufficient_sample(self) -> None:
        assert "標本不足" in sentinel_line(None)

    def test_line_contains_rates_and_anti_goodhart_note(self) -> None:
        stats = ConsistencyStats(
            articles=100,
            measured_clusters=10,
            split_clusters=3,
            single_host_measured=4,
            single_host_split=2,
        )

        line = sentinel_line(stats)

        assert "30%" in line
        assert "50%" in line
        assert "目標関数にしない" in line
