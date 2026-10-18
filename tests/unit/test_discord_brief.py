"""朝夕ブリーフの Discord 要点版 (2026-10-18 再設計) のテスト。"""

from __future__ import annotations

from typing import Any

import pytest

from src.digest import discord_brief as db
from src.forecast.burst import BurstEvent

BASE = "https://cti.example"


def _ht(n: int, *, tier: str = "") -> dict[str, Any]:
    return {
        "total": n,
        "items": [
            {
                "article_id": f"a{i}",
                "title": f"脅威 {i}",
                "category_label": "APT",
                "url": f"https://x/{i}",
                "is_japan": i == 0,
                "source_tier": tier,
            }
            for i in range(n)
        ],
    }


def _q(question: str = "問い A", claim: str = "答え。二文目。") -> dict[str, Any]:
    return {
        "question": question,
        "claim": claim,
        "confidence": "moderate",
        "moves": [{"label": "更新", "reason": "claim 文言を改訂 (記録のみ)"}],
        "claim_unrevised": False,
    }


def _payload(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "pir_brief": {"total": 9, "moved": [_q()], "unassessed": 0},
        "high_threats": _ht(2),
        "synthesis": {"headline": "見出しの一文目。二文目。", "sections": []},
        "pir": [],
    }
    return {**base, **kw}


def _burst(label: str, today: int, base: float) -> BurstEvent:
    return BurstEvent("actor", label, label, today, base, 9.9)


class TestFirstSentence:
    def test_takes_first_sentence(self) -> None:
        assert db.first_sentence("一つ目。二つ目。") == "一つ目。"

    def test_long_sentence_cut_at_comma_not_mid_word(self) -> None:
        text = "あ" * 60 + "、" + "い" * 100 + "。"
        out = db.first_sentence(text, 110)
        assert out == "あ" * 60 + "…"

    def test_long_sentence_without_comma_kept_whole(self) -> None:
        text = "あ" * 150 + "。"
        assert db.first_sentence(text, 110) == text

    def test_absurdly_long_sentence_hard_capped(self) -> None:
        text = "あ" * 400
        assert db.first_sentence(text, 110) == "あ" * 110 + "…"

    def test_empty(self) -> None:
        assert db.first_sentence("  ") == ""


class TestMorning:
    def test_summary_line_and_order(self) -> None:
        out = db.compose_morning(_payload(), bursts=[_burst("APT1", 5, 2.0)], base_url=BASE).text
        assert out.startswith("📌 高脅威 2 件 / PIR 1/9 問が動いた / 急増 1 件")
        assert out.index("高脅威 (要確認") < out.index("**PIR**") < out.index("状況総括")
        assert out.index("状況総括") < out.index("📈")
        assert out.endswith(f"{BASE}/app/daily-brief")

    def test_no_operation_records_or_analysis_sections(self) -> None:
        out = db.compose_morning(_payload(), base_url=BASE).text
        assert "claim 文言" not in out and "記録のみ" not in out
        assert "比重" not in out and "重心" not in out
        assert "・問い A\n　→ 答え。 (中確度)" in out

    def test_synthesis_is_one_sentence(self) -> None:
        out = db.compose_morning(_payload(), base_url=BASE).text
        assert "見出しの一文目。" in out and "二文目。" not in out.split("状況総括")[1]

    def test_zero_moved_pir_is_headline_only(self) -> None:
        pb = {"total": 9, "moved": [], "unassessed": 0}
        out = db.compose_morning(_payload(pir_brief=pb), base_url=BASE).text
        assert "9 問中 0 問の答えが動いた" in out and "→" not in out

    def test_high_threat_top8_with_url_sns_marker_and_rest(self) -> None:
        out = db.compose_morning(_payload(high_threats=_ht(11, tier="social")), base_url=BASE).text
        assert out.count("⚠SNS(要裏取り)") == 8
        assert "<https://x/7>" in out and "<https://x/8>" not in out
        assert f"…他 3 件 → {BASE}/app/news?importance=high" in out

    def test_focus_only_red_regions(self) -> None:
        pir = [
            {
                "title": "赤の領域",
                "total": 4,
                "summary": "要点の一文。続き。",
                "matches": [{"importance": "high", "tier": "news", "title": "t"}],
            },
            {
                "title": "SNS 領域",
                "total": 1,
                "summary": "",
                "matches": [{"importance": "high", "tier": "social", "title": "SNS 記事"}],
            },
            {
                "title": "黄の領域",
                "total": 2,
                "summary": "x",
                "matches": [{"importance": "medium", "tier": "news", "title": "m"}],
            },
        ]
        out = db.compose_morning(_payload(pir=pir), base_url=BASE).text
        assert "🔴 **赤の領域** (4 件) — 要点の一文。" in out and "続き" not in out
        assert "SNS 記事 ⚠SNS(要裏取り)" in out
        assert "黄の領域" not in out and "他 1 領域 → Web" in out

    def test_burst_ratio_and_new_without_z(self) -> None:
        events = [
            _burst("A", 6, 2.0),
            _burst("B", 3, 0.0),
            _burst("C", 30, 2.0),
            _burst("D", 4, 1.0),
        ]
        out = db.compose_morning(_payload(), bursts=events, base_url=BASE).text
        assert "通常の約 3.0 倍" in out and "(新出)" in out and "通常の約 15 倍" in out
        assert "z=" not in out and "アクター D" not in out and "…他 1 件" in out

    def test_collapses_from_the_back_when_over_budget(self) -> None:
        pir = [
            {
                "title": f"領域{i}",
                "total": 3,
                "summary": "あ" * 100 + "。",
                "matches": [{"importance": "high", "tier": "news", "title": "t"}],
            }
            for i in range(12)
        ]
        result = db.compose_morning(_payload(pir=pir), base_url=BASE, budget=1200)
        assert "pir_focus" in result.collapsed
        assert "他 6 領域 → Web" in result.text  # 🔴 を半分に畳む
        assert "高脅威 (要確認" in result.text  # 前の節は残る
        tiny = db.compose_morning(_payload(pir=pir), base_url=BASE, budget=600)
        assert "12 領域 (🔴 12) → Web" in tiny.text

    def test_default_budget_fits_one_message_for_realistic_day(self) -> None:
        pir = [
            {
                "title": f"領域{i}",
                "total": 3,
                "summary": ("あ" * 60 + "、" + "い" * 80 + "。"),
                "matches": [{"importance": "high", "tier": "news", "title": "t"}],
            }
            for i in range(9)
        ]
        payload = _payload(
            pir=pir,
            high_threats=_ht(30),
            pir_brief={
                "total": 9,
                "moved": [_q(claim="あ" * 400 + "。") for _ in range(4)],
                "unassessed": 0,
            },
        )
        assert len(db.compose_morning(payload, base_url=BASE).text) <= db.COMPACT_BUDGET


