"""scripts/sft_gate_n20_schemas.py (n20 の学習の形の検査) の単体テスト。

H2 (11-05): 検査は本番の関数を import して同じ判定にする。入力は本番の seam で描いた
実際のプロンプト (fixture の記事・判定・候補) を使い、本番の関門と結果が揃うことを固定する。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import sft_gate_n20_schemas as n20  # noqa: E402
import sft_quality_gate as gate  # noqa: E402

from src.digest import llm_digest  # noqa: E402
from src.digest.recap_points import TEMPLATE_V3, prompt_article_ids  # noqa: E402
from src.eventnews import fact_anchor  # noqa: E402
from src.eventnews.generator import (  # noqa: E402
    _PROMPT_TEMPLATE_V7,
    build_prompt,
    parse_prompt_member_texts,
    prompt_member_texts,
    select_members,
)
from src.spotlight import staged  # noqa: E402
from src.synthesis.grounded import slot_render  # noqa: E402
from src.synthesis.grounded.render import build_render_plan  # noqa: E402
from tests.unit.n20_t5_fixtures import ALLOWED, estimate, members  # noqa: E402

ANCHOR_1 = "versions up to 9.1 are affected and released a patch"
ANCHOR_2 = "linked the intrusion to a known group"
EVENT_USER = build_prompt(members(), ALLOWED, template=_PROMPT_TEMPLATE_V7)


def _fact(**over: Any) -> dict[str, Any]:
    base = {
        "text": "9.1 までの版が影響を受けると同社は述べた。",
        "source_index": 1,
        "paragraph": 1,
        "section": "what",
        "anchor": ANCHOR_1,
    }
    return {**base, **over}


def _event(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "headline": "h", "bluf": "b", "key_points": [], "facts": [_fact()],
        "discrepancies": [], "caveats": [], "unknowns": [],
    }  # fmt: skip
    return {**base, **over}


class TestEventNews:
    def test_prompt_texts_round_trip(self) -> None:
        selected, _ = select_members(members())
        want = {k: v.strip() for k, v in prompt_member_texts(selected).items()}
        assert parse_prompt_member_texts(EVENT_USER) == want

    def test_valid_passes(self) -> None:
        assert n20.schema_errors_event_news(_event(), EVENT_USER) == []

    def test_anchor_from_another_source_fails_like_production(self) -> None:
        # [2] の行に [1] の原文 — 旧の検査はプロンプト全体で照合して通していた
        row = _fact(source_index=2, text="同機関は侵入を既知の集団に結び付けたとされる。")
        errs = n20.schema_errors_event_news(_event(facts=[_fact(), row]), EVENT_USER)
        assert "anchor_not_found:facts[1]" in errs

    def test_anchor_normalized_and_stripped_length(self) -> None:
        loose = _fact(anchor="  VERSIONS  UP TO ９.１ are affected and released a patch  ")
        assert n20.schema_errors_event_news(_event(facts=[loose]), EVENT_USER) == []
        short = _fact(anchor="   up to 9.1 are             ")
        assert "anchor_length:facts[0]" in n20.schema_errors_event_news(
            _event(facts=[short]), EVENT_USER
        )

    def test_number_and_hedge_rules_are_the_production_ones(self) -> None:
        bad_num = _fact(text="9.2 までの版が影響を受ける。")
        hedge = _fact(
            source_index=1,
            text="攻撃者は欠陥を悪用した。",
            anchor="the attackers allegedly exploited a flaw in the VPN appliance",
        )
        errs = n20.schema_errors_event_news(_event(facts=[bad_num], caveats=[hedge]), EVENT_USER)
        assert "number_unsupported:facts[0]" in errs
        assert "hedge_dropped:caveats[0]" in errs

    def test_fields_and_source_index(self) -> None:
        missing = {k: v for k, v in _fact().items() if k != "anchor"}
        no_para = {k: v for k, v in _fact().items() if k != "paragraph"}
        errs = n20.schema_errors_event_news(
            _event(caveats=[missing, no_para], facts=[_fact(source_index=9)]), EVENT_USER
        )
        assert "anchor_missing:caveats[0]" in errs
        assert "row_fields:caveats[1]" in errs
        assert "source_index_out_of_range:facts[0]" in errs

    def test_unknown_identifier_token_fails(self) -> None:
        errs = n20.schema_errors_event_news(_event(bluf="see {I7}"), EVENT_USER)
        assert "identifier_token_not_in_catalog" in errs

    def test_anchored_completion_passes_the_gate(self) -> None:
        from src.eventnews.anchored_completion import anchored_completion

        raw = fact_anchor.AnchoredEventNewsDraft.model_validate(_event())
        out = anchored_completion(raw, members())
        assert out is not None
        assert (
            n20.schema_errors_event_news(json.loads(fact_anchor.completion_json(out)), EVENT_USER)
            == []
        )


# --- spotlight ----------------------------------------------------------------

_SPOT_ANCHOR = "CISA added the flaw to the KEV catalog after exploitation"


class _Pir:
    id = "pir_t"
    title = "t"
    description = "d"
    strong_signals = type("S", (), {"actors": [], "sectors": [], "countries": []})()


def _cands(n: int) -> list[dict[str, Any]]:
    return [
        {
            "title": f"Event {i}",
            "summary": f"{_SPOT_ANCHOR} number {i}.",
            "reliability": "news",
            "ttps": [],
            "article_id": f"a{i}",
            "url": f"https://kuebiko.example/{i}",
        }
        for i in range(1, n + 1)
    ]


def _stage_prompt(stage: str, n: int = 8, chosen: tuple[int, ...] = ()) -> str:
    return staged.build_stage_prompt(
        stage,
        chosen=[{"index": i, "title": f"Event {i}", "rationale": "r"} for i in chosen],
        pir=_Pir(),
        period_label="p",
        candidate_articles=_cands(n),
        ledger_situations=[],
    )


def _ke(i: int) -> dict[str, Any]:
    return {"index": i, "rationale": "r", "anchor": f"{_SPOT_ANCHOR} number {i}"}


def _spot(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "headline": "h",
        "key_events": [_ke(i) for i in range(1, 6)],
        "event_notes": [{"index": 1, "kind": "caveat", "text": "t", "anchor": _SPOT_ANCHOR}],
        "outlook_points": [{"aspect": "a", "text": "x"}, {"aspect": "b", "text": "y"}],
    }
    return {**base, **over}


class TestSpotlight:
    def test_prompt_candidates_round_trip(self) -> None:
        cands = staged.prompt_candidates(_stage_prompt("events"))
        assert [c["title"] for c in cands] == [f"Event {i}" for i in range(1, 9)]
        assert cands[0]["summary"] == f"{_SPOT_ANCHOR} number 1."
        assert staged.prompt_chosen_indexes(_stage_prompt("notes", chosen=(2, 5))) == [2, 5]

    def test_merged_valid_passes(self) -> None:
        assert n20.schema_errors_spotlight(_spot(), _stage_prompt("events")) == []

    def test_merged_rejects_c_speculation_over_max_certainty(self) -> None:
        points = [{"aspect": a, "text": "確実に起きる"} for a in ("a", "b", "c", "a")]
        errs = n20.schema_errors_spotlight(
            _spot(outlook_points=points, speculation=[]), _stage_prompt("events")
        )
        assert "forbidden_key:speculation" in errs
        assert "outlook_points_over_max:4" in errs
        assert "outlook_aspect_out_of_range:c" in errs
        assert "banned_word:確実に" in errs

    def test_index_range_duplicates_count(self) -> None:
        events = [{"index": 99, "rationale": "r", "anchor": _SPOT_ANCHOR}] * 2
        errs = n20.schema_errors_spotlight(_spot(key_events=events), _stage_prompt("events"))
        assert "key_events_count:2" in errs
        assert "key_events_index_out_of_range" in errs
        assert "key_events_index_duplicate" in errs

    @pytest.mark.parametrize(("n", "k", "ok"), [(3, 3, True), (3, 2, False), (4, 4, True)])
    def test_fewer_than_five_candidates(self, n: int, k: int, ok: bool) -> None:
        j = {"headline": "h", "key_events": [_ke(i) for i in range(1, k + 1)]}
        errs = n20.schema_errors_spotlight_events(j, _stage_prompt("events", n=n))
        assert (errs == []) is ok

    def test_event_notes_only_for_chosen_events(self) -> None:
        note = {"index": 7, "kind": "caveat", "text": "t", "anchor": f"{_SPOT_ANCHOR} number 7"}
        errs = n20.schema_errors_spotlight(_spot(event_notes=[note]), _stage_prompt("events"))
        assert "event_notes_index_not_chosen:0" in errs
        stage = n20.schema_errors_spotlight_notes(
            {"event_notes": [note]}, _stage_prompt("notes", chosen=(1, 2))
        )
        assert stage == ["event_notes_index_not_chosen:0"]

    def test_anchor_must_be_in_the_indexed_candidate(self) -> None:
        wrong = {"index": 2, "rationale": "r", "anchor": f"{_SPOT_ANCHOR} number 1"}
        errs = n20.schema_errors_spotlight_events(
            {"headline": "h", "key_events": [_ke(1), wrong, _ke(3), _ke(4), _ke(5)]},
            _stage_prompt("events"),
        )
        assert errs == ["anchor_not_verbatim:key_events[1]"]

    def test_stage_examples(self) -> None:
        notes = {
            "event_notes": [
                {
                    "index": 2,
                    "kind": "unknown",
                    "text": "経路は不明。",
                    "anchor": f"{_SPOT_ANCHOR} number 2",
                }
            ]
        }
        assert n20.schema_errors_spotlight_notes(notes, _stage_prompt("notes", chosen=(2,))) == []
        outlook = {"outlook_points": [{"aspect": "a", "text": "続く"}]}
        assert n20.schema_errors_spotlight_outlook(outlook, _stage_prompt("outlook")) == []
        assert "extra_key:headline" in n20.schema_errors_spotlight_outlook(
            {**outlook, "headline": "x"}, _stage_prompt("outlook")
        )


# --- synthesis -----------------------------------------------------------------


def _plan() -> Any:
    return build_render_plan(
        est=estimate(), period_label="p", cot_notes=False, template=slot_render.TEMPLATE_V9
    )


def _synth(ids: list[str], **over: Any) -> dict[str, Any]:
    items = [
        {"judgment_id": i, "world_clause": "侵入が続いた", "implication_clause": "警戒が要る"}
        for i in ids
    ]
    return {**{"headline_clause": "h", "items": items, "watch_clause": "w"}, **over}


class TestSynthesis:
    def test_wanted_ids_match_the_plan(self) -> None:
        plan = _plan()
        assert slot_render.prompt_wanted_ids(plan.prompt) == [*plan.moved_ids, *plan.standing_ids]
        notes = slot_render.prompt_delta_notes(plan.prompt)
        assert notes == dict.fromkeys(plan.moved_ids, "新たな報告があった")

    def test_full_coverage_passes(self) -> None:
        plan = _plan()
        wanted = [*plan.moved_ids, *plan.standing_ids]
        assert n20.schema_errors_synthesis(_synth(wanted), plan.prompt) == []

    def test_missing_unknown_and_order(self) -> None:
        plan = _plan()
        wanted = [*plan.moved_ids, *plan.standing_ids]
        errs = n20.schema_errors_synthesis(_synth([*wanted[1:], "s-zzz"]), plan.prompt)
        assert f"uncovered:{wanted[0]}" in errs
        assert f"judgment_id_not_in_input:{len(wanted) - 1}" in errs
        errs = n20.schema_errors_synthesis(_synth(list(reversed(wanted))), plan.prompt)
        assert "items_order" in errs

    @pytest.mark.parametrize("word", ["可能性が高い", "とみられる", "確証", "反証"])
    def test_banned_in_clause_is_the_production_list(self, word: str) -> None:
        plan = _plan()
        wanted = [*plan.moved_ids, *plan.standing_ids]
        errs = n20.schema_errors_synthesis(_synth(wanted, watch_clause=word), plan.prompt)
        assert f"banned_word:{word}" in errs

    def test_causal_connector_not_in_delta_note(self) -> None:
        plan = _plan()
        wanted = [*plan.moved_ids, *plan.standing_ids]
        j = _synth(wanted)
        j["items"][0]["world_clause"] = "報告を受けて侵入が拡大した"
        assert "causal_connector:0" in n20.schema_errors_synthesis(j, plan.prompt)


# --- deep_dive -----------------------------------------------------------------


def _dd_cand(article_id: str) -> Any:
    from src.digest.db_filter import DigestCandidate

    return DigestCandidate(
        article_id=article_id, title=f"題 {article_id}", url=f"https://kuebiko.example/{article_id}",
        feed_title="Feed", summary="要約", importance="high", category="apt",
        posted_channel="watch", dedup_key=None, created_at="2026-10-20T00:00:00Z",
        discord_channel_id=None, discord_message_id=None,
    )  # fmt: skip


def _dd_user() -> str:
    return llm_digest._render_prompt(
        TEMPLATE_V3, candidates=[_dd_cand("art-1"), _dd_cand("art-2")], period_label="p"
    )


LONG = "要点の本文。" * 80


def _scored(no: int, **over: Any) -> dict[str, Any]:
    base = {"no": no, "scores": {"pir": 3, "roi": 2, "novelty": 1}, "rationale": "r"}
    return {**base, **over}


class TestDeepDive:
    def test_prompt_ids_round_trip(self) -> None:
        assert prompt_article_ids(_dd_user()) == ["art-1", "art-2"]

    def test_valid_passes(self) -> None:
        sec = {"heading": "見出し", "points": [{"text": LONG, "article_ids": ["art-1", "art-2"]}]}
        assert n20.schema_errors_deep_dive({"sections": [sec]}, _dd_user()) == []

    def test_uncited_thin_heading_and_unknown_id(self) -> None:
        bad = {"heading": "x" * 41, "points": [{"text": "短い", "article_ids": ["art-9", "art-1"]}]}
        errs = n20.schema_errors_deep_dive({"sections": [bad]}, _dd_user())
        assert "heading_length:0" in errs
        assert "article_id_not_in_input:0.0" in errs
        assert "uncited_article:art-2" in errs
        assert any(e.startswith("thin_section:") for e in errs)

    def test_select_valid_and_invalid(self) -> None:
        ok = {"scored_articles": [_scored(1), _scored(2)]}
        assert n20.schema_errors_deep_dive_select(ok, "") == []
        bad = {
            "scored_articles": [_scored(1), _scored(1, scores={"pir": 9, "roi": 0, "novelty": 0})]
        }
        errs = n20.schema_errors_deep_dive_select(bad, "")
        assert "no_duplicate" in errs and "score_out_of_range:1" in errs


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
        gate._make_row(
            "train",
            4,
            _row("spotlight_outlook", _stage_prompt("outlook"), '{"outlook_points": []}'),
        ),
    ]
    res = gate.check_schema(rows)
    assert res.uninspectable == {}
    assert res.violations == 2  # 空 bluf の event_news と JSON の和訳
