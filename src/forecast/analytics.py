"""Phase 4: forecasting の純粋な時系列分析関数 (I/O 非依存、全件 unit-test 可能)。

storage から取得した「entity → 出現時刻リスト」を週次バケットに落とし、
- FC3: 分散考慮 spike (直近バケットの z-score。既存の単純 ratio より誤検知に強い)
- FC4: トレンド方向 (線形回帰 slope)
- FC5: 2 系列の相関 (Pearson)
を算出する。個人運用規模なので numpy 全件計算で十分。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

import numpy as np

DAYS_PER_WEEK = 7
# spike 判定の既定 z-score 閾値と最低件数 (0→1 のノイズを spike と呼ばない)。
DEFAULT_SPIKE_Z = 2.0
DEFAULT_SPIKE_MIN_COUNT = 2
# trend 判定: 平均で正規化した slope がこの絶対値を超えたら increasing/decreasing。
TREND_SLOPE_REL_THRESHOLD = 0.15

TrendDirection = Literal["increasing", "stable", "decreasing"]


def bucket_weekly(
    times: list[datetime],
    *,
    weeks: int,
    now: datetime,
) -> list[int]:
    """出現時刻リストを直近 ``weeks`` 週の週次カウント (古い→新しい) に落とす。

    最後の要素が「直近 7 日」。窓外 (now より未来 / weeks 週より過去) は無視。
    """
    if weeks <= 0:
        return []
    counts = [0] * weeks
    for t in times:
        delta = now - t
        if delta < timedelta(0):
            continue  # 未来は無視
        weeks_ago = delta.days // DAYS_PER_WEEK
        if 0 <= weeks_ago < weeks:
            counts[weeks - 1 - weeks_ago] += 1
    return counts


def bucket_daily(
    times: list[datetime],
    *,
    days: int,
    now: datetime,
) -> list[int]:
    """出現時刻リストを直近 ``days`` 日の日次カウント (古い→新しい) に落とす。

    ``bucket_weekly`` の日次版 (I&W 日次バースト検出用)。最後の要素が「当日」。
    窓外 (now より未来 / days 日より過去) は無視。
    """
    if days <= 0:
        return []
    counts = [0] * days
    for t in times:
        delta = now - t
        if delta < timedelta(0):
            continue  # 未来は無視
        days_ago = delta.days
        if 0 <= days_ago < days:
            counts[days - 1 - days_ago] += 1
    return counts


def trend_direction(series: list[int]) -> tuple[TrendDirection, float]:
    """週次系列の傾向を線形回帰 slope で分類する (FC4)。

    戻り値: (direction, slope)。slope は「週あたりの増減件数」。
    平均活動量で正規化した slope の絶対値が閾値未満なら "stable"。
    """
    n = len(series)
    if n < 2:
        return "stable", 0.0
    x = np.arange(n, dtype=np.float64)
    y = np.asarray(series, dtype=np.float64)
    if float(y.sum()) == 0.0:
        return "stable", 0.0
    # slope (最小二乗): cov(x,y)/var(x)
    slope = float(np.polyfit(x, y, 1)[0])
    mean = float(y.mean())
    rel = slope / mean if mean > 0 else 0.0
    if rel >= TREND_SLOPE_REL_THRESHOLD:
        return "increasing", slope
    if rel <= -TREND_SLOPE_REL_THRESHOLD:
        return "decreasing", slope
    return "stable", slope


def spike_score(
    series: list[int],
    *,
    min_count: int = DEFAULT_SPIKE_MIN_COUNT,
    z_threshold: float = DEFAULT_SPIKE_Z,
) -> tuple[float, bool]:
    """直近バケットが baseline からどれだけ外れているかを z-score で返す (FC3)。

    分散考慮: baseline (最終週を除く全週) の平均・標準偏差を使う。単純な
    current/baseline 比率と違い、元々ばらつく系列の通常変動を spike と誤認しにくい。

    戻り値: (z_score, is_spike)。
    - baseline 件数 < 2: 標準偏差が出せないので「baseline ゼロ かつ 直近 >= min_count」のみ spike。
    - std == 0 (baseline が一定): 直近がそれを上回れば spike (z は差分で代用)。
    """
    if len(series) < 2:
        return 0.0, False
    baseline = np.asarray(series[:-1], dtype=np.float64)
    latest = float(series[-1])
    if latest < min_count:
        return 0.0, False

    mean = float(baseline.mean())
    std = float(baseline.std())  # population std (ddof=0)
    if std == 0.0:
        # baseline が一定値。直近がそれを上回れば spike (z は差分を代用値とする)。
        diff = latest - mean
        is_spike = diff > 0
        return (diff if is_spike else 0.0), is_spike
    z = (latest - mean) / std
    return z, z >= z_threshold


def pearson(a: list[float], b: list[float]) -> float:
    """2 系列の Pearson 相関係数 (FC5)。分散ゼロや長さ不一致は 0.0。"""
    if len(a) != len(b) or len(a) < 2:
        return 0.0
    av = np.asarray(a, dtype=np.float64)
    bv = np.asarray(b, dtype=np.float64)
    if float(av.std()) == 0.0 or float(bv.std()) == 0.0:
        return 0.0
    return float(np.corrcoef(av, bv)[0, 1])
