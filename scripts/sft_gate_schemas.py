"""SFT 品質関門の出力 schema 検証 (課題 -> 本番の pydantic スキーマの対応表)。

scripts/sft_quality_gate.py の検査 2 が使う。要約 = SummaryEvidenceOutput、軸 = SeverityAxes、
triage = TriageDecision、pir = JudgeVerdict、pair = PairVerdict、kind = KindVerdict。
ach / detect は pydantic の型が無い (手書きパーサ) ため構造を直接検証する。
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.pipeline.summary import SummaryEvidenceOutput  # noqa: E402

KINDS = ("advisory", "exploitation", "breach", "stats", "roundup", "other")
DETECT_DOMAINS = frozenset(
    {
        "political", "military", "economic", "social", "infrastructure",
        "information", "cyber_incident", "geopolitical", "tech",
    }
)  # fmt: skip
ACH_KEYS = frozenset(
    {
        "claim", "claim_type", "confidence", "evidence", "fired_indicators", "hypotheses",
        "implication", "indicators", "key_assumptions", "leading_hypothesis",
        "missing_evidence", "scope_expanded",
    }
)  # fmt: skip
ACH_CLAIM_TYPES = frozenset({"ongoing_activity", "discrete_event", "structural"})
ACH_CONFIDENCE = frozenset({"high", "moderate", "low"})


def _summary_contract() -> tuple[set[str], dict[str, int]]:
    schema = SummaryEvidenceOutput.model_json_schema()
    required = set(schema.get("required", ()))
    caps = {k: int(v["maxItems"]) for k, v in schema["properties"].items() if "maxItems" in v}
    return required, caps


def schema_errors_summary(j: Any) -> list[str]:
    if not isinstance(j, dict):
        return ["not_object"]
    required, caps = _summary_contract()
    errs = [f"extra_key:{k}" for k in j if k not in required]
    errs += [f"missing_key:{k}" for k in required if k not in j]
    errs += [
        f"over_max_items:{k}"
        for k, cap in caps.items()
        if isinstance(j.get(k), list) and len(j[k]) > cap
    ]
    try:
        SummaryEvidenceOutput.model_validate({k: v for k, v in j.items() if k in required})
    except Exception as exc:  # noqa: BLE001 - pydantic の ValidationError を理由として記録する
        errs.append(f"pydantic:{str(exc).splitlines()[0][:60]}")
    return errs


def schema_errors_axes(j: Any) -> list[str]:
    from src.cti.severity_axes import SeverityAxes

    if not isinstance(j, dict):
        return ["not_object"]
    fields = set(SeverityAxes.model_fields)
    errs = [f"extra_key:{k}" for k in j if k not in fields] + [
        f"missing_key:{k}" for k in fields if k not in j
    ]
    try:
        SeverityAxes.model_validate(j)
    except Exception as exc:  # noqa: BLE001
        errs.append(f"pydantic:{str(exc).splitlines()[0][:60]}")
    return errs


def _model_errors(model: Any, j: Any) -> list[str]:
    if not isinstance(j, dict):
        return ["not_object"]
    fields = set(model.model_fields)
    errs = [f"extra_key:{k}" for k in j if k not in fields]
    try:
        model.model_validate(j)
    except Exception as exc:  # noqa: BLE001
        errs.append(f"pydantic:{str(exc).splitlines()[0][:60]}")
    return errs


def schema_errors_triage(j: Any) -> list[str]:
    from src.tools.article_triage import TriageDecision

    errs = _model_errors(TriageDecision, j)
    return errs + (
        [f"missing_key:{k}" for k in ("importance", "reason") if k not in j]
        if isinstance(j, dict)
        else []
    )


def schema_errors_pir(j: Any) -> list[str]:
    from src.pir.llm_judge import JudgeVerdict

    errs = _model_errors(JudgeVerdict, j)
    return errs + (
        [f"missing_key:{k}" for k in ("matched", "reason") if k not in j]
        if isinstance(j, dict)
        else []
    )


def schema_errors_pair(j: Any) -> list[str]:
    from src.eventnews.pair_judge import PairVerdict

    return _model_errors(PairVerdict, j) if isinstance(j, dict) else ["not_object"]


def schema_errors_kind(j: Any) -> list[str]:
    from src.eventnews.event_kind import KindVerdict

    errs = _model_errors(KindVerdict, j)
    if isinstance(j, dict) and j.get("kind") not in KINDS:
        errs.append(f"kind_out_of_range:{j.get('kind')}")
    return errs


def schema_errors_ach(j: Any) -> list[str]:
    if not isinstance(j, dict):
        return ["not_object"]
    errs = [f"extra_key:{k}" for k in j if k not in ACH_KEYS] + [
        f"missing_key:{k}" for k in ACH_KEYS if k not in j
    ]
    if j.get("claim_type") not in ACH_CLAIM_TYPES:
        errs.append(f"claim_type_out_of_range:{j.get('claim_type')}")
    if j.get("confidence") not in ACH_CONFIDENCE:
        errs.append(f"confidence_out_of_range:{j.get('confidence')}")
    return errs


def schema_errors_detect(j: Any) -> list[str]:
    if not isinstance(j, dict):
        return ["not_object"]
    errs = [f"extra_key:{k}" for k in j if k not in ("open", "rejected")]
    for item in j.get("open") or []:
        if not isinstance(item, dict) or set(item) != {"claim", "domain", "article_ids"}:
            errs.append("open_item_keys")
        elif item["domain"] not in DETECT_DOMAINS:
            errs.append(f"domain_out_of_range:{item['domain']}")
    for item in j.get("rejected") or []:
        if not isinstance(item, dict) or set(item) != {"article_id", "reason"}:
            errs.append("rejected_item_keys")
    if "open" not in j or "rejected" not in j:
        errs.append("missing_open_or_rejected")
    return errs


def schema_errors_ioc_verify(j: Any) -> list[str]:
    from src.cti.ioc_llm_verifier import IocVerifyOutput

    errs = _model_errors(IocVerifyOutput, j)
    if isinstance(j, dict):
        errs += [f"extra_key:{k}" for k in j if k != "decisions"]
        ids = [d.get("id") for d in j.get("decisions") or [] if isinstance(d, dict)]
        if ids != list(range(len(ids))) or not ids:
            errs.append("decision_ids_not_0..N-1")
    return errs


def schema_errors_dedup_judge(j: Any) -> list[str]:
    from src.pipeline.dedup_judge import DuplicateVerdict

    errs = _model_errors(DuplicateVerdict, j)
    if isinstance(j, dict) and j.get("verdict") not in ("duplicate", "different", "unclear"):
        errs.append(f"verdict_out_of_range:{j.get('verdict')}")
    return errs


def schema_errors_ach_aux(j: Any) -> list[str]:
    """3 形のどれか: 採点 (evidence/hypotheses/…) / 反証 (reviews) / 書き直し (claim)。"""
    if not isinstance(j, dict):
        return ["not_object"]
    if set(j) == {"claim"}:
        return [] if isinstance(j["claim"], str) and j["claim"].strip() else ["empty_claim"]
    if set(j) == {"reviews"}:
        keys = {"judgment_id", "refutes_leading", "strongest_counter", "recommended_leading"}
        return [
            "review_keys"
            for r in j["reviews"]
            if not isinstance(r, dict)
            or set(r) != keys
            or not isinstance(r["refutes_leading"], bool)
        ]
    want = {
        "evidence", "hypotheses", "leading_hypothesis", "confidence", "key_assumptions",
        "missing_evidence", "indicators", "claim_type", "implication",
    }  # fmt: skip
    errs = [f"extra_key:{k}" for k in j if k not in want]
    errs += [f"missing_key:{k}" for k in want if k not in j]
    if j.get("claim_type") not in ACH_CLAIM_TYPES:
        errs.append(f"claim_type_out_of_range:{j.get('claim_type')}")
    if j.get("confidence") not in ACH_CONFIDENCE:
        errs.append(f"confidence_out_of_range:{j.get('confidence')}")
    return errs


SCHEMA_CHECKS: dict[str, Callable[[Any], list[str]]] = {
    "summary": schema_errors_summary,
    "axes": schema_errors_axes,
    "triage": schema_errors_triage,
    "pir": schema_errors_pir,
    "pair": schema_errors_pair,
    "kind": schema_errors_kind,
    "ach": schema_errors_ach,
    "detect": schema_errors_detect,
    "ioc_verify": schema_errors_ioc_verify,
    "ach_aux": schema_errors_ach_aux,
    "dedup_judge": schema_errors_dedup_judge,
}
