"""書き直しが元の draft より悪ければ採らない (2026-09-21)。

実測 (20:09-20:24 の 1 事象 6 呼出): 3 回目で事実 19 件・尾部も満ちた良い draft が
出たのに、識別子の網羅不足で書き直しに入り、書き直しの 3 回はすべて出力上限
6,144 に張り付いて尾部が空になった。**最後に残ったのは最初より悪い版**
(coverage 0.48 / facts 10)。書き直しは無条件に元を置き換えていた。
"""

from __future__ import annotations

from src.eventnews.models import EventNewsDraft, FactItem
from src.eventnews.runner import rewrite_regressed


def _draft(*, facts: int, tail: int, fact_chars: int = 80) -> EventNewsDraft:
    return EventNewsDraft(
        headline="h",
        bluf="b",
        facts=[FactItem(text=f"f{i}" + "あ" * fact_chars, source_index=1) for i in range(facts)],
        caveats=[FactItem(text=f"c{i}", source_index=1) for i in range(tail)],
    )


class TestRewriteRegressed:
    def test_full_tail_lost_to_truncated_rewrite_is_a_regression(self) -> None:
        before = _draft(facts=19, tail=2)
        after = _draft(facts=40, tail=0, fact_chars=300)  # 上限で切れて尾部が消えた

        assert rewrite_regressed(before=before, after=after)

    def test_rewrite_that_keeps_the_tail_is_accepted(self) -> None:
        before, after = _draft(facts=19, tail=2), _draft(facts=20, tail=1)

        assert not rewrite_regressed(before=before, after=after)

    def test_tail_was_already_empty_so_nothing_is_lost(self) -> None:
        """元から尾部が空なら、書き直しを採る (失うものが無い)。"""
        before = _draft(facts=19, tail=0)
        after = _draft(facts=40, tail=0, fact_chars=300)

        assert not rewrite_regressed(before=before, after=after)

    def test_short_rewrite_without_tail_is_not_truncation(self) -> None:
        """切れていない (短い) のに尾部が空 = モデルの判断。書き直しを採る。"""
        before, after = _draft(facts=19, tail=2), _draft(facts=5, tail=0)

        assert not rewrite_regressed(before=before, after=after)
