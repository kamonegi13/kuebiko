"""改訂の含意・指標・欠けている証拠の関門 (LEDGER_IMPL_GROUNDING / LEDGER_JP_IMPLICATION)。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from src.assessment import revision_grounding as rg
from src.assessment.revision_grounding import (
    IMPL_FLAG_ENV,
    JP_FLAG_ENV,
    GateContext,
    apply_revision_gates,
    find_violations,
    has_japan_link,
    normalized_dedupe,
    split_sentences,
    subject_owner_index,
)
from src.assessment.situation_store import RevisionRow, SituationRow
from src.synthesis.grounded.incremental import (
    NO_JAPAN_NOTICE,
    PriorJudgmentView,
    with_no_japan_notice,
)
from src.synthesis.grounded.passes import ClaimAnalysis

OWN_TEXT = (
    "ShadowCrew が Orbit社 のポータルへ侵入し 4,708 件のアカウントを窃取したと報じられた。"
    " CVE-2026-1111 が悪用された。"
)


def _sit(sid: str, *anchors: str) -> SituationRow:
    return SituationRow(
        situation_id=sid,
        title="t",
        domain="cyber_incident",
        status="active",
        anchors=frozenset(anchors),
        pir_ids=(),
        opened_at="",
        last_evidence_at="",
    )


_FOREIGN = {"zephyr": frozenset({"s-2"})}


def _ctx(*, jp: bool = True, owners: dict[str, frozenset[str]] | None = None) -> GateContext:
    return GateContext(
        situation_id="s-1",
        own_text=OWN_TEXT,
        foreign_owners=owners or {},
        has_japan_link=jp,
    )


def _analysis(**kw: object) -> ClaimAnalysis:
    base = ClaimAnalysis(
        evidence=(),
        hypotheses=(),
        leading_hypothesis="criminal_financial",
        llm_confidence="low",
        key_assumptions=(),
        missing_evidence=(),
        indicators=(),
        claim_type="discrete_event",
        implication="",
    )
    return replace(base, **kw)  # type: ignore[arg-type]


def _prev(**kw: str) -> RevisionRow:
    base = {
        "situation_id": "s-1",
        "rev": 1,
        "claim": "c",
        "claim_type": "discrete_event",
        "leading_hypothesis": "criminal_financial",
        "confidence": "low",
        "confidence_basis": "",
        "hypotheses_json": "[]",
        "assumptions_json": "[]",
        "missing_json": json.dumps(["前回の欠けている証拠"]),
        "indicators_json": json.dumps(["前回の指標"]),
        "implication": "前回の含意。",
        "delta_type": "opened",
        "delta_note": "",
        "created_at": "",
    }
    return RevisionRow(**{**base, **kw})  # type: ignore[arg-type]


def test_flag_defaults_to_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(IMPL_FLAG_ENV, raising=False)
    monkeypatch.delenv(JP_FLAG_ENV, raising=False)
    assert rg.impl_grounding_mode() == "0"
    assert rg.jp_implication_mode() == "0"
    monkeypatch.setenv(IMPL_FLAG_ENV, "bogus")
    assert rg.impl_grounding_mode() == "0"


def test_flag_zero_returns_same_object(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(IMPL_FLAG_ENV, raising=False)
    monkeypatch.delenv(JP_FLAG_ENV, raising=False)
    a = _analysis(implication="CVE-9999-0001 が悪用された。", indicators=("A", "A"))
    assert apply_revision_gates(a, _ctx(), _prev()) is a


def test_grounded_sentence_has_no_violation() -> None:
    assert find_violations("ShadowCrew は 4,708 件を窃取した。", _ctx(), check_terms=True) == []


def test_ungrounded_cve_and_number_are_violations() -> None:
    kinds = {
        (v.kind, v.token)
        for v in find_violations(
            "CVE-2026-9999 で 12,345 件が影響を受けた。", _ctx(), check_terms=True
        )
    }
    assert ("ungrounded_term", "CVE-2026-9999") in kinds
    assert ("ungrounded_number", "12345") in kinds


def test_foreign_subject_is_violation_unless_own() -> None:
    owners = subject_owner_index([_sit("s-2", "actor:lazarus"), _sit("s-1", "actor:shadowcrew")])
    ctx = _ctx(owners=owners)
    kinds = [v.kind for v in find_violations("Lazarus の関与が疑われる。", ctx)]
    assert "foreign_subject" in kinds
    assert find_violations("ShadowCrew の関与が疑われる。", ctx) == []


def test_internal_wording_is_violation() -> None:
    assert any(
        v.kind == "internal_wording" for v in find_violations("今回の新着で確認された。", _ctx())
    )


def test_shadow_logs_but_keeps_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(IMPL_FLAG_ENV, "shadow")
    monkeypatch.delenv(JP_FLAG_ENV, raising=False)
    a = _analysis(implication="CVE-2026-9999 が悪用された。", indicators=("A", "A"))
    out = apply_revision_gates(a, _ctx(), _prev())
    assert out.implication == a.implication
    assert out.indicators == ("A", "A")


def test_on_drops_violating_sentence_and_dedupes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(IMPL_FLAG_ENV, "1")
    a = _analysis(
        implication="ShadowCrew は窃取した。CVE-2026-9999 が悪用された。",
        indicators=("監視する Ａ", "監視する A", "Zephyr の観測"),
        missing_evidence=("欠けている証拠", "欠けている 証拠"),
    )
    out = apply_revision_gates(a, _ctx(owners=_FOREIGN), _prev())
    assert out.implication == "ShadowCrew は窃取した。"
    assert out.indicators == ("監視する Ａ",)
    assert out.missing_evidence == ("欠けている証拠",)


def test_on_keeps_previous_when_everything_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(IMPL_FLAG_ENV, "1")
    a = _analysis(implication="CVE-2026-9999 が悪用された。", indicators=("Zephyr の観測",))
    out = apply_revision_gates(a, _ctx(owners=_FOREIGN), _prev())
    assert out.implication == "前回の含意。"
    assert out.indicators == ("前回の指標",)


def test_jp_implication_detected_only_without_japan_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(IMPL_FLAG_ENV, raising=False)
    monkeypatch.setenv(JP_FLAG_ENV, "1")
    a = _analysis(implication="ShadowCrew は窃取した。日本を含む同地域への影響を監視すべき。")
    assert apply_revision_gates(a, _ctx(jp=True), _prev()).implication == a.implication
    out = apply_revision_gates(a, _ctx(jp=False), _prev())
    assert out.implication == "ShadowCrew は窃取した。"


def test_jp_shadow_keeps_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(IMPL_FLAG_ENV, raising=False)
    monkeypatch.setenv(JP_FLAG_ENV, "shadow")
    a = _analysis(implication="国内の組織も注意が必要。")
    assert apply_revision_gates(a, _ctx(jp=False), _prev()).implication == a.implication


def test_has_japan_link() -> None:
    assert has_japan_link(
        claim="x", anchors=[], entity_keys={"involved_country:JP"}, evidence_text=""
    )
    assert has_japan_link(
        claim="x", anchors=["victim_org:株式会社テスト"], entity_keys=[], evidence_text=""
    )
    assert not has_japan_link(
        claim="ShadowCrew が侵入",
        anchors=["actor:shadowcrew"],
        entity_keys=[],
        evidence_text="Orbit breach",
    )


def test_helpers() -> None:
    assert split_sentences("あ。い。") == ["あ。", "い。"]
    assert normalized_dedupe(["A b", "Ａ　ｂ", "", "c"]) == ("A b", "c")


def test_prompt_unchanged_when_no_notice() -> None:
    assert with_no_japan_notice("") == NO_JAPAN_NOTICE
    assert with_no_japan_notice("sig").startswith("sig\n\n")


def test_incremental_prompt_byte_identical_when_flag_off() -> None:
    from src.synthesis.grounded.hypotheses import hypotheses_for_domain
    from src.synthesis.grounded.passes import _ATTRIBUTION_OPTIONS, _render

    prior = PriorJudgmentView(
        claim="c",
        claim_type="discrete_event",
        leading_hypothesis="organized_state_op",
        confidence="low",
        hypotheses=({"hypothesis": "organized_state_op", "consistent": 1, "inconsistent": 0},),
        indicators=("i1",),
        key_excerpts=({"polarity": "supports", "excerpt": "e"},),
    )
    expected = {
        "": "ee86ed169f93b0a588a43890e2730166f62a23bada3a8995284f75bbe12861f1",
        "Q?": "c33e7f35f0b7854067f84f1a2cef8bd38df17bbf0c49b19ab2764a0df4ceb0cc",
    }
    for question, digest in expected.items():
        prompt = _render(
            "synthesis/ground_incremental.j2",
            situation_title="T",
            prior=prior,
            sources=[{"feed_title": "f", "text": "t"}],
            attribution_options=_ATTRIBUTION_OPTIONS,
            hypotheses=hypotheses_for_domain("cyber_incident"),
            question=question,
            aggregate_signal="",
        )
        assert hashlib.sha256(prompt.encode()).hexdigest() == digest


def test_generic_katakana_and_kanji_terms_do_not_fire() -> None:
    text = "ダークウェブのリークとスパイ活動のコンポーネントが経由で流通する。"
    assert find_violations(text, _ctx(), check_terms=True) == []


def test_indicators_ignore_ungrounded_terms_but_not_foreign() -> None:
    ctx = _ctx(owners=_FOREIGN)
    assert find_violations("CVE-2030-0001 と Mirage の観測", ctx) == []
    assert [v.kind for v in find_violations("Zephyr の観測", ctx)] == ["foreign_subject"]


def test_ascii_generic_words_are_not_proper_nouns() -> None:
    assert find_violations("The API exposes RCE via HTTP.", _ctx(), check_terms=True) == []
    kinds = [v.token for v in find_violations("Mirage が使われた。", _ctx(), check_terms=True)]
    assert kinds == ["Mirage"]


def test_gate_failure_does_not_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    for mode in ("shadow", "1"):
        monkeypatch.setenv(IMPL_FLAG_ENV, mode)

        def boom(*a: object, **k: object) -> list[object]:
            raise RuntimeError("x")

        monkeypatch.setattr(rg, "find_violations", boom)
        a = _analysis(implication="何か。", indicators=("A",))
        assert apply_revision_gates(a, _ctx(), _prev()) is a


def test_gate_analysis_context_failure_returns_original(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(IMPL_FLAG_ENV, "shadow")

    class _BadRepo:
        def get_article_grounding(self, article_id: str) -> dict[str, object] | None:
            raise RuntimeError("db")

    class _Store:
        def evidence_excerpts(self, situation_id: str, *, limit: int = 5) -> list[dict[str, str]]:
            return []

    a = _analysis(implication="何か。")
    out = rg.gate_analysis(
        a,
        prev=None,
        repo=_BadRepo(),
        store=_Store(),
        situation_id="s-1",
        row=None,
        claim="c",
        read_aids=["x"],
        entities_by_id={},
        owners={},
        article_cache={},
    )
    assert out is a


def test_no_japan_link_false_unless_flag_one(monkeypatch: pytest.MonkeyPatch) -> None:
    kw = {"claim": "x", "anchors": [], "entity_keys": [], "evidence_text": ""}
    for mode in ("0", "shadow"):
        monkeypatch.setenv(JP_FLAG_ENV, mode)
        assert rg.no_japan_link(**kw) is False  # type: ignore[arg-type]
    monkeypatch.setenv(JP_FLAG_ENV, "1")
    assert rg.no_japan_link(**kw) is True  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_incremental_prompt_identical_for_flag_off_call() -> None:
    from unittest.mock import AsyncMock

    from src.synthesis.grounded.incremental import (
        _WireIncremental,
        incremental_ground_and_score,
    )

    prior = PriorJudgmentView("c", "discrete_event", "unverified_or_false", "low", (), ("i",), ())
    prompts: list[str] = []

    async def fake(prompt: str, *a: object, **k: object) -> _WireIncremental:
        prompts.append(prompt)
        return _WireIncremental()

    llm = AsyncMock()
    llm.generate_structured = fake
    kw = {
        "llm": llm,
        "situation_title": "T",
        "prior": prior,
        "domain": "cyber_incident",
        "sources": [{"feed_title": "f", "text": "t", "article_id": "a"}],
        "tier_by_id": {},
    }
    await incremental_ground_and_score(**kw, aggregate_signal="sig")  # type: ignore[arg-type]
    await incremental_ground_and_score(**kw, aggregate_signal="sig", no_japan_link=False)  # type: ignore[arg-type]
    await incremental_ground_and_score(**kw, aggregate_signal="sig", no_japan_link=True)  # type: ignore[arg-type]
    assert prompts[0] == prompts[1]
    assert prompts[2] != prompts[0] and NO_JAPAN_NOTICE in prompts[2]
