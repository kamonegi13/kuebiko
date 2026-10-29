"""logprob 判定のしきい値表 (モデル × step × 問い、s24_plan §4.2)。

``config/models/probe_thresholds.json`` = ``{model: {step: {question: threshold}}}``。
``scripts/calibrate_probes.py`` が書き、判定の呼び出し側が**解決済みモデルの項**を読む想定。
項が無ければ ``None`` (fail-closed: その判定は使わず従来の決定論の経路へ戻る)。
旧モデルの項は消さない (戻し先)。

本番の 3 判定 (薄い本文・PIR 要点の裏付け・組織名の同一性) は ``effective_threshold`` 経由で
この表を読む (旗 ``PROBE_THRESHOLDS_TABLE``、既定 1。0 で従来の定数に戻る)。
先頭が ``_`` のキー (``_calibration`` など) はしきい値として扱わない。
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from types import MappingProxyType
from typing import Any

from src.logging_config import get_logger
from src.tools.model_tiers import Step, resolve_step_model

_log = get_logger(__name__)

#: 旗。"0" で表を読まず、呼び出し側が渡す従来の定数を使う。
PROBE_THRESHOLDS_TABLE_ENV = "PROBE_THRESHOLDS_TABLE"

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
        if str(model).startswith("_") or not isinstance(steps, dict):
            continue
        for step, questions in steps.items():
            if str(step).startswith("_") or not isinstance(questions, dict):
                continue
            for question, value in questions.items():
                if (
                    str(question).startswith("_")
                    or isinstance(value, bool)
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


def table_enabled() -> bool:
    """表を本番の判定に使うか (``PROBE_THRESHOLDS_TABLE``、既定 on。"0" で off)。"""
    return os.environ.get(PROBE_THRESHOLDS_TABLE_ENV, "1").strip() != "0"


_WARNED_MISSING: set[tuple[str, str]] = set()


def _warn_missing_once(model: str, step: Step, question: str) -> None:
    """項なしの警告は (モデル, step) ごとに 1 プロセスで 1 回だけ (熱い経路のログ洪水を防ぐ)。"""
    key = (model.strip(), step.value)
    if key in _WARNED_MISSING:
        return
    _WARNED_MISSING.add(key)
    _log.warning("probe_threshold_missing", step=step.value, question=question, model=model)


def effective_threshold(
    step: Step,
    question: str = MAIN_QUESTION,
    *,
    fallback: float,
    model: str | None = None,
    db_path: Path | None = None,
    path: Path | None = None,
) -> float | None:
    """本番の判定が使うしきい値。旗 off → ``fallback`` (従来の定数)。

    旗 on → 判定に使う実モデル (``model`` 省略時は ``db_path`` で解決した step の実効モデル)
    の項。項が無いときだけ ``None`` (fail-closed: 呼び出し側はその判定を使わない)。
    モデルを解決できない (DB 障害等) ときは ``fallback`` (従来の定数) に退避する
    (関門を黙って止めない)。
    """
    if not table_enabled():
        return fallback
    try:
        resolved = model if model is not None else resolve_step_model(step, db_path=db_path)
    except Exception as e:  # noqa: BLE001 — 解決の失敗では従来の定数へ退避する
        _log.warning("probe_threshold_resolve_failed", step=step.value, error=str(e)[:200])
        return fallback
    value = probe_threshold(resolved, step, question, path=path)
    if value is None:
        _warn_missing_once(resolved, step, question)
    return value


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
