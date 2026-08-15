"""Unit tests for src.grok.jsonl_to_briefings."""

from __future__ import annotations

from src.grok.jsonl_parser import TweetEngagement, TweetRecord
from src.grok.jsonl_to_briefings import (
    get_routing,
    records_to_briefings,
    tweet_to_briefing,
)


def _make_record(
    tweet_id: str = "1",
    theme: str = "B",
    text: str = "Akira ransomware group listed new victim.",
    like: int = 10,
    retweet: int = 5,
    external_urls: list[str] | None = None,
) -> TweetRecord:
    return TweetRecord(
        tweet_id=tweet_id,
        url=f"https://x.com/test/status/{tweet_id}",
        author_handle="@test",
        author_name="Test Account",
        posted_at="2026-05-25T03:42:00Z",
        lang="en",
        text=text,
        engagement=TweetEngagement(like=like, retweet=retweet),
        matched_theme=theme,
        external_urls=external_urls or [],
    )


class TestGetRouting:
    def test_routes_theme_a_to_alert(self) -> None:
        routing = get_routing("A")
        assert routing is not None
        assert routing.channel == "alert"
        assert routing.importance == "high"

    def test_routes_theme_b_to_watch(self) -> None:
        routing = get_routing("B")
        assert routing is not None
        assert routing.channel == "watch"
        assert routing.importance == "medium"

    def test_routes_theme_j1_to_japan_watch(self) -> None:
        routing = get_routing("J1")
        assert routing is not None
        assert routing.channel == "japan_watch"

    def test_routes_theme_j2_to_alert(self) -> None:
        routing = get_routing("J2")
        assert routing is not None
        assert routing.channel == "alert"

    def test_unknown_theme_returns_none(self) -> None:
        assert get_routing("X") is None
        assert get_routing("") is None


