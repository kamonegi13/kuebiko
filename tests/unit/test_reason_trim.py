"""reason 欄は長さで検証を落とさず、判定を保ったまま収める。"""

import json

from src.cti.ioc_llm_verifier import IocDecision
from src.cti.relevance_ml import JapanInvolvementJudgement
from src.pir.llm_judge import JudgeVerdict
from src.tools.reason_trim import TRIAGE_REASON_MAX, trim_reason

LONG = "根拠の文。" * 100


def test_short_reason_unchanged() -> None:
    assert trim_reason("短い。") == "短い。"


def test_trims_at_sentence_boundary() -> None:
    out = trim_reason(LONG)
    assert len(out) <= TRIAGE_REASON_MAX
    assert out.endswith("。")


def test_trims_with_ellipsis_without_delimiter() -> None:
    out = trim_reason("あ" * 1000)
    assert len(out) == TRIAGE_REASON_MAX
    assert out.endswith("…")


def test_judge_verdict_keeps_matched() -> None:
    v = JudgeVerdict.model_validate({"matched": True, "reason": LONG})
    assert v.matched is True
    assert len(v.reason) <= TRIAGE_REASON_MAX


def test_ioc_decision_keeps_action() -> None:
    d = IocDecision.model_validate({"id": 1, "action": "keep", "reason": LONG})
    assert d.action == "keep"
    assert len(d.reason) <= TRIAGE_REASON_MAX


def test_japan_cascade_keeps_flag() -> None:
    j = JapanInvolvementJudgement.model_validate({"japan_involved": True, "reason": LONG})
    assert j.japan_involved is True


def test_schemas_carry_no_max_length() -> None:
    for m in (JudgeVerdict, IocDecision, JapanInvolvementJudgement):
        assert "maxLength" not in json.dumps(m.model_json_schema())


def test_decision_field_precedes_reason_in_schema() -> None:
    from src.tools.article_triage import TriageDecision

    for m, key in (
        (TriageDecision, "importance"),
        (JudgeVerdict, "matched"),
        (JapanInvolvementJudgement, "japan_involved"),
        (IocDecision, "action"),
    ):
        props = list(m.model_json_schema()["properties"])
        assert props.index(key) < props.index("reason"), m.__name__


def test_truncated_json_still_salvages_decision() -> None:
    from src.tools.article_triage import TriageDecision
    from src.tools.llm_client import _repair_truncated_json

    cut = '{"importance": "high", "reason": "途中で切れた理由'
    salvaged = _repair_truncated_json(cut)
    assert salvaged is not None
    assert TriageDecision.model_validate(salvaged).importance == "high"


def test_summary_title_and_claim_wire_trim_without_failing() -> None:
    from src.assessment.detect_decompose import ClusterClaimWire
    from src.pipeline.summary import TITLE_JA_MAX, SummaryOutput

    fields = SummaryOutput.model_fields
    assert "title_ja" in fields
    w = ClusterClaimWire.model_validate({"claim": "あ。" * 1000, "skip_reason": "x" * 500})
    assert 0 < len(w.claim) <= 600
    assert len(w.skip_reason) <= 120
    assert TITLE_JA_MAX == 120
