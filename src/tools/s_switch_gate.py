"""S 切替時の ML 一括学び直しの「合否の関門」と学習スクリプト出力の解析 (S-SWITCH-RETRAIN)。

純粋関数のみ (DB・LLM・ファイル非依存)。基準値の出典は ``docs/s_switch_retrain.md`` §4 と
``data/eval/s_switch_retrain_procedure.md`` §4 (D1 の手順書・CLAUDE.md §7 の 10-28 の行)。
**1 本でも不合格・未測定なら 5 本とも出さない** (組で切替・組で戻す)。
未測定は不合格と同じ扱い (fail-closed): 測っていない指標で通さない。
"""

# ruff: noqa: E501

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

PASS: Final = "pass"
FAIL: Final = "fail"
UNMEASURED: Final = "unmeasured"

#: detect (D1 §4): 補正 t の 95% 区間の下限 > この値 / baycomp の P(悪い) < 上限
DETECT_AUC_LOWER_FLOOR: Final = -0.01
DETECT_P_WORSE_MAX: Final = 0.2
#: 深刻さ C の現行 (CLAUDE.md §7 10-28: 分割外 AUC 0.916・適合 0.69・回収 0.83)
SEVC_BASELINE: Final[Mapping[str, float]] = {"auc": 0.916, "precision": 0.69, "recall": 0.83}
#: ⚠ 暫定値の注記 (pair のずれ 0.10・relation の「回収が現行以上」): D1 に相当する既存の閾値が無く
#: 置いた仮の値。**最初の切替 (SW-S26) で実測してから確定する** (確定までは判定の参考値)。
#: relation の適合率の下限 (train_relation_model.py の --min-precision 既定)
RELATION_MIN_PRECISION: Final = 0.8
#: pair の 'same' 率の許容ずれ (cos の帯ごと、絶対値。10-29 版 45 の教訓: 半減は不可)
PAIR_SAME_RATE_MAX_SHIFT: Final = 0.10

MODELS: Final[tuple[str, ...]] = ("detect", "severity_s3", "deep_dive", "relation", "pair")


@dataclass(frozen=True)
class GateResult:
    model: str
    status: str  # PASS / FAIL / UNMEASURED
    detail: str
    #: 合否には効かない注意 (交絡など)。空でなければ表示する
    warning: str = ""


def _num(m: Mapping[str, Any], key: str) -> float | None:
    v = m.get(key)
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


#: 手でしか測れない指標の取り方 (未測定の表示に添える)
MANUAL_HINTS: Final[Mapping[str, str]] = {
    "new_only_opens": "D1 の digest (data/mlx/d1_detect_compare.py digest) を人が審判して数える",
    "current_only_opens": "同上 (現行だけが開設した記事の審判済みの件数)",
    "control_only_opens": "同上 (対照だけが開設した記事の審判済みの件数)",
}

#: detect のメトリクスの 3 腕の形 (新 vs 対照が合否、対照 vs 現行は参考)
DETECT_NEW_VS_CONTROL: Final = "new_vs_control"
DETECT_CONTROL_VS_CURRENT: Final = "control_vs_current"
CONFOUNDED_WARNING: Final = (
    "対照腕なし: 新 vs 現行の差に「S の変更」と「種別の欠け (goldset 840 件が other) の解消」が"
    "混ざる。relabel を現行の S で回し train --control-s で対照を作って 3 腕で比べる"
)


def _missing(model: str, keys: list[str]) -> GateResult:
    hints = [f"{k} = {MANUAL_HINTS[k]}" for k in keys if k in MANUAL_HINTS]
    detail = "未測定: " + ", ".join(keys)
    return GateResult(model, UNMEASURED, detail + (" (" + "; ".join(hints) + ")" if hints else ""))


