"""続報で「事実が増えたか」を数値で判定する (条件⑤)。

⭐ 2026-09-02 の利用者指摘: **規模が大きく変わっていなくても、記事内容が更新される
なら更新**。①〜④ はすべて entity ベースなので、同じ被害組織・同じアクターのまま
事実だけが増える続報 — 国内クラウド事業者の第 2 報「営業管理システムも被害、
最大 120 万件」型 — が reinforced (ただの追随報道) に落ちていた。

⚠ 文面の差分は採らない。原文に書かれた数値だけを見る。
⚠ 日付と割合は除く (実測 2,091 合流で日付は大半が公開日、割合は分析の比)。
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.eventnews import quantities, state
from src.eventnews.models import MemberArticle
from src.eventnews.state import compute_source_breakdown

_NOW = datetime(2026, 9, 2, tzinfo=UTC)


def _member(aid: str, *, title: str = "", body: str = "", feed: str = "媒体") -> MemberArticle:
    return MemberArticle(
        article_id=aid,
        title=title or f"見出し {aid}",
        url=f"https://kuebiko.example/{aid}",
        feed_title=feed,
        feed_url="https://kuebiko.example/feed",
        host="kuebiko.example",
        importance="high",
        category="breach",
        status="posted",
        anchor_ts=_NOW,
        summary="",
        body=body,
        entities=frozenset({("victim_org", "sakura")}),
    )


def _new_values(new: MemberArticle, prior: list[MemberArticle]) -> tuple[str, ...]:
    return quantities.new_values(
        quantities.supporting_texts([new])[0], quantities.supporting_texts(prior)
    )


def test_new_number_in_a_follow_up_is_detected() -> None:
    # Arrange — 第1報「480 件」→ 第2報「最大 120 万件」(被害組織は同じ)
    first = _member("a", body="レンタルサーバの480件で個人データ漏えいの可能性。")
    second = _member("b", body="営業管理システムも被害。最大約120万件の顧客アカウントに影響。")

    # Act
    got = _new_values(second, [first])

    # Assert
    assert any("136" in v for v in got)


def test_repeating_the_same_number_is_not_new() -> None:
    # Arrange — 別媒体が同じ数字を報じただけ
    first = _member("a", body="480件の個人データが漏えいした可能性がある。")
    second = _member("b", body="同社によれば480件が影響を受けたという。")

    # Act / Assert
    assert _new_values(second, [first]) == ()


def test_ratios_are_not_counted_as_new_facts() -> None:
    """割合は分析の比であって事象の規模ではない (実データの雑音はここに集中していた)。"""
    # Arrange
    first = _member("a", body="サーバが侵害された。")
    second = _member("b", body="調査対象の100%が影響を受け、修正率は0.5%にとどまる。")

    # Act / Assert
    assert _new_values(second, [first]) == ()


def test_new_quantities_alone_make_the_arrival_an_update() -> None:
    """entity が 1 つも増えなくても、新しい数値があれば updated。"""
    # Arrange — 駆動 entity は既出のものだけ
    new_member = _member("b", body="最大約120万件に影響が拡大した。")
    before = compute_source_breakdown([_member("a")])
    after = compute_source_breakdown([_member("a"), new_member])

    # Act
    decision = state.decide_arrival(
        {"victim_org": frozenset({"sakura"})},
        new_member,
        before,
        after,
        "high",
        new_quantities=("120万件",),
    )

    # Assert
    assert decision.kind == "updated"
    assert "new_quantities" in decision.reasons
    assert decision.new_facts["new_quantities"] == ["120万件"]


def test_without_new_quantities_the_arrival_stays_reinforced() -> None:
    """何も増えていなければ従来どおり reinforced (恒真にしない)。"""
    # Arrange
    others = [_member("a"), _member("c", feed="別媒体")]
    new_member = _member("b")
    before = compute_source_breakdown(others)
    after = compute_source_breakdown([*others, new_member])

    # Act
    decision = state.decide_arrival(
        {"victim_org": frozenset({"sakura"})}, new_member, before, after, "high"
    )

    # Assert
    assert decision.kind == "reinforced"
    assert "new_quantities" not in decision.new_facts
