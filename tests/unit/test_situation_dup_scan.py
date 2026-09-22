"""重複して開設された情勢の定期検査 (2026-09-22)。

規則 (`match_claim`) は誤って繋ぐ一方で本当の重複を取りこぼす。埋込で総当たりして
高い余弦の組を拾い、**人の確認へ回す** (自動では統合しない)。

実測 (Opus 盲検 115 組・余弦の帯で層化抽出):

| 帯 | 統合すべき |
|---|---|
| 0.75 以上 | 3/3 (100%) |
| 0.65-0.75 | 6/12 (50%) |
| 0.55-0.65 | 1/40 (2%) |

⭐ 0.65 を境に急落する。0.55-0.65 を拾うと 98% が空振りになる。
⚠ 常設情報要求 (standing) は設計上 別物 — 中国 / ロシア / イラン / 北朝鮮の事前配置は
  余弦 0.86 前後で近いが統合してはいけない。
"""

from __future__ import annotations

import numpy as np

from src.assessment.situation_dup_scan import (
    DUP_SCAN_THRESHOLD,
    DuplicatePair,
    find_duplicate_pairs,
)


def _v(x: float, y: float) -> np.ndarray:
    v = np.array([x, y], dtype=np.float32)
    out: np.ndarray = v / np.linalg.norm(v)
    return out


class TestThreshold:
    def test_threshold_is_where_the_rate_collapses(self) -> None:
        assert DUP_SCAN_THRESHOLD == 0.65


class TestFindDuplicatePairs:
    def test_close_pair_is_reported_with_its_cosine(self) -> None:
        got = find_duplicate_pairs(
            [("s1", "A", "event", _v(1, 0)), ("s2", "B", "event", _v(1, 0.02))]
        )

        assert len(got) == 1
        assert got[0].a_id == "s1" and got[0].b_id == "s2" and got[0].cosine > 0.99

    def test_distant_pair_is_not_reported(self) -> None:
        assert (
            find_duplicate_pairs([("s1", "A", "event", _v(1, 0)), ("s2", "B", "event", _v(0, 1))])
            == []
        )

    def test_standing_situations_are_excluded(self) -> None:
        """⚠ 監視対象国ごとの常設問いは互いに近いが、統合してはいけない。"""
        got = find_duplicate_pairs(
            [
                ("s1", "中国の事前配置", "standing", _v(1, 0)),
                ("s2", "ロシアの事前配置", "standing", _v(1, 0.01)),
                ("s3", "普通の情勢", "event", _v(1, 0.02)),
            ]
        )

        assert [(p.a_id, p.b_id) for p in got] == []

    def test_results_are_sorted_by_cosine_descending(self) -> None:
        got = find_duplicate_pairs(
            [
                ("s1", "A", "event", _v(1, 0)),
                ("s2", "B", "event", _v(1, 0.30)),
                ("s3", "C", "event", _v(1, 0.02)),
            ]
        )

        assert [p.cosine for p in got] == sorted((p.cosine for p in got), reverse=True)


class TestAuditSection:
    """週次監査に載せる 1 行 (ops へ届くこと自体が監査の生存証明)。"""

    def test_reports_the_count_and_the_closest_pair(self) -> None:
        from src.assessment.situation_dup_scan import audit_line

        line = audit_line(
            [
                DuplicatePair("s1", "A の情勢", "s2", "B の情勢", 0.91),
                DuplicatePair("s3", "C", "s4", "D", 0.70),
            ]
        )

        assert "2 組" in line and "0.91" in line and "A の情勢" in line
        assert "⚠" in line

    def test_no_duplicates_is_still_reported(self) -> None:
        """⭐ 0 件でも 1 行出す — 出なくなったことに気付けなくなる。"""
        from src.assessment.situation_dup_scan import audit_line

        line = audit_line([])

        assert "0 組" in line and "⚠" not in line