def _gate_detect_pair(
    m: Mapping[str, Any], *, other_only_key: str, other_label: str, warning: str = ""
) -> GateResult:
    """新 vs 比べる相手 (現行 or 対照) の合否。``m`` は 4 指標の辞書。"""
    lower, p_worse = _num(m, "auc_diff_ci_lower"), _num(m, "p_worse")
    new_only, other_only = _num(m, "new_only_opens"), _num(m, other_only_key)
    absent = [
        k
        for k, v in (
            ("auc_diff_ci_lower", lower),
            ("p_worse", p_worse),
            ("new_only_opens", new_only),
            (other_only_key, other_only),
        )
        if v is None
    ]
    if absent or lower is None or p_worse is None or new_only is None or other_only is None:
        res = _missing("detect", absent)
        return GateResult(res.model, res.status, res.detail, warning)
    reasons = []
    if lower <= DETECT_AUC_LOWER_FLOOR:
        reasons.append(f"AUC 差の下限 {lower:+.3f} <= {DETECT_AUC_LOWER_FLOOR}")
    if p_worse >= DETECT_P_WORSE_MAX:
        reasons.append(f"P(悪い) {p_worse:.2f} >= {DETECT_P_WORSE_MAX}")
    if new_only < other_only:
        reasons.append(f"新だけの開設 {new_only:.0f} < {other_label}だけ {other_only:.0f}")
    detail = "; ".join(reasons) or (
        f"AUC 差下限 {lower:+.3f}・P(悪い) {p_worse:.2f}"
        f"・開設 {new_only:.0f}>={other_only:.0f} (新 vs {other_label})"
    )
    return GateResult("detect", FAIL if reasons else PASS, detail, warning)


def _control_info(m: Mapping[str, Any]) -> str:
    """対照 vs 現行 (種別の欠けを埋めた効果)。合否に使わず表示だけ。"""
    lower, p_worse, mean = (
        _num(m, "auc_diff_ci_lower"),
        _num(m, "p_worse"),
        _num(m, "mean_diff"),
    )
    if lower is None or p_worse is None:
        return "参考 (対照 vs 現行 = 欠けを埋めた効果): 未測定"
    avg = f"平均差 {mean:+.3f}・" if mean is not None else ""
    return f"参考 (対照 vs 現行 = 欠けを埋めた効果、合否に使わない): {avg}下限 {lower:+.3f}・P(悪い) {p_worse:.2f}"


def gate_detect(m: Mapping[str, Any]) -> GateResult:
    """3 腕 (``new_vs_control`` あり) は新 vs 対照で合否、対照 vs 現行は参考。

    対照なしの従来の形 (4 指標が平ら) は新 vs 現行で判定するが、交絡の警告を付ける。
    """
    arm = m.get(DETECT_NEW_VS_CONTROL)
    if not isinstance(arm, Mapping):
        return _gate_detect_pair(
            m, other_only_key="current_only_opens", other_label="現行", warning=CONFOUNDED_WARNING
        )
    res = _gate_detect_pair(arm, other_only_key="control_only_opens", other_label="対照")
    info = m.get(DETECT_CONTROL_VS_CURRENT)
    return GateResult(
        res.model,
        res.status,
        res.detail,
        _control_info(info if isinstance(info, Mapping) else {}),
    )


def gate_severity_s3(
    m: Mapping[str, Any], baseline: Mapping[str, float] = SEVC_BASELINE
) -> GateResult:
    keys = ("auc", "precision", "recall")
    vals = {k: _num(m, k) for k in keys}
    absent = [k for k, v in vals.items() if v is None]
    if absent:
        return _missing("severity_s3", absent)
    bad = [f"{k} {vals[k]:.3f} < {baseline[k]:.3f}" for k in keys if (vals[k] or 0.0) < baseline[k]]
    detail = "; ".join(bad) or "AUC・適合・回収とも現行以上"
    return GateResult("severity_s3", FAIL if bad else PASS, detail)


