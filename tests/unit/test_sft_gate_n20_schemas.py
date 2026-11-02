"""scripts/sft_gate_n20_schemas.py (n20 契約 T1 の出力検査) の単体テスト。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import sft_gate_n20_schemas as n20  # noqa: E402
import sft_quality_gate as gate  # noqa: E402

ANCHOR = "The attackers exploited a flaw in the VPN appliance firmware"
EVENT_USER = f"[1] **Source A**\n{ANCHOR} to gain initial access.\nI1 = CVE-2026-1111\n"
SPOT_USER = "\n".join(f"[{i}] **Event {i}** {ANCHOR} number {i}" for i in range(1, 9))


def _fact(**over: Any) -> dict[str, Any]:
    base = {"text": "t", "source_index": 1, "paragraph": 1, "section": "what", "anchor": ANCHOR}
    return {**base, **over}


def _event(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "headline": "h", "bluf": "b", "key_points": [], "facts": [_fact()],
        "discrepancies": [], "caveats": [], "unknowns": [],
    }  # fmt: skip
    return {**base, **over}


def test_event_news_valid_passes() -> None:
    assert n20.schema_errors_event_news(_event(), EVENT_USER) == []


def test_event_news_anchor_not_verbatim_and_short_fail() -> None:
    errs = n20.schema_errors_event_news(
        _event(facts=[_fact(anchor="x" * 30), _fact(anchor="short")]), EVENT_USER
    )
    assert "anchor_not_verbatim:facts[0]" in errs
    assert "anchor_length:facts[1]" in errs


def test_event_news_missing_anchor_and_bad_source_index_fail() -> None:
    bad = {k: v for k, v in _fact(source_index=9).items() if k != "anchor"}
    errs = n20.schema_errors_event_news(
        _event(caveats=[bad], facts=[_fact(source_index=9)]), EVENT_USER
    )
    assert "anchor_missing:caveats[0]" in errs
    assert "source_index_out_of_range:facts[0]" in errs


def test_event_news_unknown_identifier_token_fails() -> None:
    errs = n20.schema_errors_event_news(_event(bluf="see {I7}"), EVENT_USER)
    assert "identifier_token_not_in_catalog" in errs


def _spot(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "headline": "h",
        "key_events": [{"index": i, "rationale": "r", "anchor": ANCHOR} for i in range(1, 6)],
        "event_notes": [{"index": 1, "kind": "k", "text": "t", "anchor": ANCHOR}],
        "outlook_points": [{"aspect": "a", "text": "x"}, {"aspect": "b", "text": "y"}],
    }
    return {**base, **over}


def test_spotlight_valid_passes() -> None:
    assert n20.schema_errors_spotlight(_spot(), SPOT_USER) == []


def test_spotlight_rejects_aspect_c_speculation_over_max_and_certainty() -> None:
    points = [{"aspect": a, "text": "確実に起きる"} for a in ("a", "b", "c", "a")]
    errs = n20.schema_errors_spotlight(_spot(outlook_points=points, speculation=[]), SPOT_USER)
    assert "forbidden_key:speculation" in errs
    assert "outlook_points_over_max:4" in errs
    assert "outlook_aspect_out_of_range:c" in errs
    assert "banned_word:確実に" in errs


def test_spotlight_index_range_duplicates_and_count_fail() -> None:
    events = [{"index": 99, "rationale": "r", "anchor": ANCHOR}] * 2
    errs = n20.schema_errors_spotlight(_spot(key_events=events), SPOT_USER)
    assert "key_events_count:2" in errs
    assert "key_events_index_out_of_range" in errs
    assert "key_events_index_duplicate" in errs


def _synth(**over: Any) -> dict[str, Any]:
    item = {"judgment_id": "s-abc", "world_clause": "w", "implication_clause": "i"}
    return {**{"headline_clause": "h", "items": [item], "watch_clause": "w"}, **over}


def test_synthesis_valid_passes_and_unknown_id_or_banned_fails() -> None:
    user = "判定 [s-abc] の材料"
    assert n20.schema_errors_synthesis(_synth(), user) == []
    bad = {"judgment_id": "s-zzz", "world_clause": "反証あり", "implication_clause": "i"}
    errs = n20.schema_errors_synthesis(_synth(items=[bad, bad]), user)
    assert "judgment_id_not_in_input:0" in errs
    assert "judgment_id_duplicate:1" in errs
    assert "banned_word:反証" in errs


def _scored(no: int, **over: Any) -> dict[str, Any]:
    base = {"no": no, "scores": {"pir": 3, "roi": 2, "novelty": 1}, "rationale": "r"}
    return {**base, **over}


def test_deep_dive_select_valid_and_invalid() -> None:
    ok = {"scored_articles": [_scored(1), _scored(2)]}
    assert n20.schema_errors_deep_dive_select(ok, "") == []
    bad = {"scored_articles": [_scored(1), _scored(1, scores={"pir": 9, "roi": 0, "novelty": 0})]}
    errs = n20.schema_errors_deep_dive_select(bad, "")
    assert "no_duplicate" in errs
    assert "score_out_of_range:1" in errs
    over = {"scored_articles": [_scored(1)] * 51}
    assert "scored_articles_over_max:51" in n20.schema_errors_deep_dive_select(over, "")


def test_deep_dive_valid_and_invalid() -> None:
    user = "ids: art-1 art-2"
    sec = {"heading": "見出し", "points": [{"text": "要点", "article_ids": ["art-1"]}]}
    assert n20.schema_errors_deep_dive({"sections": [sec]}, user) == []
    bad = {"heading": "x" * 41, "points": [{"text": "t", "article_ids": ["art-9"]}]}
    errs = n20.schema_errors_deep_dive({"sections": [bad]}, user)
    assert "heading_length:0" in errs
    assert "article_id_not_in_input:0.0" in errs


def _row(task: str, user: str, out: str) -> dict[str, Any]:
    return {
        "messages": [
            {"role": "user", "content": f"[task: {task}]\n{user}"},
            {"role": "assistant", "content": out},
        ]
    }


def test_gate_check_schema_inspects_n20_tasks_and_translation_non_json() -> None:
    rows = [
        gate._make_row("train", 0, _row("event_news", EVENT_USER, json.dumps(_event()))),
        gate._make_row("train", 1, _row("event_news", EVENT_USER, json.dumps(_event(bluf="")))),
        gate._make_row("train", 2, _row("translate", "src", "これは訳文です")),
        gate._make_row("train", 3, _row("actor_sync", "src", '{"a": "b"}')),
    ]
    res = gate.check_schema(rows)
    assert res.uninspectable == {}
    assert res.violations == 2  # 空 bluf の event_news と JSON の和訳
