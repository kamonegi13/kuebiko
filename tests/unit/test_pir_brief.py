"""PIR ブリーフ (常設の問いの日次の答え) — 段D。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from src.digest.pir_brief import (
    build_pir_brief,
    format_pir_brief_compact,
    format_pir_brief_full,
    pir_brief_payload,
)

NOW = datetime(2026, 9, 29, 0, 0, tzinfo=UTC)


def _card(
    sid: str,
    *,
    question: str = "問い",
    trajectory: list[dict[str, Any]] | None = None,
    assessed: bool = True,
    confidence: str = "low",
    claim: str = "答え",
    last_evidence_at: str = "2026-09-28T20:00:00+00:00",
    indicators: list[dict[str, Any]] | None = None,
    fired: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "situation_id": sid,
        "question": question,
        "assessed": assessed,
        "claim": claim,
        "confidence": confidence,
        "leading_label": "",
        "last_evidence_at": last_evidence_at,
        "indicators": indicators or [],
        "fired_indicators": fired or [],
        "missing_evidence": [],
        "trajectory": trajectory or [],
    }


def _rev(at: str, delta: str, reason: str = "", conf: str = "low") -> dict[str, Any]:
    return {
        "rev": 1,
        "at": at,
        "confidence": conf,
        "delta_type": delta,
        "note": reason,
        "reason": reason,
    }


class TestBuild:
    def test_counts_only_moves_inside_window(self) -> None:
        # Arrange: 24h 内の強化 / 6 日前の更新 (最新だが古い) / 24h 内でも継続
        cards = [
            _card(
                "a",
                trajectory=[_rev("2026-09-28T21:30:00+00:00", "strengthened", "確度 low→moderate")],
            ),
            _card(
                "b",
                trajectory=[_rev("2026-09-23T18:42:00+00:00", "claim_revised", "claim 文言を改訂")],
            ),
            _card("c", trajectory=[_rev("2026-09-28T22:00:00+00:00", "no_change")]),
        ]

        # Act
        brief = build_pir_brief(cards, now=NOW)

        # Assert
        assert brief.total == 3
        assert [q.situation_id for q in brief.moved] == ["a"]
        assert {q.situation_id for q in brief.still} == {"b", "c"}

    def test_earlier_move_in_window_counts_even_if_latest_is_no_change(self) -> None:
        cards = [
            _card(
                "a",
                trajectory=[
                    _rev("2026-09-28T07:00:00+00:00", "escalated", "被害・標的の拡大を観測"),
                    _rev("2026-09-28T23:00:00+00:00", "no_change"),
                ],
            )
        ]

        brief = build_pir_brief(cards, now=NOW)

        assert len(brief.moved) == 1
        assert brief.moved[0].moves[0].delta_type == "escalated"
        assert brief.moved[0].moves[0].reason == "被害・標的の拡大を観測"

    def test_unassessed_is_counted_separately(self) -> None:
        brief = build_pir_brief([_card("a", assessed=False)], now=NOW)

        assert brief.unassessed == 1
        assert brief.moved == () and brief.still == ()

    def test_freshness_and_open_indicators(self) -> None:
        card = _card(
            "a",
            last_evidence_at="2026-09-19T00:00:00+00:00",
            indicators=[
                {"indicator": "x", "status": "open"},
                {"indicator": "y", "status": "hit"},
                {"indicator": "z", "status": "open"},
            ],
        )

        brief = build_pir_brief([card], now=NOW)

        assert brief.still[0].days_since_evidence == 10
        assert brief.still[0].open_indicators == 2

    def test_naive_timestamp_is_read_as_utc(self) -> None:
        cards = [_card("a", trajectory=[_rev("2026-09-28 21:30:00", "weakened")])]

        assert len(build_pir_brief(cards, now=NOW).moved) == 1

    def test_broken_timestamp_does_not_count_as_moved(self) -> None:
        cards = [_card("a", trajectory=[_rev("not-a-date", "weakened")], last_evidence_at="")]

        brief = build_pir_brief(cards, now=NOW)

        assert brief.moved == ()
        assert brief.still[0].days_since_evidence is None


class TestFormat:
    def _brief(self) -> Any:
        cards = [
            _card(
                "a",
                question="北朝鮮は事前配置を進めているか",
                claim="直接証拠は確認されていない",
                confidence="moderate",
                trajectory=[_rev("2026-09-28T21:30:00+00:00", "strengthened", "確度 low→moderate")],
                fired=["IT 労働者の侵入"],
            ),
            _card("b", question="中国は事前配置を進めているか", claim="確認されていない"),
        ]
        return build_pir_brief(cards, now=NOW)

    def test_header_states_how_many_moved(self) -> None:
        text = format_pir_brief_full(self._brief())

        assert "本日、2 問中 1 問の答えが動いた" in text

    def test_moved_question_comes_first_with_reason(self) -> None:
        text = format_pir_brief_full(self._brief())

        moved_at = text.index("北朝鮮は事前配置を進めているか")
        still_at = text.index("中国は事前配置を進めているか")
        assert moved_at < still_at
        assert "強化" in text and "確度 low→moderate" in text
        assert "IT 労働者の侵入" in text

    def test_quiet_day_says_nothing_moved(self) -> None:
        brief = build_pir_brief([_card("b")], now=NOW)

        assert "本日、1 問中 0 問の答えが動いた" in format_pir_brief_full(brief)
        assert "0 問" in format_pir_brief_compact(brief)

    def test_empty_input_renders_nothing(self) -> None:
        brief = build_pir_brief([], now=NOW)

        assert format_pir_brief_full(brief) == ""
        assert format_pir_brief_compact(brief) == ""

    def test_compact_lists_moved_only(self) -> None:
        text = format_pir_brief_compact(self._brief())

        assert "北朝鮮は事前配置を進めているか" in text
        assert "中国は事前配置を進めているか" not in text

    def test_payload_shape(self) -> None:
        payload = pir_brief_payload(self._brief())

        assert payload["total"] == 2
        assert payload["moved_count"] == 1
        assert payload["moved"][0]["moves"][0]["label"] == "強化"
        assert payload["still"][0]["days_since_evidence"] == 0


class TestTrajectoryWindow:
    """問いの推移は 30 日分 (月次の状況総括を畳んだ長期の軌跡)。"""

    def test_keeps_only_last_30_days(self) -> None:
        from src.ui.services.standing_posture import _within_trajectory_window

        def rev(at: str) -> tuple[object, ...]:
            return (1, "", "", "low", "no_change", at)

        revs = [rev("2026-08-01T00:00:00+00:00"), rev("2026-09-10T00:00:00+00:00")]

        kept = _within_trajectory_window(revs, now=NOW)

        assert [r[5] for r in kept] == ["2026-09-10T00:00:00+00:00"]

    def test_latest_revision_is_kept_even_if_old(self) -> None:
        from src.ui.services.standing_posture import _within_trajectory_window

        revs = [(1, "", "", "low", "no_change", "2026-07-01T00:00:00+00:00")]

        assert _within_trajectory_window(revs, now=NOW) == revs
