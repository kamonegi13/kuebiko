"""アクター行動史 月次蒸留 (F7) のテスト — 決定論射影と run 横断 dedup が核心。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.cti.actor_observed_history import (
    JST,
    ActorMonthProfile,
    distill_month,
    month_label,
    month_window_utc,
    months_between,
)
from src.storage.run_history import RunHistoryRepository


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "aop.db")


def _row(
    aid: str,
    *,
    subject: str,
    created: str = "2026-07-20T03:00:00+00:00",
    sector: str | None = None,
    country: str | None = None,
    channel: str | None = None,
    feed: str = "https://feed.example/a",
) -> dict[str, object]:
    return {
        "article_id": aid,
        "created_at": created,
        "subject_actor_ids": subject,
        "victim_sector_canonical": sector,
        "victim_country_iso": country,
        "posted_channel": channel,
        "feed_url": feed,
    }


# ---------- 月ユーティリティ ----------


def test_month_label_uses_jst_boundary() -> None:
    # UTC 7/31 15:30 = JST 8/1 00:30 → '2026-08' (JST 境界)
    dt = datetime(2026, 7, 31, 15, 30, tzinfo=UTC)
    assert month_label(dt) == "2026-08"


def test_month_window_utc_roundtrip() -> None:
    start, end = month_window_utc("2026-07")
    assert start.astimezone(JST).strftime("%Y-%m-%d %H:%M") == "2026-07-01 00:00"
    assert end.astimezone(JST).strftime("%Y-%m-%d %H:%M") == "2026-08-01 00:00"


def test_months_between_spans_year_boundary() -> None:
    assert months_between("2026-11", "2027-01") == ["2026-11", "2026-12", "2027-01"]
    assert months_between("2026-08", "2026-07") == []


# ---------- distill_month ----------


def test_distill_counts_basic_dimensions() -> None:
    rows = [
        _row("a1", subject="qilin", sector="healthcare", country="US", feed="https://f1"),
        _row("a2", subject="qilin", sector="healthcare", country="JP", feed="https://f2"),
        _row("a3", subject="apt29", country="DE", feed="https://f1"),
    ]
    entities = {
        "a1": {("malware_family", "Qilin"), ("cve", "CVE-2026-1111")},
        "a2": {("ttp", "T1486")},
        "a3": {("campaign", "MidnightOp")},
    }
    out = distill_month("2026-07", rows, entities, kev_cves={"CVE-2026-1111"})
    by_id = {p.actor_id: p for p in out}
    q = by_id["qilin"]
    assert q.subject_articles == 2
    assert q.distinct_sources == 2
    assert q.sectors == {"healthcare": 2}
    assert q.countries == {"US": 1, "JP": 1}
    assert q.malware == {"Qilin": 1}
    assert q.ttps == {"T1486": 1}
    assert q.japan_targeted == 1  # victim=JP
    assert q.kev_hits == 1
    a = by_id["apt29"]
    assert a.subject_articles == 1
    assert a.campaigns == {"MidnightOp": 1}
    assert a.kev_hits == 0


def test_distill_dedupes_run_crossing_duplicate_rows() -> None:
    """同一 article_id の run 横断重複行は 1 記事として数える (実測 27 行の教訓)。"""
    rows = [
        _row("a1", subject="qilin", created="2026-07-20T03:00:00+00:00"),
        _row("a1", subject="qilin", created="2026-07-21T03:00:00+00:00", sector="energy"),
        _row("a1", subject="qilin", created="2026-07-19T03:00:00+00:00"),
    ]
    out = distill_month("2026-07", rows, {}, kev_cves=set())
    assert len(out) == 1
    p = out[0]
    assert p.subject_articles == 1
    # 最新行 (07-21、sector=energy) が採用される
    assert p.sectors == {"energy": 1}


def test_distill_splits_comma_separated_subjects() -> None:
    rows = [_row("a1", subject="qilin, apt29")]
    out = distill_month("2026-07", rows, {}, kev_cves=set())
    assert sorted(p.actor_id for p in out) == ["apt29", "qilin"]


def test_distill_japan_targeted_via_channel() -> None:
    rows = [_row("a1", subject="qilin", channel="japan_watch")]
    out = distill_month("2026-07", rows, {}, kev_cves=set())
    assert out[0].japan_targeted == 1


def test_distill_empty_input() -> None:
    assert distill_month("2026-07", [], {}, kev_cves=set()) == []


# ---------- repo roundtrip ----------


def test_repo_replace_and_list_roundtrip(repo: RunHistoryRepository) -> None:
    p1 = ActorMonthProfile(
        actor_id="qilin",
        month="2026-07",
        subject_articles=5,
        distinct_sources=3,
        sectors={"healthcare": 2},
        countries={"US": 3, "JP": 1},
        malware={"Qilin": 2},
        ttps={"T1486": 1},
        campaigns={},
        japan_targeted=1,
        kev_hits=2,
    )
    assert repo.replace_actor_month_profiles("2026-07", [p1]) == 1
    got = repo.list_actor_month_profiles(["qilin"])
    assert len(got) == 1
    assert got[0].sectors == {"healthcare": 2}
    assert got[0].countries == {"US": 3, "JP": 1}
    assert got[0].kev_hits == 2


def test_repo_replace_removes_stale_actor_rows(repo: RunHistoryRepository) -> None:
    """再蒸留で消えたアクターの stale 行が残らない (月単位の全置換)。"""
    a = ActorMonthProfile(actor_id="a", month="2026-07", subject_articles=1)
    b = ActorMonthProfile(actor_id="b", month="2026-07", subject_articles=1)
    other = ActorMonthProfile(actor_id="a", month="2026-06", subject_articles=9)
    repo.replace_actor_month_profiles("2026-06", [other])
    repo.replace_actor_month_profiles("2026-07", [a, b])
    repo.replace_actor_month_profiles("2026-07", [a])  # 再蒸留で b が消えた
    assert [p.actor_id for p in repo.list_actor_month_profiles(["b"])] == []
    # 他の月は不干渉
    months = [p.month for p in repo.list_actor_month_profiles(["a"])]
    assert months == ["2026-06", "2026-07"]


def test_repo_count_rows(repo: RunHistoryRepository) -> None:
    assert repo.count_actor_profile_rows() == 0
    repo.replace_actor_month_profiles("2026-07", [ActorMonthProfile(actor_id="a", month="2026-07")])
    assert repo.count_actor_profile_rows() == 1


def test_repo_list_returns_multiple_ids_month_order(repo: RunHistoryRepository) -> None:
    """merge 合算のための複数 id 取得 (旧 id + canonical) が月昇順で返る。"""
    repo.replace_actor_month_profiles(
        "2026-08", [ActorMonthProfile(actor_id="new_id", month="2026-08", subject_articles=2)]
    )
    repo.replace_actor_month_profiles(
        "2026-07", [ActorMonthProfile(actor_id="old_id", month="2026-07", subject_articles=3)]
    )
    got = repo.list_actor_month_profiles(["new_id", "old_id"])
    assert [(p.month, p.actor_id) for p in got] == [("2026-07", "old_id"), ("2026-08", "new_id")]


# ---------- F5: alias 使用統計 ----------


def test_alias_usage_record_and_totals(repo: RunHistoryRepository) -> None:
    repo.record_alias_usage("a1", [("qilin", "Qilin"), ("qilin", "Agenda")])
    repo.record_alias_usage("a2", [("qilin", "Qilin")])
    # 同一記事の再記録 (run 横断重複) は二重計上しない
    repo.record_alias_usage("a1", [("qilin", "Qilin")])
    assert repo.alias_usage_totals(["qilin"]) == {"Qilin": 2, "Agenda": 1}
    assert repo.alias_usage_totals(["unknown"]) == {}
    assert repo.alias_usage_totals([]) == {}


def test_distill_casefolds_entity_variants() -> None:
    """P1-S3 (2026-07-26): 大小文字違いの entity 表記ゆれを 1 キーに畳む。

    実データで ZOHOMURK / Zohomurk が別カウントされ、永久記録の月次行に
    重複表示が固定されかけた。表示形は最頻出表記を採用する。
    """
    rows = [
        _row("a1", subject="qilin"),
        _row("a2", subject="qilin"),
        _row("a3", subject="qilin"),
    ]
    entities = {
        "a1": {("malware_family", "ZOHOMURK")},
        "a2": {("malware_family", "Zohomurk")},
        "a3": {("malware_family", "Zohomurk"), ("campaign", "OpX"), ("campaign", "OPX")},
    }
    out = distill_month("2026-07", rows, entities, kev_cves=set())
    p = out[0]
    # 3 件が最頻出表記 'Zohomurk' 1 キーに畳まれる
    assert p.malware == {"Zohomurk": 3}
    # 同一記事内の表記ゆれ 2 形も 1 キー (どちらの表示形でも可、件数が正)
    assert sum(p.campaigns.values()) == 2
    assert len(p.campaigns) == 1
