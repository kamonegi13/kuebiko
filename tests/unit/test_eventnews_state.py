"""事象ニュースの独立媒体数算出 + 状態機械のテスト (docs/event_news_design.md §7 §8)。

すべて決定論 (LLM を呼ばない)。
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.eventnews.models import MemberArticle, SourceBreakdown
from src.eventnews.state import compute_source_breakdown, decide_arrival

_TS = datetime(2026, 8, 20, 0, 0, tzinfo=UTC)


def _member(
    article_id: str = "a1",
    *,
    feed_title: str = "BleepingComputer",
    feed_url: str = "https://bleepingcomputer.com/feed",
    host: str = "bleepingcomputer.com",
    importance: str = "medium",
    entities: frozenset[tuple[str, str]] = frozenset(),
) -> MemberArticle:
    return MemberArticle(
        article_id=article_id,
        title="t",
        url=f"https://e/{article_id}",
        feed_title=feed_title,
        feed_url=feed_url,
        host=host,
        importance=importance,
        category="apt",
        status="posted",
        anchor_ts=_TS,
        summary="s",
        body="",
        entities=entities,
    )


def _breakdown(
    independent: int = 1,
    state_media: int = 0,
    unclassified: int = 1,
    best_tier: str = "news",
) -> SourceBreakdown:
    return SourceBreakdown(
        independent=independent,
        state_media=state_media,
        unclassified=unclassified,
        best_tier=best_tier,
    )


class TestComputeSourceBreakdown:
    def test_empty_members_returns_zero_breakdown(self) -> None:
        bd = compute_source_breakdown([])

        assert bd == SourceBreakdown(
            independent=0, state_media=0, unclassified=0, best_tier="unknown"
        )

    def test_three_values_distinguish_state_media_and_unclassified(self) -> None:
        # 中国国営 2 社 (globaltimes.cn / xinhuanet.com) + news 1 + research 1
        members = [
            _member(
                "a1",
                feed_title="Global Times",
                feed_url="https://www.globaltimes.cn/x",
                host="globaltimes.cn",
            ),
            _member(
                "a2",
                feed_title="Xinhua",
                feed_url="https://www.xinhuanet.com/x",
                host="xinhuanet.com",
            ),
            _member(
                "a3",
                feed_title="BleepingComputer",
                feed_url="https://bleepingcomputer.com/feed",
                host="bleepingcomputer.com",
            ),
            _member(
                "a4",
                feed_title="Mandiant",
                feed_url="https://www.mandiant.com/x",
                host="mandiant.com",
            ),
        ]

        bd = compute_source_breakdown(members)

        assert bd.independent == 4
        assert bd.state_media == 2  # 中国国営 2 社 (§8 の SSoT 追記が効いている)
        assert bd.unclassified == 1  # BleepingComputer (news catch-all)
        assert bd.best_tier == "research"  # official > research > news > social > state_media

    def test_same_medium_counted_once_via_feed_url(self) -> None:
        members = [
            _member("a1", feed_url="https://bleepingcomputer.com/1"),
            _member("a2", feed_url="https://bleepingcomputer.com/1"),  # 同一 feed_url
        ]

        bd = compute_source_breakdown(members)

        assert bd.independent == 1

    def test_unclassified_is_never_hidden_as_zero(self) -> None:
        # §8: NOT NULL DEFAULT 0 — 未分類を「無い」と混同させない (明示的に 0 以上を返す)
        bd = compute_source_breakdown([_member("a1")])

        assert bd.unclassified == 1
        assert bd.state_media == 0


class TestDecideArrival:
    def test_reinforced_when_nothing_changes(self) -> None:
        existing = {"cve": frozenset({"CVE-2024-1111"})}
        new_member = _member(entities=frozenset({("cve", "CVE-2024-1111")}))
        before = _breakdown(independent=2)
        after = _breakdown(independent=2)

        decision = decide_arrival(existing, new_member, before, after, importance_before="medium")

        assert decision.kind == "reinforced"
        assert decision.change_kind is None
        assert decision.reasons == ()
        assert decision.new_facts == {"article_id": "a1"}

    def test_new_cve_triggers_updated(self) -> None:
        existing = {"cve": frozenset({"CVE-2024-1111"})}
        new_member = _member(entities=frozenset({("cve", "CVE-2024-2222")}))
        before = _breakdown(independent=2)
        after = _breakdown(independent=2)

        decision = decide_arrival(existing, new_member, before, after, importance_before="medium")

        assert decision.kind == "updated"
        assert decision.reasons == ("new_cve",)
        assert decision.change_kind == "add"
        assert decision.new_facts["added_entities"] == {"cve": ["CVE-2024-2222"]}

    def test_media_increase_triggers_updated(self) -> None:
        existing: dict[str, frozenset[str]] = {}
        new_member = _member()
        before = _breakdown(independent=1)
        after = _breakdown(independent=2)

        decision = decide_arrival(existing, new_member, before, after, importance_before="medium")

        assert decision.kind == "updated"
        assert "media_increase" in decision.reasons
        assert decision.new_facts["media_delta"] == 1

    def test_tier_rise_from_news_to_official_triggers_updated(self) -> None:
        existing: dict[str, frozenset[str]] = {}
        new_member = _member(feed_title="CISA", feed_url="https://www.cisa.gov/x", host="cisa.gov")
        before = _breakdown(best_tier="news")
        after = _breakdown(best_tier="official")

        decision = decide_arrival(existing, new_member, before, after, importance_before="medium")

        assert decision.kind == "updated"
        assert "tier_rise" in decision.reasons
        assert decision.new_facts["tier_transition"] == "news->official"

    def test_tier_rise_from_social_to_research_triggers_updated(self) -> None:
        # 条件③は news/social/state_media → research/official のいずれの遷移も対象
        existing: dict[str, frozenset[str]] = {}
        new_member = _member()
        before = _breakdown(best_tier="social")
        after = _breakdown(best_tier="research")

        decision = decide_arrival(existing, new_member, before, after, importance_before="medium")

        assert decision.kind == "updated"
        assert "tier_rise" in decision.reasons

    def test_importance_rise_triggers_updated(self) -> None:
        existing: dict[str, frozenset[str]] = {}
        new_member = _member(importance="high")
        before = _breakdown()
        after = _breakdown()

        decision = decide_arrival(existing, new_member, before, after, importance_before="medium")

        assert decision.kind == "updated"
        assert "importance_rise" in decision.reasons

    def test_disjoint_new_actor_is_change_kind_correct(self) -> None:
        existing = {"actor": frozenset({"APT41"})}
        new_member = _member(entities=frozenset({("actor", "Volt Typhoon")}))
        before = _breakdown(independent=2)
        after = _breakdown(independent=2)

        decision = decide_arrival(existing, new_member, before, after, importance_before="medium")

        assert decision.kind == "updated"
        assert decision.change_kind == "correct"
        assert "new_actor" in decision.reasons

    def test_overlapping_actor_is_change_kind_add(self) -> None:
        # actor は既存と重複 (置換ではない) だが cve が新規のため updated / change_kind='add'
        existing = {"actor": frozenset({"APT41"})}
        new_member = _member(entities=frozenset({("actor", "APT41"), ("cve", "CVE-2024-9999")}))
        before = _breakdown(independent=2)
        after = _breakdown(independent=2)

        decision = decide_arrival(existing, new_member, before, after, importance_before="medium")

        assert decision.kind == "updated"
        assert decision.change_kind == "add"

    def test_malware_family_is_excluded_from_driver_entities(self) -> None:
        # JOIN_ENTITY_TYPES には malware_family が含まれるが駆動 entity には数えない (§7)
        existing: dict[str, frozenset[str]] = {}
        new_member = _member(entities=frozenset({("malware_family", "Cobalt Strike")}))
        before = _breakdown()
        after = _breakdown()

        decision = decide_arrival(existing, new_member, before, after, importance_before="medium")

        assert decision.kind == "reinforced"
