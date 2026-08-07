"""ransomware.live → ArticleRecord マッピングの unit テスト (純粋部分)。

DB/Discord は触らず、incident → ArticleRecord の構造化フィールド付与と status/importance
ロジックを検証する (JP=posted / global=collected)。
"""

from __future__ import annotations

from datetime import datetime

from src.sources.ransomware_ingest import (
    _CATEGORY,
    _build_news_index,
    _covered_by_news,
    _summary,
    _title,
    incident_to_article_record,
)
from src.sources.ransomware_live import RansomwareIncident, dedup_key

_JP = RansomwareIncident(
    victim="Acme Kogyo K.K.",
    group="lockbit",
    country_iso="JP",
    country_raw="JP",
    sector_canonical="manufacturing",
    sector_raw="Manufacturing",
    discovered="2026-06-18T13:00:00+00:00",
    description="Japanese manufacturer breach.",
    url="http://example.onion/acme",
)
_US = RansomwareIncident(
    victim="Globex Inc",
    group="akira",
    country_iso="US",
    country_raw="US",
    sector_canonical="technology",
    sector_raw="Technology",
    discovered="2026-06-17",
    description="",
    url="https://ransomware.live/id/globex",
)


def test_jp_record_is_posted_with_victim_fields() -> None:
    # Act
    rec = incident_to_article_record(
        _JP, run_id=1, status="posted", importance="medium", posted_channel="japan_watch"
    )

    # Assert: 地図が読む構造化フィールドが直接セットされる
    assert rec.status == "posted"
    assert rec.posted_channel == "japan_watch"
    assert rec.category == _CATEGORY  # breach (地図 cyber カテゴリ)
    assert rec.victim_country_iso == "JP"
    assert rec.victim_sector_canonical == "manufacturing"
    assert rec.article_id == dedup_key(_JP)
    assert rec.dedup_key == dedup_key(_JP)
    assert rec.url == "http://example.onion/acme"
    assert rec.pmesii_i_cyber is True


def test_global_record_is_collected_not_posted() -> None:
    # Act
    rec = incident_to_article_record(_US, run_id=1, status="collected", importance="low")

    # Assert: global は collected (Discord 非投稿、地図のみ可視)
    assert rec.status == "collected"
    assert rec.posted_channel is None
    assert rec.victim_country_iso == "US"
    assert rec.discord_message_id is None


def test_title_includes_group_victim_country() -> None:
    # Act / Assert
    t = _title(_JP)
    assert "lockbit" in t
    assert "Acme Kogyo K.K." in t
    assert "(JP)" in t


def test_title_prefers_canonical_display_group() -> None:
    """辞書解決済みグループは canonical 表示名を使う (2026-08-01 thegentlemen 事故対策)。

    生 slug をタイトルに残すと LLM がその綴りを primary_actor として再生産し、
    3 記事閾値超過 → 新興提案 → 綴り違い二重登録の連鎖が起きる。
    """
    t = _title(_JP, display_group="LockBit")
    assert "LockBit: " in t
    assert "lockbit:" not in t


def test_record_title_uses_display_group() -> None:
    rec = incident_to_article_record(
        _JP,
        run_id=1,
        status="posted",
        importance="medium",
        display_group="LockBit",
    )
    assert rec.title.startswith("LockBit: ")


def test_summary_contains_structured_meta_and_source() -> None:
    # Act
    s = _summary(_JP)

    # Assert
    assert "lockbit" in s
    assert "Manufacturing" in s
    assert "ransomware.live" in s


def test_record_published_at_uses_discovered() -> None:
    # Act
    rec = incident_to_article_record(_US, run_id=2, status="collected", importance="low")

    # Assert: discovered (ISO 文字列) が datetime にパースされる
    assert rec.published_at is not None
    assert rec.published_at.year == 2026
    assert rec.published_at.month == 6
    assert rec.published_at.day == 17


# ----- クロスソース重複判定 (_covered_by_news) -----


def test_covered_when_news_has_same_org_within_window() -> None:
    # Arrange: ニュースが同一 org を近接日に掲載
    idx = _build_news_index([("acme kogyo k.k.", datetime(2026, 6, 18))])

    # Act / Assert: ±60日内 → 重複 (ニュース優先)
    assert _covered_by_news(idx, "Acme Kogyo K.K.", datetime(2026, 6, 20)) is True


def test_not_covered_when_news_org_too_far_in_time() -> None:
    # Arrange: 同一 org だが半年前のニュース (別被害とみなす)
    idx = _build_news_index([("acme kogyo k.k.", datetime(2025, 12, 1))])

    # Act / Assert: 窓外 → 重複でない (再被害を誤消ししない)
    assert _covered_by_news(idx, "Acme Kogyo K.K.", datetime(2026, 6, 20)) is False


def test_not_covered_when_org_absent_from_news() -> None:
    # Arrange
    idx = _build_news_index([("other corp", datetime(2026, 6, 18))])

    # Act / Assert
    assert _covered_by_news(idx, "Acme Kogyo K.K.", datetime(2026, 6, 20)) is False


def test_covered_with_unknown_date_falls_back_to_org_match() -> None:
    # Arrange: rl 側の日付不明 → org 一致のみで保守的に重複扱い
    idx = _build_news_index([("acme kogyo k.k.", datetime(2026, 6, 18))])

    # Act / Assert
    assert _covered_by_news(idx, "Acme Kogyo K.K.", None) is True


def test_build_news_index_drops_unparseable_dates() -> None:
    # Arrange: 日付 None / 不正は除外
    idx = _build_news_index([("a", None), ("b", "not-a-date"), ("c", "2026-06-18T00:00:00")])

    # Assert
    assert "a" not in idx and "b" not in idx
    assert len(idx["c"]) == 1
