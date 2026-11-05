"""事象ニュースの anchor の関門 (n20 T5、旗 FACT_ANCHOR)。LLM は fake。"""

from __future__ import annotations

import json
from typing import Any

import pytest

from src.eventnews import fact_anchor
from src.eventnews.fact_anchor import AnchoredEventNewsDraft, AnchoredFactItem
from src.eventnews.models import EventNewsDraft
from src.tools.llm_client import LLMError
from tests.unit.n20_t5_fixtures import ALLOWED, BODY_1, BODY_2, members

TEXTS = {1: BODY_1, 2: BODY_2}
ANCHOR_HEDGED = "the attackers allegedly exploited a flaw in the VPN appliance"
ANCHOR_RANGE = "versions up to 9.1 are affected and released a patch"


def _row(text: str, anchor: str, *, idx: int = 1) -> AnchoredFactItem:
    return AnchoredFactItem(text=text, source_index=idx, paragraph=1, section="what", anchor=anchor)


def _draft(**over: Any) -> AnchoredEventNewsDraft:
    base: dict[str, Any] = {
        "headline": "h",
        "bluf": "b",
        "facts": [_row("9.1 までの版が影響を受ける。", ANCHOR_RANGE)],
    }
    base.update(over)
    return AnchoredEventNewsDraft(**base)


class TestLineIssues:
    def test_verbatim_anchor_passes(self) -> None:
        assert (
            fact_anchor.line_issues(_row("9.1 までの版が影響を受ける。", ANCHOR_RANGE), TEXTS) == []
        )

    def test_anchor_matches_across_width_and_case(self) -> None:
        row = _row(
            "9.1 までの版が影響。", "VERSIONS  UP TO ９.１ are affected and released a patch"
        )
        assert fact_anchor.line_issues(row, TEXTS) == []

    def test_anchor_from_another_source_is_rejected(self) -> None:
        # 出典ずれ: [2] の行に [1] の原文
        assert fact_anchor.line_issues(_row("t", ANCHOR_RANGE, idx=2), TEXTS) == [
            "anchor_not_found"
        ]

    def test_absence_claim_may_anchor_any_source(self) -> None:
        row = _row("帰属は確認されていない。", "the attribution has not been confirmed", idx=0)
        assert fact_anchor.line_issues(row, TEXTS) == []

    @pytest.mark.parametrize("anchor", ["short", "x" * 121, ""])
    def test_anchor_length(self, anchor: str) -> None:
        assert fact_anchor.line_issues(_row("t", anchor), TEXTS) == ["anchor_length"]

    def test_hedge_must_carry_into_text(self) -> None:
        assert fact_anchor.line_issues(_row("攻撃者は欠陥を悪用した。", ANCHOR_HEDGED), TEXTS) == [
            "hedge_dropped"
        ]
        assert (
            fact_anchor.line_issues(_row("攻撃者は欠陥を悪用したとされる。", ANCHOR_HEDGED), TEXTS)
            == []
        )

    def test_month_may_is_not_a_hedge(self) -> None:
        assert not fact_anchor.has_hedge("released in May 2026")
        assert fact_anchor.has_hedge("the flaw may be exploited")

    def test_number_not_in_source_is_flagged(self) -> None:
        # 版の上限 9.1 を 9.2 と書いた (取り違え) — 本文のどこにも無い
        assert fact_anchor.line_issues(_row("9.2 までの版が影響。", ANCHOR_RANGE), TEXTS) == [
            "number_unsupported"
        ]

    def test_number_elsewhere_in_same_source_passes(self) -> None:
        # H3 (a): anchor の外でも、同じ記事の本文に在る数値は通す (1,200 台)
        row = _row("9.1 までの版が影響し、約 1200 台が露出していた。", ANCHOR_RANGE)
        assert fact_anchor.line_issues(row, TEXTS) == []

    def test_number_from_another_source_is_flagged_for_facts(self) -> None:
        # [2] の記事の行に [1] の数値 (1,200) を書いた — 出典ずれの数値は落とす
        row = _row(
            "同機関は約 1200 台に影響したと述べた。", "linked the intrusion to a known group", idx=2
        )
        assert fact_anchor.line_issues(row, TEXTS) == ["number_unsupported"]

    def test_discrepancy_may_cite_the_other_sides_number(self) -> None:
        row = _row(
            "[2] は台数に触れず、[1] は 1200 台とする。",
            "linked the intrusion to a known group",
            idx=2,
        )
        assert fact_anchor.line_issues(row, TEXTS, section="discrepancies") == []
        bad = _row("[2] は 3400 台とする。", "linked the intrusion to a known group", idx=2)
        assert fact_anchor.line_issues(bad, TEXTS, section="discrepancies") == [
            "number_unsupported"
        ]

    def test_scale_words_months_and_number_words_are_normalized(self) -> None:
        src = "About 1.2 million users were hit on October 15, 2026; twelve agencies responded."
        assert fact_anchor.unsupported_numbers("120万人が 2026年10月15日 に被害。", src) == set()
        assert fact_anchor.unsupported_numbers("12 機関が対応した。", src) == set()
        assert fact_anchor.unsupported_numbers("130万人が被害。", src) == {"1300000"}
        assert fact_anchor.unsupported_numbers("2026年11月に公表。", src) == {"11"}

    def test_identifier_tokens_and_single_digits_are_not_checked(self) -> None:
        assert fact_anchor.unsupported_numbers("{I12} は 9.1 まで", BODY_1) == set()
        assert fact_anchor.unsupported_numbers("3 件の版", BODY_1) == set()