class TestTweetToBriefing:
    def test_converts_basic_record(self) -> None:
        record = _make_record(tweet_id="100", theme="B")
        msg = tweet_to_briefing(record)
        assert msg is not None
        assert msg.importance == "medium"  # B → medium
        assert msg.metadata["target_channel"] == "watch"
        assert msg.metadata["routing_rule_id"] == "GROK_JSONL"
        assert msg.metadata["matched_theme"] == "B"
        assert msg.metadata["tweet_id"] == "100"
        assert "🏴" in msg.title  # B badge

    def test_summary_contains_tweet_text(self) -> None:
        record = _make_record(text="Critical CVE-2026-1234 actively exploited.")
        msg = tweet_to_briefing(record)
        assert msg is not None
        assert "Critical CVE-2026-1234" in msg.summary

    def test_title_contains_observation_text(self) -> None:
        # Phase B-cal2: title はセクションラベルでなく観測内容を主役にする
        record = _make_record(
            theme="J1", text="芸術文化ホールで個人情報漏洩の可能性、Web サイト改ざん被害を公表"
        )
        msg = tweet_to_briefing(record)
        assert msg is not None
        assert "芸術文化ホール" in msg.title  # 内容が title に載る
        assert "@test" in msg.title  # 出典 handle も載る

    def test_title_truncates_long_text_with_ellipsis(self) -> None:
        long_text = "あ" * 200
        record = _make_record(theme="J1", text=long_text)
        msg = tweet_to_briefing(record)
        assert msg is not None
        assert "…" in msg.title
        assert len(msg.title) < 150  # 暴走しない

    def test_title_falls_back_to_label_when_text_empty(self) -> None:
        # RT のみ等で text 空 → ラベル見出しに fallback
        record = _make_record(theme="J1", text="")
        msg = tweet_to_briefing(record)
        assert msg is not None
        assert "日本企業 breach/incident" in msg.title

    def test_title_strips_leading_hashtag_noise(self) -> None:
        # "#Cyberattack\n\n<content>" → 先頭ハッシュタグを落として本文を前に出す
        record = _make_record(
            theme="B",
            text="#Cyberattack\n\nAkira ransomware group has listed Acme as a new victim.",
        )
        msg = tweet_to_briefing(record)
        assert msg is not None
        assert "#Cyberattack" not in msg.title
        assert "Akira ransomware" in msg.title

    def test_title_strips_leading_emoji_only_token(self) -> None:
        record = _make_record(theme="B", text="🚨🚨 LockBit が新たな被害者を公開")
        msg = tweet_to_briefing(record)
        assert msg is not None
        assert "LockBit" in msg.title

    def test_title_keeps_inline_hashtags(self) -> None:
        # 本文中のタグは保持 (先頭のみ除去)
        record = _make_record(theme="B", text="Akira listed Acme #ransomware")
        msg = tweet_to_briefing(record)
        assert msg is not None
        assert "#ransomware" in msg.title

    def test_title_all_hashtags_falls_back_to_original(self) -> None:
        # 全部タグ → 原文に fallback (内容消失を防ぐ)
        record = _make_record(theme="B", text="#LockBit #ransomware")
        msg = tweet_to_briefing(record)
        assert msg is not None
        assert "LockBit" in msg.title  # 原文がそのまま載る

    def test_primary_source_is_tweet_permalink(self) -> None:
        # 永続 URL に使われる sources[0] は X パーマリンク (grok.com でない)
        record = _make_record(tweet_id="555")
        msg = tweet_to_briefing(record)
        assert msg is not None
        assert msg.sources[0].url == "https://x.com/test/status/555"
        assert "grok.com" not in msg.sources[0].url

    def test_external_urls_included_in_sources(self) -> None:
        record = _make_record(
            external_urls=["https://example.com/article1", "https://example.com/article2"],
        )
        msg = tweet_to_briefing(record)
        assert msg is not None
        # 第 1 source = tweet URL、その後 external_urls 最大 2 件
        assert len(msg.sources) >= 2
        source_urls = [s.url for s in msg.sources]
        assert "https://example.com/article1" in source_urls

    def test_unknown_theme_returns_none(self) -> None:
        record = _make_record(theme="Z")
        assert tweet_to_briefing(record) is None

    def test_dedup_key_prefers_external_url(self) -> None:
        record = _make_record(external_urls=["https://example.com/breach"])
        msg = tweet_to_briefing(record)
        assert msg is not None
        assert msg.metadata["dedup_key"] == "https://example.com/breach"

    def test_dedup_key_falls_back_to_tweet_url(self) -> None:
        record = _make_record(tweet_id="999")
        msg = tweet_to_briefing(record)
        assert msg is not None
        assert msg.metadata["dedup_key"] == "https://x.com/test/status/999"

    def test_bluf_truncates_long_text(self) -> None:
        long_text = "x" * 200
        record = _make_record(text=long_text)
        msg = tweet_to_briefing(record)
        assert msg is not None
        assert len(msg.bluf) <= 80
        assert msg.bluf.endswith("...")

    def test_japanese_theme_routing(self) -> None:
        record_j1 = _make_record(theme="J1")
        msg_j1 = tweet_to_briefing(record_j1)
        assert msg_j1 is not None
        assert msg_j1.metadata["target_channel"] == "japan_watch"
        assert msg_j1.importance == "high"

        record_j3 = _make_record(theme="J3")
        msg_j3 = tweet_to_briefing(record_j3)
        assert msg_j3 is not None
        assert msg_j3.metadata["target_channel"] == "watch"
        assert msg_j3.importance == "medium"


class TestRecordsToBriefings:
    def test_converts_mixed_theme_records(self) -> None:
        records = [
            _make_record(tweet_id="1", theme="A"),
            _make_record(tweet_id="2", theme="B"),
            _make_record(tweet_id="3", theme="J1"),
        ]
        briefings = records_to_briefings(records)
        assert len(briefings) == 3

    def test_skips_unknown_themes(self) -> None:
        records = [
            _make_record(tweet_id="1", theme="A"),
            _make_record(tweet_id="2", theme="Z"),  # unknown
            _make_record(tweet_id="3", theme="J1"),
        ]
        briefings = records_to_briefings(records)
        assert len(briefings) == 2

    def test_empty_records_yields_empty(self) -> None:
        assert records_to_briefings([]) == []


class TestExpandedThemes:
    """9 タスク拡張 (2026-08-15) の新テーマ routing。"""

    def test_theme_g_ot_ics_to_watch(self) -> None:
        r = get_routing("G")
        assert r is not None and (r.importance, r.channel) == ("medium", "watch")

    def test_theme_h_iab_to_alert(self) -> None:
        r = get_routing("H")
        assert r is not None and (r.importance, r.channel) == ("high", "alert")

    def test_theme_i2_hacktivist_to_japan_watch(self) -> None:
        r = get_routing("I2")
        assert r is not None and (r.importance, r.channel) == ("high", "japan_watch")
