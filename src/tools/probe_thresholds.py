"""logprob 判定のしきい値表 (モデル × step × 問い、s24_plan §4.2)。

``config/models/probe_thresholds.json`` = ``{model: {step: {question: threshold}}}``。
``scripts/calibrate_probes.py`` が書き、判定の呼び出し側が**解決済みモデルの項**を読む想定。
項が無ければ ``None`` (fail-closed: その判定は使わず従来の決定論の経路へ戻る)。
旧モデルの項は消さない (戻し先)。

このモジュールは表の読み書きだけを持ち、既存の本番定数 (briefing / pir_daily_focus) は
まだ参照していない (配線は切替の手順 §4.2-6 で行う)。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from types import MappingProxyType
from typing import Any

from src.tools.model_tiers import Step

THRESHOLDS_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "models" / "probe_thresholds.json"
)
#: 単一の問いの判定が使う question 名
MAIN_QUESTION = "main"
#: org_identity は 2 問の AND
ORG_QUESTIONS: tuple[str, str] = ("event_logit", "org_logit")

#: しきい値を持つ step と、その問い名の集合 (これ以外の step は表に入れない)
PROBE_STEP_QUESTIONS: MappingProxyType[Step, tuple[str, ...]] = MappingProxyType(
    {
        Step.THIN_BODY_JUDGE: (MAIN_QUESTION,),
        Step.PIR_FOCUS_CLAIM_JUDGE: (MAIN_QUESTION,),
        Step.ASSIGN_EVIDENCE_JUDGE: (MAIN_QUESTION,),
        Step.ORG_IDENTITY_JUDGE: ORG_QUESTIONS,
    }
)

#: 切替前の本番定数 (戻し先の項。briefing.THIN_BODY_PROBE_THRESHOLD ほかと一致させる)。
#: assign の 0.5 は F1 の「既定の境界」、org は org_identity.EVENT_MIN_LOGIT / ORG_MIN_LOGIT。
CURRENT_PRODUCTION_CONSTANTS: MappingProxyType[Step, MappingProxyType[str, float]] = (
    MappingProxyType(
        {
            Step.THIN_BODY_JUDGE: MappingProxyType({MAIN_QUESTION: 0.075}),
            Step.PIR_FOCUS_CLAIM_JUDGE: MappingProxyType({MAIN_QUESTION: 0.22}),
            Step.ASSIGN_EVIDENCE_JUDGE: MappingProxyType({MAIN_QUESTION: 0.5}),
            Step.ORG_IDENTITY_JUDGE: MappingProxyType({"event_logit": 12.0, "org_logit": -4.0}),
        }
    )
)

Thresholds = dict[str, dict[str, dict[str, float]]]


def load_thresholds(path: Path | None = None) -> Thresholds:
    """表を読む。ファイルが無い・壊れている場合は空 (fail-closed)。数値以外の値は捨てる。"""
    target = path or THRESHOLDS_PATH
    try:
        raw: Any = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: Thresholds = {}
    for model, steps in raw.items():
        if not isinstance(steps, dict):
            continue
        for step, questions in steps.items():
            if not isinstance(questions, dict):
                continue
            for question, value in questions.items():
                if (
                    isinstance(value, bool)
                    or not isinstance(value, int | float)
                    or not math.isfinite(value)
                ):
                    continue
                out.setdefault(str(model), {}).setdefault(str(step), {})[str(question)] = float(
                    value
                )
    return out


def probe_threshold(
    model: str, step: Step, question: str = MAIN_QUESTION, *, path: Path | None = None
) -> float | None:
    """解決済みモデルの項。無ければ ``None`` (呼び出し側は判定を使わない)。"""
    return load_thresholds(path).get(model.strip(), {}).get(step.value, {}).get(question)


def missing_entries(model: str, thresholds: Thresholds) -> list[str]:
    """このモデルに足りない ``step/question`` (4 本すべての問いが揃っているかの検査用)。"""
    entries = thresholds.get(model.strip(), {})
    return [
        f"{step.value}/{q}"
        for step, questions in PROBE_STEP_QUESTIONS.items()
        for q in questions
        if q not in entries.get(step.value, {})
    ]


def merged(base: Thresholds, model: str, step: Step, values: dict[str, float]) -> Thresholds:
    """``base`` に 1 モデル × 1 step の項を足した新しい表 (他のモデル・step の項は触らない)。"""
    out: Thresholds = {m: {s: dict(q) for s, q in steps.items()} for m, steps in base.items()}
    out.setdefault(model, {})[step.value] = dict(values)
    return out


def save_thresholds(thresholds: Thresholds, path: Path | None = None) -> None:
    """表を書く (キー順固定・原子的に置換)。"""
    target = path or THRESHOLDS_PATH
    ordered = {
        m: {s: dict(sorted(q.items())) for s, q in sorted(st.items())}
        for m, st in sorted(thresholds.items())
    }
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(ordered, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(target)
