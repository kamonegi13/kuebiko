"""ransomware.live 構造化取込の純粋ロジック (parse / 正規化 / dedup) の unit テスト。

ネットワークは触らない (fetch は薄いラッパなので parse/dedup の決定論を検証する)。
"""

from __future__ import annotations

from src.cti.taxonomy_normalizer import load_normalizer
from src.sources.ransomware_live import (
    RansomwareIncident,
    dedup_key,
    parse_incident,
    parse_incidents,
)

# recentvictims endpoint の実サンプル形 (victim / group / url キー)。
_RECENT_ROW: dict[str, object] = {
    "activity": "Manufacturing",
    "country": "JP",
    "description": "Japanese manufacturer breach claim.",
    "discovered": "2026-06-18T13:51:06.776180+00:00",
    "group": "lockbit",
    "victim": "Acme Kogyo K.K.",
    "claim_url": "http://example.onion/blog/acme",
    "url": "https://ransomware.live/id/acme",
}

# countryvictims endpoint の実サンプル形 (post_title / group_name / post_url キー)。
_COUNTRY_ROW: dict[str, object] = {
    "activity": "Technology",
    "country": "JP",
    "description": "tech victim",
    "published": "2026-06-10T00:00:00+00:00",
    "group_name": "akira",
    "post_title": "Beta Tech Co",
    "post_url": "https://ransomware.live/id/beta",
}


def test_parse_recent_schema_maps_victim_group_country() -> None:
    # Arrange
    norm = load_normalizer()

    # Act
    inc = parse_incident(_RECENT_ROW, norm)

    # Assert
    assert inc is not None
    assert inc.victim == "Acme Kogyo K.K."
    assert inc.group == "lockbit"
    assert inc.country_iso == "JP"
    assert inc.url == "http://example.onion/blog/acme"


def test_parse_country_schema_uses_alternate_keys() -> None:
    # Arrange
    norm = load_normalizer()

    # Act
    inc = parse_incident(_COUNTRY_ROW, norm)

    # Assert: post_title→victim, group_name→group, post_url→url
    assert inc is not None
    assert inc.victim == "Beta Tech Co"
    assert inc.group == "akira"
    assert inc.country_iso == "JP"
    assert inc.url == "https://ransomware.live/id/beta"


def test_parse_skips_record_without_victim() -> None:
    # Arrange
    norm = load_normalizer()

    # Act / Assert: 被害組織名が無い record は None (地図に偽の点を出さない)
    assert parse_incident({"group": "play", "country": "US"}, norm) is None


def test_parse_unresolvable_country_keeps_raw_iso_none() -> None:
    # Arrange
    norm = load_normalizer()
    row = {**_RECENT_ROW, "country": "ZZ-not-a-country"}

    # Act
    inc = parse_incident(row, norm)

    # Assert: 解決不能でも raw は保持、iso は None (保守的)
    assert inc is not None
    assert inc.country_iso is None
    assert inc.country_raw == "ZZ-not-a-country"


def test_dedup_key_stable_across_timestamp_jitter() -> None:
    # Arrange: 同一 group+victim+日 だが timestamp が異なる 2 record
    a = RansomwareIncident("Acme", "lockbit", "JP", "JP", None, "x", "2026-06-18T01:00:00Z", "", "")
    b = RansomwareIncident("Acme", "lockbit", "JP", "JP", None, "x", "2026-06-18T23:59:00Z", "", "")

    # Act / Assert: 日付は date 部分のみ → 同一キーに収斂
    assert dedup_key(a) == dedup_key(b)
    assert dedup_key(a).startswith("rl_")


def test_dedup_key_differs_by_victim() -> None:
    # Arrange
    a = RansomwareIncident("Acme", "lockbit", "JP", "JP", None, "x", "2026-06-18", "", "")
    b = RansomwareIncident("Beta", "lockbit", "JP", "JP", None, "x", "2026-06-18", "", "")

    # Act / Assert
    assert dedup_key(a) != dedup_key(b)


def test_parse_incidents_filters_invalid() -> None:
    # Arrange
    rows: list[dict[str, object]] = [_RECENT_ROW, {"group": "no-victim"}, _COUNTRY_ROW]

    # Act
    out = parse_incidents(rows)

    # Assert: 2 件 (victim 無しは除外)
    assert len(out) == 2
    assert {i.victim for i in out} == {"Acme Kogyo K.K.", "Beta Tech Co"}
