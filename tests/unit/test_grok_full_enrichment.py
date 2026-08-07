"""Grok JSONL full-enrichment の overlay ロジック検証 (Phase: Grok full-enrichment)。

現 Grok (生 X 投稿) を他ソースと同等に enrichment し、**channel routing は content
engine (enriched 側) を採用**する `_merge_grok_overlay` の正しさを検証する
(B1 2026-06-16: theme→channel の source 依存ルーティングを廃止、content-based に統一)。
"""

from __future__ import annotations

import pytest

from src.main import _grok_subarticle_id, _merge_grok_overlay
from src.tools.discord_publisher import BriefingMessage, Source


def _msg_with_tweet(tweet_id: str) -> BriefingMessage:
    return BriefingMessage(
        title="t",
        summary="s",
        importance="medium",
        category="breach",
        metadata={"tweet_id": tweet_id} if tweet_id else {},
    )


@pytest.mark.unit
def test_grok_subarticle_id_unique_per_tweet() -> None:
    # 同一親レポートでも tweet ごとに異なる article_id (body/entity 混載バグの修正)
    parent = "grok:7e22b2895db3ae58:123435"
    a = _grok_subarticle_id(parent, _msg_with_tweet("111"), 0)
    b = _grok_subarticle_id(parent, _msg_with_tweet("222"), 1)
    assert a != b
    assert a == f"{parent}#111" and b == f"{parent}#222"
    # tweet_id は安定 → 再処理で同一 id (べき等)
    assert _grok_subarticle_id(parent, _msg_with_tweet("111"), 9) == a


@pytest.mark.unit
def test_grok_subarticle_id_index_fallback() -> None:
    # tweet_id 欠落時は index で一意化
    parent = "grok:abc:1"
    a = _grok_subarticle_id(parent, _msg_with_tweet(""), 0)
    b = _grok_subarticle_id(parent, _msg_with_tweet(""), 1)
    assert a == f"{parent}#i0" and b == f"{parent}#i1" and a != b


def _enriched() -> BriefingMessage:
    """full enrichment を受けた briefing 想定 (日本語 title + Diamond/PMESII/victim 等)。"""
    return BriefingMessage(
        title="🔴 GENESIS ランサムが新たな被害者を追加",  # 翻訳済 (title_ja)
        bluf="GENESIS ランサムウェアグループが新規被害組織を公開",
        importance="high",  # LLM 判定
        category="breach",  # LLM 判定 (grok_x_signal_* ではない)
        summary="GENESIS ランサムウェアグループが新たな被害組織をリークサイトに掲載した。",
        iocs=["evil-c2.example.com"],
        mitre_techniques=["T1486"],
        sources=[Source(title="enriched-src", url="https://example.com/enriched", language="ja")],
        metadata={
            # enrichment 由来 (保持されるべき)
            "pmesii_axes": ["E", "I-cyber"],
            "victim_sector_canonical": "manufacturing",
            "socio_political_intent": "financial",
            "technical_axis_summary": "リークサイト経由で公開",
            "editorial_stance": "factual_report",
            # _build_briefing の content engine (route()) が決めた channel/rule (採用されるべき)
            "target_channel": "watch",
            "routing_reason": "content:apt_high_no_escalation_signal",
            "routing_rule_id": "R-CONTENT",
            "dedup_key": "llm-derived-key",
        },
    )


def _grok_mechanical() -> BriefingMessage:
    """tweet_to_briefing が返す機械変換版 (Grok の routing/source/dedup の ground truth)。"""
    return BriefingMessage(
        title="🏴‍☠️[B] Ransomware Alert GENESIS ... (@FalconFeedsio)",  # 英語原文
        bluf="Ransomware Alert: GENESIS group ...",
        importance="medium",
        category="grok_x_signal_b",
        summary="**FalconFeeds.io** (@FalconFeedsio) — ...",
        sources=[
            Source(
                title="@FalconFeedsio",
                url="https://x.com/FalconFeedsio/status/123",
                language="auto",
            ),
        ],
        metadata={
            "target_channel": "alert",  # Grok theme routing (B1: 採用されない)
            "routing_reason": "grok_jsonl:theme_A",
            "routing_rule_id": "GROK_JSONL",
            "dedup_key": "grok-tweet-123",  # tweet ベース dedup (保持されるべき)
            "tweet_id": "123",
            "engagement_score": 70,
            "matched_theme": "B",
        },
    )


@pytest.mark.unit
def test_overlay_keeps_enriched_content() -> None:
    # Act
    merged = _merge_grok_overlay(_enriched(), _grok_mechanical())
    # Assert: 内容は enrichment 側 (日本語 title / LLM importance・category / IOC)
    assert "ランサム" in merged.title  # 翻訳済タイトル保持
    assert merged.importance == "high"  # LLM 判定保持
    assert merged.category == "breach"  # LLM 判定保持 (grok_x_signal_* でない)
    assert merged.iocs == ["evil-c2.example.com"]


@pytest.mark.unit
def test_overlay_preserves_enrichment_metadata() -> None:
    merged = _merge_grok_overlay(_enriched(), _grok_mechanical())
    m = merged.metadata
    # Diamond / PMESII / victim / editorial は enrichment 側が残る
    assert m["pmesii_axes"] == ["E", "I-cyber"]
    assert m["victim_sector_canonical"] == "manufacturing"
    assert m["socio_political_intent"] == "financial"
    assert m["technical_axis_summary"] == "リークサイト経由で公開"
    assert m["editorial_stance"] == "factual_report"


@pytest.mark.unit
def test_overlay_channel_is_content_based_not_theme() -> None:
    # B1: channel routing は content engine (enriched) を採用、Grok theme で上書きしない
    merged = _merge_grok_overlay(_enriched(), _grok_mechanical())
    m = merged.metadata
    assert m["target_channel"] == "watch"  # content engine の決定 (theme の "alert" でない)
    assert m["routing_rule_id"] == "R-CONTENT"  # GROK_JSONL でない
    assert m["routing_reason"] == "content:apt_high_no_escalation_signal"


@pytest.mark.unit
def test_overlay_preserves_grok_tweet_signals() -> None:
    # tweet 由来信号 (dedup_key / matched_theme / engagement) は Grok 側を保持
    merged = _merge_grok_overlay(_enriched(), _grok_mechanical())
    m = merged.metadata
    assert m["dedup_key"] == "grok-tweet-123"  # tweet ベース dedup (重要)
    assert m["matched_theme"] == "B"  # 分類ラベルとして保持 (badge/signal 用)
    assert m["engagement_score"] == 70
    assert m["tweet_id"] == "123"


@pytest.mark.unit
def test_overlay_source_is_tweet_permalink() -> None:
    merged = _merge_grok_overlay(_enriched(), _grok_mechanical())
    # source は tweet permalink に差し替わる (リンク先 = 元ツイート)
    assert merged.sources[0].url == "https://x.com/FalconFeedsio/status/123"
