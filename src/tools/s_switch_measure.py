"""S 切替の関門の指標を計算する純粋関数 (S-SWITCH-RETRAIN)。

DB・ファイル・LLM に触れない。入力は配列と数値だけ。計算の出典:

* 深刻さ C: 分割外の確率 (``train_severity_s3_model`` の層化 5 分割 × 5 反復) から AUC と、
  固定の閾値 0.50 での適合・回収 (CLAUDE.md §7 10-28 の 0.916 / 0.69 / 0.83 と同じ測り方)。
* detect: 同じ fold の AUC 差の補正 t (Nadeau & Bengio 2003) と、差の事後 (Student t) で
  ``P(差 < -rope)`` = P(悪い)。``data/mlx/model_zoo.py`` の ``corrected_resampled_t`` /
  ``bayes_probs`` (baycomp の単一比較) と同じ式で、baycomp には依存しない。
* pair: cos の帯ごとの「同じ」判定率の現行とのずれ。
"""

# ruff: noqa: E501

from __future__ import annotations

import importlib
from collections.abc import Mapping, Sequence
from typing import Any, Final

import numpy as np

#: AUC 差が ±この値以内なら「実質同等」 (model_zoo.DEFAULT_ROPE と同じ)
DEFAULT_ROPE: Final = 0.01
CI_LEVEL: Final = 0.95
#: 深刻さ C の固定の閾値 (CLAUDE.md §7 10-28 の「閾値 0.50 据え置き」)
SEVC_FIXED_THRESHOLD: Final = 0.50
#: pair の cos の帯の端 (最後は 1.0 を含める)。帯ごとの same 率のずれを見る
COS_BAND_EDGES: Final[tuple[float, ...]] = (0.0, 0.6, 0.7, 0.8, 0.9, 1.0001)
#: 帯に入る対がこれ未満なら率が粗すぎるので比べない
MIN_BAND_PAIRS: Final = 10


def auc_score(y: Sequence[int] | np.ndarray, p: Sequence[float] | np.ndarray) -> float:
    """ROC AUC (順位和、同値は平均順位)。正例か負例が無ければ nan。"""
    yy = np.asarray(y, dtype=int)
    pp = np.asarray(p, dtype=float)
    n_pos = int((yy == 1).sum())
    n_neg = len(yy) - n_pos
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    order = np.argsort(pp, kind="mergesort")
    sorted_p = pp[order]
    ranks = np.empty(len(pp), dtype=float)
    i = 0
    while i < len(pp):
        j = i
        while j + 1 < len(pp) and sorted_p[j + 1] == sorted_p[i]:
            j += 1
        ranks[order[i : j + 1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return float((ranks[yy == 1].sum() - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg))


def precision_recall_at(
    y: Sequence[int] | np.ndarray, p: Sequence[float] | np.ndarray, threshold: float
) -> tuple[float, float]:
    """``p >= threshold`` を正と付けたときの (適合率, 回収率)。付けた数が 0 なら適合率 0。"""
    yy = np.asarray(y, dtype=int)
    sel = np.asarray(p, dtype=float) >= threshold
    tp = float((sel & (yy == 1)).sum())
    precision = tp / float(sel.sum()) if sel.any() else 0.0
    recall = tp / float((yy == 1).sum()) if (yy == 1).any() else 0.0
    return precision, recall


def compare_fold_aucs(
    a: Sequence[float],
    b: Sequence[float],
    *,
    test_train_ratio: float,
    rope: float = DEFAULT_ROPE,
) -> dict[str, float]:
    """同じ fold の AUC を ``b - a`` で比べる (``b`` が新)。補正 t の 95% 区間と P(悪い)。

    戻り値: ``mean_diff`` / ``ci_lower`` / ``ci_upper`` / ``p_worse`` / ``p_rope`` / ``p_better`` / ``n``。
    分散に (1/n + n_test/n_train) を掛ける補正を使う。差の事後を Student t (自由度 n-1) と見て
    P(悪い) = P(差 < -rope)、P(良い) = P(差 > rope)。
    """
    if len(a) != len(b):
        raise ValueError("fold の数が揃っていない")
    n = len(a)
    if n < 2:
        raise ValueError("差が 2 個以上要る")
    stats: Any = importlib.import_module("scipy.stats")  # 重い依存なので遅延 (型の stub も不要に)

    diffs = np.asarray(b, dtype=float) - np.asarray(a, dtype=float)
    mean = float(diffs.mean())
    var = float(np.var(diffs, ddof=1))
    se = float(np.sqrt((1.0 / n + test_train_ratio) * var))
    df = n - 1
    if se == 0.0:
        worse = 1.0 if mean < -rope else 0.0
        better = 1.0 if mean > rope else 0.0
        return {
            "mean_diff": mean,
            "ci_lower": mean,
            "ci_upper": mean,
            "p_worse": worse,
            "p_better": better,
            "p_rope": 1.0 - worse - better,
            "n": float(n),
        }
    half = float(stats.t.ppf(0.5 + CI_LEVEL / 2, df)) * se
    worse = float(stats.t.cdf((-rope - mean) / se, df))
    better = float(stats.t.sf((rope - mean) / se, df))
    return {
        "mean_diff": mean,
        "ci_lower": mean - half,
        "ci_upper": mean + half,
        "p_worse": worse,
        "p_better": better,
        "p_rope": max(0.0, 1.0 - worse - better),
        "n": float(n),
    }


def band_same_rates(
    cos: Sequence[float] | np.ndarray,
    predicted_same: Sequence[bool] | np.ndarray,
    *,
    edges: Sequence[float] = COS_BAND_EDGES,
) -> list[tuple[int, float | None]]:
    """cos の帯ごとの (対の数, 「同じ」と判定した率)。対が無い帯の率は None。"""
    c = np.asarray(cos, dtype=float)
    s = np.asarray(predicted_same, dtype=bool)
    out: list[tuple[int, float | None]] = []
    for lo, hi in zip(edges[:-1], edges[1:], strict=True):
        mask = (c >= lo) & (c < hi)
        n = int(mask.sum())
        out.append((n, float(s[mask].mean()) if n else None))
    return out


def same_rate_shifts(
    cos: Sequence[float] | np.ndarray,
    new_same: Sequence[bool] | np.ndarray,
    current_same: Sequence[bool] | np.ndarray,
    *,
    edges: Sequence[float] = COS_BAND_EDGES,
    min_pairs: int = MIN_BAND_PAIRS,
) -> list[float]:
    """帯ごとの |新の same 率 − 現行の same 率|。``min_pairs`` 未満の帯は比べない。"""
    new = band_same_rates(cos, new_same, edges=edges)
    cur = band_same_rates(cos, current_same, edges=edges)
    shifts: list[float] = []
    for (n, r_new), (_, r_cur) in zip(new, cur, strict=True):
        if n >= min_pairs and r_new is not None and r_cur is not None:
            shifts.append(abs(r_new - r_cur))
    return shifts


def merge_metrics(existing: Mapping[str, Any], computed: Mapping[str, Any]) -> dict[str, Any]:
    """測った値で上書きして重ねる。**手で足した値は、今回 None なら守る** (再計算で消さない)。

    辞書は再帰で重ねる。計算値が None で既存が None でないときだけ既存を残す。
    """
    merged: dict[str, Any] = dict(existing)
    for key, value in computed.items():
        old = merged.get(key)
        if isinstance(value, Mapping) and isinstance(old, Mapping):
            merged[key] = merge_metrics(old, value)
        elif value is None and old is not None:
            continue
        else:
            merged[key] = value
    return merged
