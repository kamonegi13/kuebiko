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
    assert find_violations("ShadowCrew は 4,708 件を窃取した。", _ctx(), check_numbers=True) == []


def test_ungrounded_cve_and_number_are_violations() -> None:
    kinds = {
        (v.kind, v.token)
        for v in find_violations(
            "CVE-2026-9999 で 12,345 件が影響を受けた。", _ctx(), check_numbers=True
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
    out = apply_revision_gates(a, _ctx(), _prev())
    assert out.implication == "ShadowCrew は窃取した。"
    assert out.indicators == ("監視する Ａ",)
    assert out.missing_evidence == ("欠けている証拠",)


def test_on_keeps_previous_when_everything_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(IMPL_FLAG_ENV, "1")
    a = _analysis(implication="CVE-2026-9999 が悪用された。", indicators=("Zephyr の観測",))
    out = apply_revision_gates(a, _ctx(), _prev())
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