class TestEvening:
    def test_with_threats_has_summary_threats_headline_link(self) -> None:
        out = db.compose_evening(
            _payload(pir_brief=None), prior_headline="別の見出し", base_url=BASE
        ).text
        assert "朝以降の高脅威" in out and "状況総括" in out and "/app/daily-brief" in out
        assert "PIR" not in out

    def test_short_line_when_nothing_changed(self) -> None:
        payload = _payload(high_threats=_ht(0))
        out = db.compose_evening(
            payload, prior_headline="見出しの一文目。二文目。", base_url=BASE
        ).text
        assert out.startswith("🌆 夕の更新") and len(out.splitlines()) == 2

    def test_not_short_when_headline_differs(self) -> None:
        payload = _payload(high_threats=_ht(0))
        out = db.compose_evening(payload, prior_headline="違う", base_url=BASE).text
        assert not out.startswith("🌆")

    def test_not_short_when_threats_exist_even_if_same_headline(self) -> None:
        out = db.compose_evening(
            _payload(), prior_headline="見出しの一文目。二文目。", base_url=BASE
        ).text
        assert not out.startswith("🌆")


@pytest.mark.parametrize("val,expected", [("0", False), ("1", True), ("", True)])
def test_flag(monkeypatch: pytest.MonkeyPatch, val: str, expected: bool) -> None:
    monkeypatch.setenv(db.ENV_FLAG, val)
    assert db.is_enabled() is expected


def test_runner_uses_legacy_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.pipeline.runners import _compose_discord_summary

    monkeypatch.setenv(db.ENV_FLAG, "0")
    out = _compose_discord_summary(
        slot="morning", payload=_payload(), bursts=[], base_url=None, legacy=lambda: "LEGACY"
    )
    assert out == "LEGACY"


def test_runner_evening_short_line_uses_prior_morning_headline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.pipeline import runners

    monkeypatch.delenv(db.ENV_FLAG, raising=False)
    monkeypatch.setattr(runners, "_latest_morning_headline", lambda: "見出しの一文目。二文目。")
    out = runners._compose_discord_summary(
        slot="evening",
        payload=_payload(high_threats=_ht(0), pir_brief=None),
        bursts=[],
        base_url=None,
        legacy=lambda: "LEGACY",
    )
    assert out.startswith("🌆 夕の更新")
