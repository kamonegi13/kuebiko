"""eval gold set の選定ロジックのテスト。

同一入力比較の器 (2026-08-17)。既存の `verify_prompt_cutover.py` は**期間比較**で、
モデル/プロンプトの変化と収集コーパスの変化を分離できない。gold set は入力を凍結して
「同じ記事を新旧に通す」ことで、その交絡を断つ。

最重要の不変量: **稀少だが致命的な層が必ず標本に入ること**。CTI の致命的失敗は
「0day・日本標的の high を 1 件落とす」= 稀少クラスの再現率で、無作為抽出だと
標本から漏れて平均指標に隠れる。
"""

from __future__ import annotations

import pytest

from src.eval.goldset import GoldArticle, select_goldset


def art(
    aid: str,
    *,
    importance: str = "medium",
    category: str = "vulnerability",
    body_chars: int = 2000,
) -> GoldArticle:
    return GoldArticle(
        article_id=aid,
        title=f"title-{aid}",
        body="本文" * (body_chars // 2),
        feed_title="feed",
        published="2026-08-01",
        category=category,
        importance=importance,
    )


class TestSelectGoldset:
    def test_caps_each_stratum(self) -> None:
        cands = [art(f"a{i}", importance="low", category="vulnerability") for i in range(10)]
        got = select_goldset(cands, per_stratum=3)
        assert len(got) == 3

    def test_rare_stratum_is_always_included(self) -> None:
        """1 件しかない層も必ず入る (稀少クラスこそ測りたい対象)。"""
        cands = [art(f"bulk{i}", importance="low", category="vulnerability") for i in range(50)]
        cands.append(art("rare", importance="high", category="apt"))
        got = select_goldset(cands, per_stratum=3)
        assert "rare" in {a.article_id for a in got}

    def test_covers_all_strata_present_in_input(self) -> None:
        cands = [
            art("h-apt", importance="high", category="apt"),
            art("m-vuln", importance="medium", category="vulnerability"),
            art("l-geo", importance="low", category="geopolitical"),
        ]
        got = select_goldset(cands, per_stratum=5)
        assert {(a.importance, a.category) for a in got} == {
            ("high", "apt"),
            ("medium", "vulnerability"),
            ("low", "geopolitical"),
        }

    def test_excludes_short_bodies(self) -> None:
        """本文が薄い記事は「抽出の失敗」を測ることになり、判定の評価にならない。"""
        cands = [art("thin", body_chars=100), art("thick", body_chars=3000)]
        got = select_goldset(cands, per_stratum=5, min_body_chars=500)
        assert [a.article_id for a in got] == ["thick"]

    def test_is_deterministic(self) -> None:
        """同じ入力からは同じ標本 (再現できない gold set は基準線にならない)。"""
        cands = [art(f"a{i}") for i in range(20)]
        assert select_goldset(cands, per_stratum=4) == select_goldset(cands, per_stratum=4)

    def test_order_of_input_does_not_change_the_sample(self) -> None:
        cands = [art(f"a{i}") for i in range(20)]
        assert select_goldset(cands, per_stratum=4) == select_goldset(
            list(reversed(cands)), per_stratum=4
        )

    def test_duplicate_ids_are_dropped(self) -> None:
        cands = [art("dup"), art("dup"), art("other")]
        got = select_goldset(cands, per_stratum=5)
        assert sorted(a.article_id for a in got) == ["dup", "other"]

    def test_empty_input(self) -> None:
        assert select_goldset([], per_stratum=5) == []

    @pytest.mark.parametrize("per_stratum", [0, -1])
    def test_invalid_per_stratum_is_rejected(self, per_stratum: int) -> None:
        with pytest.raises(ValueError, match="per_stratum"):
            select_goldset([art("a")], per_stratum=per_stratum)


class TestEvalTargets:
    """評価対象の登録 (2026-08-18)。

    summarizer だけでなく取込時の judgment 分類器も同じ標本・同じ器で測る。
    judgment の判定基準はコード内にあるため ``--drop-field`` は使えない。
    """

    def test_both_calls_are_registered(self) -> None:
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
        from eval_goldset import _TARGETS

        assert set(_TARGETS) == {"summarizer", "judgment"}
        # summarizer だけがテンプレート (= 編集可能な判定基準) を持つ
        assert _TARGETS["summarizer"][1] is True
        assert _TARGETS["judgment"][1] is False
