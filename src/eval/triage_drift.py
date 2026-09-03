"""triage のドリフト週次検知 — 凍結 goldset を現在の 26B に通し day-0 と比べる。

⭐ day-0 (2026-09-03) の実測: 26B vs Sonnet 55.3% (不一致は全て 26B が高めに
付ける方向・見逃し方向 0 件で健全と判定)。26B vs 過去の保存値は 40% —
**保存済みラベルは現在の体制を代表しない**。だからこの goldset は凍結し、
基準は day-0 の 26B 自身の答え (now_26b) に置く。基準からの移動 = ドリフト。

判定ロジックの SSoT はここ。scripts/triage_drift_check.py は薄い CLI ラッパ。
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from src.config_loader import load_app_config
from src.logging_config import get_logger
from src.tools.article_triage import TriageDecision
from src.tools.model_tiers import Step, build_llm_for

_log = get_logger(__name__)

GOLDSET_PATH = Path("data/eval/triage_goldset.json")
#: 警告する移動率。day-0 の自己一致は定義上 100% なので、通常週は数 % に収まる想定。
DRIFT_WARN_RATE = 0.15


@dataclass(frozen=True)
class TriageDriftResult:
    total: int
    moved: int
    demoted: int  # high/medium → low (見逃し方向への移動)
    transitions: tuple[tuple[str, str, int], ...]

    @property
    def rate(self) -> float:
        return self.moved / self.total if self.total else 0.0


async def measure_drift(goldset_path: Path = GOLDSET_PATH) -> TriageDriftResult | None:
    """凍結プロンプトを再実行して day-0 からの移動を数える。goldset が無ければ None。"""
    if not goldset_path.exists():
        _log.warning("triage_goldset_missing", path=str(goldset_path))
        return None
    rows = json.loads(goldset_path.read_text(encoding="utf-8"))["rows"]
    base = [r for r in rows if r.get("now_26b")]
    llm = build_llm_for(Step.TRIAGE, load_app_config())
    moved = demoted = judged = 0
    conf: Counter[tuple[str, str]] = Counter()
    for r in base:
        try:
            v = await llm.generate_structured(
                prompt=r["prompt"], schema=TriageDecision, temperature=0.0, think=False
            )
        except Exception as e:  # noqa: BLE001 — 1 件の失敗で週次を落とさない
            _log.warning("triage_drift_judge_failed", error=str(e)[:120])
            continue
        judged += 1
        if v.importance != r["now_26b"]:
            moved += 1
            conf[(r["now_26b"], v.importance)] += 1
            if r["now_26b"] in ("high", "medium") and v.importance == "low":
                demoted += 1
    return TriageDriftResult(
        total=judged,
        moved=moved,
        demoted=demoted,
        transitions=tuple((a, b, n) for (a, b), n in conf.most_common()),
    )


async def run_weekly_triage_drift() -> None:
    """週次: ドリフトを測って ops へ報告する (閾値超えは警告つき)。"""
    result = await measure_drift()
    if result is None:
        return
    trans = " / ".join(f"{a}→{b} {n}" for a, b, n in result.transitions[:5]) or "移動なし"
    _log.info(
        "triage_drift_measured",
        total=result.total,
        moved=result.moved,
        rate=round(result.rate, 3),
        demoted=result.demoted,
    )
    warn = result.rate >= DRIFT_WARN_RATE
    title = "triage ドリフト警告" if warn else "triage ドリフト週次"
    body = (
        f"凍結 goldset {result.total} 件のうち day-0 から移動 {result.moved} 件"
        f" ({result.rate:.0%})。内訳: {trans}。見逃し方向への降格 {result.demoted} 件。"
        + ("\n閾値超え — プロンプト/PIR/モデルの直近変更を確認してください。" if warn else "")
    )
    try:
        from src.ui.services.ops_notify import post_ops_message

        await post_ops_message(title=title, body=body, importance="high" if warn else "low")
    except Exception as e:  # noqa: BLE001 — 通知失敗で週次を落とさない
        _log.warning("triage_drift_notify_failed", error=str(e)[:120])