class TestApplyGate:
    def test_drop_removes_bad_rows_and_strips_anchor(self) -> None:
        draft = _draft(
            facts=[
                _row("9.1 までの版が影響を受ける。", ANCHOR_RANGE),
                _row("別記事の文", ANCHOR_RANGE, idx=2),
            ],
            caveats=[_row("攻撃者は悪用したとされる。", ANCHOR_HEDGED)],
        )
        res = fact_anchor.apply_anchor_gate(draft, TEXTS, drop=True)
        assert isinstance(res.draft, EventNewsDraft)
        assert [f.text for f in res.draft.facts] == ["9.1 までの版が影響を受ける。"]
        assert len(res.draft.caveats) == 1
        assert res.dropped == 1
        assert "anchor" not in json.loads(res.draft.model_dump_json())["facts"][0]
        # 教師用: 同じ行を anchor つきで持つ
        assert [f.anchor for f in res.anchored.facts] == [ANCHOR_RANGE]
        assert len(res.anchored.caveats) == 1

    def test_no_drop_keeps_rows_but_reports(self) -> None:
        draft = _draft(facts=[_row("x", "nope nope nope nope nope nope")])
        res = fact_anchor.apply_anchor_gate(draft, TEXTS, drop=False)
        assert len(res.draft.facts) == 1 and res.dropped == 0
        assert fact_anchor.issue_counts(res.issues) == {"anchor_not_found": 1}

    def test_schema_requires_anchor(self) -> None:
        schema = AnchoredEventNewsDraft.model_json_schema()
        item = schema["$defs"]["AnchoredFactItem"]
        assert set(item["required"]) == {"text", "source_index", "paragraph", "section", "anchor"}
        assert set(schema["required"]) >= {"facts", "caveats", "discrepancies"}


class _ScriptedLLM:
    model = "fake"

    def __init__(self, replies: list[AnchoredEventNewsDraft]) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []
        self.schemas: list[type] = []

    async def generate_structured(self, prompt: str, schema: type, **_kw: Any) -> Any:
        self.prompts.append(prompt)
        self.schemas.append(schema)
        return self.replies.pop(0)


@pytest.mark.asyncio
async def test_on_mode_uses_v7_and_returns_plain_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.eventnews.generator import generate_draft

    monkeypatch.setenv("FACT_ANCHOR", "1")
    llm = _ScriptedLLM([_draft()])
    draft = await generate_draft(members(), ALLOWED, llm)  # type: ignore[arg-type]
    assert llm.schemas == [AnchoredEventNewsDraft]
    assert "anchor (根拠の抜き書き)" in llm.prompts[0]
    assert type(draft) is EventNewsDraft and len(draft.facts) == 1


