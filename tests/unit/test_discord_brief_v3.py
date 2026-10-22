"""Discord ブリーフの見た目の直し 5 点 (2026-10-22) のテスト。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.digest import discord_brief as db
from src.digest.high_threat_digest import (
    HighThreatItem,
    collect_event_info,
    collect_other_channel_counts,
    high_threats_payload,
)
from src.storage.run_history import RunHistoryRepository

BASE = "https://cti.example"


def _item(i: int, title: str, *, event_id: str = "", headline: str = "") -> dict[str, Any]:
    return {
        "article_id": f"a{i}",
        "title": title,
        "category_label": "インシデント",
        "url": f"https://x/{i}",
        "is_japan": False,
        "source_tier": "",
        "event_id": event_id,
        "event_headline": headline,
    }


def _payload(items: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "high_threats": {"total": len(items), "items": items},
        "synthesis": {"headline": "総括の一文。"},
        "pir_brief": None,
        "pir": [],
    }
    return {**base, **kw}


class TestEventGrouping:
    def test_same_event_articles_collapse_to_one_line_with_count(self) -> None:
        items = [
            _item(0, "AOMEI 更新サーバ改ざん", event_id="ev1", headline="AOMEI の更新が改ざん"),
            _item(1, "AOMEI backup hit", event_id="ev1", headline="AOMEI の更新が改ざん"),
            _item(2, "AOMEI 続報", event_id="ev1", headline="AOMEI の更新が改ざん"),
            _item(3, "別件の漏洩"),
        ]
        out = db.compose_morning(_payload(items), base_url=BASE).text
        assert out.count("AOMEI の更新が改ざん") == 1
        assert "(報道 3 件)" in out
        assert "・別件の漏洩" in out and "(報道 1 件)" not in out
        assert "2 事象" in out

    def test_articles_without_event_stay_one_line_each(self) -> None:
        items = [_item(i, f"独立 {i}") for i in range(3)]
        out = db.compose_morning(_payload(items), base_url=BASE).text
        assert out.count("・独立") == 3 and "報道" not in out and "事象" not in out

    def test_limit_counts_events_not_articles(self) -> None:
        items = [_item(i, f"記事{i}", event_id=f"ev{i // 2}") for i in range(30)]
        out = db.compose_morning(_payload(items), base_url=BASE).text
        assert out.count("(報道 2 件)") == db.HIGH_THREAT_MAX_ITEMS
        assert "…他 7 事象" in out


class TestNoFragments:
    def test_sentence_never_cut_at_comma(self) -> None:
        text = "あ" * 60 + "、" + "い" * 100 + "。"
        assert db.first_sentence(text, 110) == text
        assert "…" not in db.first_sentence(text, 110)

    def test_too_long_sentence_returns_empty_not_fragment(self) -> None:
        assert db.first_sentence("あ" * 400, 110) == ""

    def test_focus_without_clean_sentence_is_title_and_count_only(self) -> None:
        pir = [
            {
                "title": "領域X",
                "total": 5,
                "summary": "あ" * 60 + "、" + "い" * 400 + "。",
                "matches": [{"importance": "high", "tier": "news", "title": "記事の見出し"}],
            }
        ]
        out = db.compose_morning(_payload([_item(0, "t")], pir=pir), base_url=BASE).text
        assert "🔴 **領域X** (5 件)" in out
        assert "…" not in out and "記事の見出し" not in out

    def test_pir_answer_too_long_shows_question_and_confidence_only(self) -> None:
        pb = {
            "total": 9,
            "unassessed": 0,
            "moved": [{"question": "問い", "claim": "あ" * 500 + "。", "confidence": "low"}],
        }
        out = db.compose_morning(_payload([_item(0, "t")], pir_brief=pb), base_url=BASE).text
        assert "・問い (低確度)" in out and "→" not in out and "…" not in out


class TestOtherChannels:
    def test_thin_day_adds_other_channel_line(self) -> None:
        p = _payload([_item(0, "t")], other_channels={"alert": 3, "japan_watch": 8})
        out = db.compose_evening(p, base_url=BASE).text
        assert "別のチャンネル: alert 3 件・日本関連の注視 8 件" in out

    def test_zero_parts_omitted(self) -> None:
        p = _payload([_item(0, "t")], other_channels={"alert": 0, "japan_watch": 2})
        out = db.compose_morning(p, base_url=BASE).text
        assert "別のチャンネル: 日本関連の注視 2 件" in out and "alert" not in out

    def test_not_shown_on_busy_day_or_when_nothing(self) -> None:
        busy = _payload(
            [_item(i, f"独立 {i}") for i in range(db.THIN_EVENTS_MAX + 1)],
            other_channels={"alert": 3, "japan_watch": 8},
        )
        assert "別のチャンネル" not in db.compose_morning(busy, base_url=BASE).text
        none = _payload([_item(0, "t")], other_channels={"alert": 0, "japan_watch": 0})
        assert "別のチャンネル" not in db.compose_morning(none, base_url=BASE).text
        assert (
            "別のチャンネル"
            not in db.compose_morning(_payload([_item(0, "t")]), base_url=BASE).text
        )


class TestForeignTitles:
    def test_chinese_title_replaced_by_event_headline(self) -> None:
        items = [
            _item(
                0,
                "俄羅斯APT28入侵北約成員國造船企業",
                event_id="e",
                headline="APT28 が造船企業を侵害",
            )
        ]
        out = db.compose_morning(_payload(items), base_url=BASE).text
        assert "APT28 が造船企業を侵害" in out and "俄羅斯" not in out

    def test_english_title_replaced_by_title_ja_field(self) -> None:
        it = _item(0, "Prolific Chinese espionage group exploits zero-day")
        it["title_ja"] = "中国系スパイ集団がゼロデイを悪用"
        out = db.compose_morning(_payload([it]), base_url=BASE).text
        assert "中国系スパイ集団がゼロデイを悪用" in out

    def test_foreign_title_kept_when_no_japanese_available(self) -> None:
        out = db.compose_morning(
            _payload([_item(0, "Prolific Chinese espionage group")]), base_url=BASE
        ).text
        assert "Prolific Chinese espionage group" in out

    def test_japanese_title_not_replaced(self) -> None:
        items = [_item(0, "PHP コアに未認証 RCE", event_id="e", headline="別の見出し")]
        out = db.compose_morning(_payload(items), base_url=BASE).text
        assert "PHP コアに未認証 RCE" in out or "別の見出し" in out
        assert db.is_foreign_title("PHP コアに未認証 RCE") is False


class TestPersistence:
    def test_discord_text_round_trip(self, tmp_path: Path) -> None:
        repo = RunHistoryRepository(db_path=tmp_path / "b.db")
        repo.record_daily_brief(
            run_id=None,
            slot="morning",
            period_label="2026-10-22",
            title="t",
            bluf="",
            summary="全文",
            section_count=0,
            sources=[],
            discord_text="投稿した本文",
            generated_at=datetime(2026, 10, 22, tzinfo=UTC),
        )
        row = repo.list_daily_briefs(limit=1)[0]
        assert row["discord_text"] == "投稿した本文"
        assert repo.get_daily_brief(row["id"])["discord_text"] == "投稿した本文"  # type: ignore[index]

    def test_discord_text_defaults_empty(self, tmp_path: Path) -> None:
        repo = RunHistoryRepository(db_path=tmp_path / "b.db")
        repo.record_daily_brief(
            run_id=None,
            slot="evening",
            period_label="d",
            title="t",
            bluf="",
            summary="s",
            section_count=0,
            sources=[],
        )
        assert repo.list_daily_briefs(limit=1)[0]["discord_text"] == ""

    def test_fill_rate_registered(self) -> None:
        from src.ui.services.fill_rate_audit import METRICS

        assert "daily_brief_discord_text" in {m.key for m in METRICS}


class TestCollectors:
    def test_payload_carries_event_info(self) -> None:
        it = HighThreatItem("a1", "t", "incident", "https://x", False)
        out = high_threats_payload([it], total=1, event_info={"a1": ("ev1", "見出し")})["items"][0]
        assert out["event_id"] == "ev1" and out["event_headline"] == "見出し"

    def test_payload_without_event_info_has_empty_fields(self) -> None:
        it = HighThreatItem("a1", "t", "incident", "https://x", False)
        out = high_threats_payload([it], total=1)["items"][0]
        assert out["event_id"] == "" and out["event_headline"] == ""

    def test_collectors_fail_soft_on_empty_db(self, tmp_path: Path) -> None:
        RunHistoryRepository(db_path=tmp_path / "e.db")
        assert collect_event_info(["a1"], db_path=tmp_path / "e.db") == {}
        counts = collect_other_channel_counts(
            lookback_hours=24, now=datetime(2026, 10, 22, tzinfo=UTC), db_path=tmp_path / "e.db"
        )
        assert counts == {"alert": 0, "japan_watch": 0}


class TestTitleChoice:
    def test_long_event_headline_falls_back_to_shorter_article_title(self) -> None:
        items = [
            _item(0, "短い見出し", event_id="e", headline="あ" * 120),
            _item(1, "別の報道", event_id="e", headline="あ" * 120),
        ]
        out = db.compose_morning(_payload(items), base_url=BASE).text
        assert "短い見出し" in out and "…" not in out
