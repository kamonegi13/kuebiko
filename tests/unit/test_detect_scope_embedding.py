"""情勢一覧の絞り込みを埋込で行う (2026-09-22)。

全件判定 (422 組・同一事象 39 件) で方式を比較した結果:

| 方式 | AUC | 回収 | 別事象を残す |
|---|---|---|---|
| **埋込 題名 × 要約** | **0.960** | 31/33 (94%) | 30/287 (10%) |
| 埋込 題名 × 見出し | 0.926 | 36/39 (92%) | 132/383 (34%) |
| 群化 ML | 0.804 | 36/39 | 234/383 (61%) |
| 決定論 (キー重なり) | 0.763 | 25/33 (75%) | 69/287 (24%) |

⭐ **決定論の併用は無意味**: 埋込が落とす同一 2 件は決定論も落とす (決定論だけが拾える
同一は **0 件**)。和集合にすると別事象が 10% → 28% に増えるだけ。
"""

from __future__ import annotations

import numpy as np

from src.assessment.detect_scope import (
    SUMMARY_THRESHOLD,
    TITLE_THRESHOLD,
    relevant_titles_by_embedding,
)


def _v(x: float, y: float) -> np.ndarray:
    v = np.array([x, y], dtype=np.float32)
    out: np.ndarray = v / np.linalg.norm(v)
    return out


class TestThresholds:
    def test_summary_threshold_is_the_measured_operating_point(self) -> None:
        # 回収 94% / 別事象 10%。0.50 は回収が同じで別事象 25%、0.60 は回収 76%
        assert SUMMARY_THRESHOLD == 0.55

    def test_title_threshold_is_looser_because_the_register_differs(self) -> None:
        assert TITLE_THRESHOLD == 0.45


class TestRelevantTitlesByEmbedding:
    def test_situation_close_to_any_candidate_is_kept(self) -> None:
        got = relevant_titles_by_embedding(
            situations=[("s1", "近い情勢", _v(1, 0)), ("s2", "遠い情勢", _v(0, 1))],
            candidates=[("a1", _v(1, 0.02), True)],
        )

        assert got == ["近い情勢"]

    def test_candidate_without_summary_uses_the_looser_title_threshold(self) -> None:
        """⚠ 要約埋込は 75% の記事にしかない。欠測は見出しで代替し、閾値も変える。"""
        borderline = _v(1.0, 0.85)  # 余弦 ≈ 0.76 … ではなく境界付近を作る
        cos = float(np.dot(_v(1, 0), borderline))
        assert TITLE_THRESHOLD < cos < 1.0

        kept_as_title = relevant_titles_by_embedding(
            situations=[("s1", "情勢", _v(1, 0))], candidates=[("a1", borderline, False)]
        )

        assert kept_as_title == ["情勢"]

    def test_order_follows_the_situation_list_and_titles_are_unique(self) -> None:
        got = relevant_titles_by_embedding(
            situations=[("s1", "A", _v(1, 0)), ("s2", "B", _v(1, 0)), ("s3", "A", _v(1, 0))],
            candidates=[("a1", _v(1, 0), True)],
        )

        assert got == ["A", "B"]

    def test_no_candidates_means_no_titles(self) -> None:
        assert relevant_titles_by_embedding(situations=[("s1", "A", _v(1, 0))], candidates=[]) == []