@pytest.mark.asyncio
async def test_hedge_drop_retries_once_then_takes_better(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.eventnews.generator import generate_draft

    monkeypatch.setenv("FACT_ANCHOR", "on")
    bad = _draft(caveats=[_row("攻撃者は悪用した。", ANCHOR_HEDGED)])
    good = _draft(caveats=[_row("攻撃者は悪用したとされる。", ANCHOR_HEDGED)])
    llm = _ScriptedLLM([bad, good])
    draft = await generate_draft(members(), ALLOWED, llm)  # type: ignore[arg-type]
    assert len(llm.prompts) == 2 and "caveats[0]" in llm.prompts[1]
    assert [c.text for c in draft.caveats] == ["攻撃者は悪用したとされる。"]


@pytest.mark.asyncio
async def test_all_facts_dropped_raises_llm_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.eventnews.generator import generate_draft

    monkeypatch.setenv("FACT_ANCHOR", "on")
    llm = _ScriptedLLM([_draft(facts=[_row("t", "not in any source text at all")])])
    with pytest.raises(LLMError):
        await generate_draft(members(), ALLOWED, llm)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_shadow_measure_logs_counts_and_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.eventnews import anchored_generator

    seen: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(anchored_generator._log, "info", lambda e, **kw: seen.append((e, kw)))
    monkeypatch.setattr(anchored_generator._log, "warning", lambda e, **kw: seen.append((e, kw)))
    llm = _ScriptedLLM([_draft()])
    await anchored_generator.shadow_measure(members(), ALLOWED, llm, item_id="ev1")  # type: ignore[arg-type]
    assert seen[0][0] == "eventnews_fact_anchor_shadow" and seen[0][1]["rows"] == 1

    class _Boom:
        model = "x"

        async def generate_structured(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("down")

    await anchored_generator.shadow_measure(members(), ALLOWED, _Boom(), item_id="ev2")  # type: ignore[arg-type]
    assert seen[-1][0] == "eventnews_fact_anchor_shadow_failed"


def test_runner_prompt_version_follows_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.eventnews import runner

    monkeypatch.delenv("FACT_ANCHOR", raising=False)
    assert runner._prompt_version() == "eventnews-v6"
    monkeypatch.setenv("FACT_ANCHOR", "shadow")
    assert runner._prompt_version() == "eventnews-v6"
    monkeypatch.setenv("FACT_ANCHOR", "1")
    assert runner._prompt_version() == "eventnews-v7"


def test_v7_output_passes_the_n20_training_gate_shape() -> None:
    """関門を通す前の v7 の形が、学習の関門 (scripts/sft_gate_n20_schemas.py) の形と一致する。"""
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "sft_gate_n20_schemas", Path("scripts/sft_gate_n20_schemas.py")
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    from src.eventnews.generator import build_prompt

    user = build_prompt(members(), ALLOWED, template="eventnews/refine_v7.j2")
    out = json.loads(_draft().model_dump_json())
    assert mod.schema_errors_event_news(out, user) == []


class TestAnchoredCompletion:
    def test_keeps_anchor_after_both_gates_and_resolves_identifiers_in_text_only(self) -> None:
        from src.eventnews.anchored_completion import anchored_completion

        raw = _draft(
            facts=[
                _row("{I1} の 9.1 までの版が影響を受ける。", ANCHOR_RANGE),
                _row("出典なしの行", ANCHOR_RANGE, idx=0),  # 識別子関門が facts から落とす
                _row("9.2 までの版", ANCHOR_RANGE),  # anchor の関門が落とす
            ],
            caveats=[_row("攻撃者は悪用したとされる。", ANCHOR_HEDGED)],
        )
        out = anchored_completion(raw, members())
        assert out is not None
        assert [f.anchor for f in out.facts] == [ANCHOR_RANGE]
        # 識別子関門は text にだけ当たる (カタログに無い番号は置換)、anchor は原文のまま
        assert "{I1}" not in out.facts[0].text
        assert [c.anchor for c in out.caveats] == [ANCHOR_HEDGED]
        canon = json.loads(fact_anchor.completion_json(out))
        assert list(canon["facts"][0]) == ["text", "source_index", "paragraph", "section", "anchor"]

    def test_none_when_no_fact_survives(self) -> None:
        from src.eventnews.anchored_completion import anchored_completion

        raw = _draft(facts=[_row("t", "not in any source text at all")])
        assert anchored_completion(raw, members()) is None

    def test_template_example_rows_carry_all_fields(self) -> None:
        from pathlib import Path

        tmpl = Path("prompts/eventnews/refine_v7.j2").read_text(encoding="utf-8")
        rows = [ln for ln in tmpl.splitlines() if '"anchor": "出典' in ln]
        assert rows and all('"paragraph"' in ln and '"section"' in ln for ln in rows)
