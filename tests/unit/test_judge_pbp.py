"""審判の事後合理化を防ぐ採点 (2026-09-20、SYNTHESIS §58)。

⚠ 現行の審判は 1 回の呼出で総合判定と観点別得点を同時に出す。文献 (arXiv:2605.23970)
はこれを**事後合理化を誘発する典型**とする — 結論が先に立ち、観点の数字が後から辻褄を
合わせる。実際、当方の審判は接地違反で勝敗を **12 戦 12 勝で完全予測**しており、
完璧すぎること自体が兆候だった。

対策:
1. **独立採点** — 各腕を**相手を見ずに**単独で採点する (比較の枠組みを外す)
2. **証拠の固定** — 違反はすべて本文からの引用を伴う。引用が本文に無ければ棄却
3. **総合は別呼出** — 観点を確定させた後に判定する

3 の後で「独立採点した違反数が、なお勝敗を予測するか」を見れば、接地という説明が
本物か事後合理化かが決まる。
"""

from __future__ import annotations

import pytest

from scripts.judge_eventnews_pbp import mcnemar_p, verified_violations


class _V:
    """審判が返す違反 1 件 (quote = 本文からの引用)。"""

    def __init__(self, quote: str, reason: str = "根拠なし") -> None:
        self.quote = quote
        self.reason = reason


def test_violation_must_quote_text_that_exists_in_the_summary() -> None:
    """引用が本文に無い違反は数えない (審判の捏造を通さない)。"""
    summary = "Chrome 151.0.7922.138 で 3 件の脆弱性が修正された。悪用は確認されていない。"
    got = verified_violations(
        [_V("悪用は確認されていない"), _V("中国系 APT が悪用している")], summary
    )

    assert [v.quote for v in got] == ["悪用は確認されていない"]


def test_whitespace_and_width_differences_do_not_reject_a_real_quote() -> None:
    """全角空白や改行の違いで正当な引用を落とさない (照合は正規化してから)。"""
    summary = "修正版は　151.0.7922.138\nである。"
    got = verified_violations([_V("修正版は 151.0.7922.138 である。")], summary)

    assert len(got) == 1


def test_empty_quote_is_rejected() -> None:
    assert verified_violations([_V("")], "本文") == []


@pytest.mark.parametrize(
    ("a_wins", "b_wins", "expected_significant"),
    [
        (12, 2, True),  # 現行の審判の実績
        (15, 3, True),
        (6, 2, False),  # 16 窓時点の第 3 の採点者
        (5, 5, False),
    ],
)
def test_mcnemar_uses_only_discordant_pairs(
    a_wins: int, b_wins: int, expected_significant: bool
) -> None:
    """引き分けは情報を持たないので検定に入れない (文献の指摘)。"""
    p = mcnemar_p(a_wins, b_wins)

    assert (p < 0.05) is expected_significant
    assert mcnemar_p(a_wins, b_wins) == mcnemar_p(b_wins, a_wins)  # 対称


def test_mcnemar_with_no_decided_pairs_is_not_significant() -> None:
    assert mcnemar_p(0, 0) == 1.0