def gate_deep_dive(m: Mapping[str, Any], current_corr: float | None) -> GateResult:
    corr = _num(m, "holdout_corr")
    if corr is None or current_corr is None:
        return _missing("deep_dive", ["holdout_corr", "現行の holdout_corr"])
    ok = corr >= current_corr
    return GateResult(
        "deep_dive", PASS if ok else FAIL, f"相関 {corr:.3f} (現行 {current_corr:.3f})"
    )


def gate_relation(m: Mapping[str, Any]) -> GateResult:
    prec, rec, cur = _num(m, "precision"), _num(m, "recall"), _num(m, "current_recall")
    absent = [
        k for k, v in (("precision", prec), ("recall", rec), ("current_recall", cur)) if v is None
    ]
    if absent or prec is None or rec is None or cur is None:
        return _missing("relation", absent)
    bad = []
    if prec < RELATION_MIN_PRECISION:
        bad.append(f"適合率 {prec:.3f} < {RELATION_MIN_PRECISION}")
    if rec < cur:
        bad.append(f"回収 {rec:.3f} < 現行 {cur:.3f}")
    return GateResult(
        "relation", FAIL if bad else PASS, "; ".join(bad) or f"適合 {prec:.3f}・回収 {rec:.3f}"
    )


def gate_pair(m: Mapping[str, Any]) -> GateResult:
    shifts, prec, cur = (
        m.get("same_rate_shift_by_cos_band"),
        _num(m, "precision"),
        _num(m, "current_precision"),
    )
    if not isinstance(shifts, list) or not shifts or prec is None or cur is None:
        return _missing("pair", ["same_rate_shift_by_cos_band", "precision", "current_precision"])
    worst = max(abs(float(s)) for s in shifts)
    bad = []
    if worst > PAIR_SAME_RATE_MAX_SHIFT:
        bad.append(f"same 率のずれ最大 {worst:.2f} > {PAIR_SAME_RATE_MAX_SHIFT}")
    if prec < cur:
        bad.append(f"適合 {prec:.3f} < 現行 {cur:.3f}")
    return GateResult(
        "pair", FAIL if bad else PASS, "; ".join(bad) or f"ずれ最大 {worst:.2f}・適合 {prec:.3f}"
    )


def evaluate_gates(
    metrics: Mapping[str, Mapping[str, Any]], *, current_deep_dive_corr: float | None
) -> tuple[bool, tuple[GateResult, ...]]:
    """5 本の関門。全部 PASS のときだけ ``all_pass``。"""
    empty: Mapping[str, Any] = {}
    results = (
        gate_detect(metrics.get("detect", empty)),
        gate_severity_s3(metrics.get("severity_s3", empty)),
        gate_deep_dive(metrics.get("deep_dive", empty), current_deep_dive_corr),
        gate_relation(metrics.get("relation", empty)),
        gate_pair(metrics.get("pair", empty)),
    )
    return all(r.status == PASS for r in results), results


# --- 学習スクリプトの標準出力の解析 (各 train_*.py の print と同じ形) ---

_SEV_RE = re.compile(r"回収\s*(\d+(?:\.\d+)?)%\s*適合率\s*(\d+(?:\.\d+)?)%")
_REL_RE = re.compile(r"精度\s*(\d+(?:\.\d+)?)%\s*回収\s*(\d+(?:\.\d+)?)%")
_DD_RE = re.compile(r"検証:\s*相関\s*(-?\d+(?:\.\d+)?)")


def parse_severity_stdout(text: str) -> dict[str, float]:
    m = _SEV_RE.search(text)
    return {"recall": float(m[1]) / 100, "precision": float(m[2]) / 100} if m else {}


def parse_relation_stdout(text: str) -> dict[str, float]:
    m = _REL_RE.search(text)
    return {"precision": float(m[1]) / 100, "recall": float(m[2]) / 100} if m else {}


def parse_deep_dive_stdout(text: str) -> dict[str, float]:
    m = _DD_RE.search(text)
    return {"holdout_corr": float(m[1])} if m else {}
