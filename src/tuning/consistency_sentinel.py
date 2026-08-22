"""一貫性番兵 — intent 判定の不安定率を週次で測る (§10.2f、2026-08-22)。

パネル (C2) 退役の後継。正解ラベルが存在しない intent に対する唯一の機械的な
誤り信号 = 「同一事象を扱う複数記事の間で判定が割れているか」を、embedding
クラスタで決定論的に測る (LLM 呼出ゼロ)。単一ホストのテンプレ連報クラスタで
判定が割れる事例は、ソース間の正当な視点差では説明できない純粋な分類器
不安定性の証拠 (オフライン実測 §10.2f)。

**用途は番兵 (検出器) 専用** — 不安定率の推移を rubric 変更のドリフト監視に使う。
⚠ 一貫性を目標関数・合格条件にしてはならない (揃える方向へ最適化すると無難な
出力へ収束する — 壁 3 合意 Goodhart と同型の罠)。

閾値 t=0.85 はオフライン実測のスイープ (0.80/0.85/0.90) の中間水準。窓 14 日は
「同一事象の続報」の実用的な範囲 (dedup cluster 窓 48h より広く、storyline の
副事象混入 (t=0.80 で観測) を閾値側で抑える)。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import numpy as np
import structlog
from numpy.typing import NDArray

from src.cti.ioc_source_filter import normalize_host

if TYPE_CHECKING:
    from src.storage.run_history import RunHistoryRepository

_log = structlog.get_logger(__name__)

# 集計窓は 90 日 (2026-08-22 独立レビュー: 14 日窓では測定可能クラスタが ~14 個しか
# できず、二項 CI が 5〜51% に開いて番兵として機能しない。10pt のシフト検出には
# 100 クラスタ級 ≈ 90 日分が要る)。同一事象のペア判定は 14 日 (§10.2f と同じ定義) —
# 窓を広げるのは「蓄積期間」であって「事象の定義」ではない。
WINDOW_DAYS = 90
PAIR_WINDOW_DAYS = 14
SIM_THRESHOLD = 0.85
# クラスタ統計が意味を持つ最小標本 (下回ったら「標本不足」— 0 件と測れないを区別)
MIN_ROWS = 50
# 数字を出してよい最小クラスタ数 (CI 幅が広すぎる数字は錯覚を生むだけ)
MIN_CLUSTERS = 30
MIN_SINGLE_HOST_CLUSTERS = 10
# 週次ガバナンス内で走るため計算量の安全弁 (新しい方を優先)
MAX_ROWS = 8000
_DEFAULT_EMBED_MODEL = "snowflake-arctic-embed2"
_WILSON_Z = 1.96


@dataclass(frozen=True)
class ConsistencyStats:
    """直近窓の intent 不安定率。single_host 層が分類器不安定性の本命信号。"""

    articles: int
    measured_clusters: int  # サイズ>=2 かつ intent 2 件以上のクラスタ
    split_clusters: int
    single_host_measured: int
    single_host_split: int

    @property
    def rate(self) -> float:
        return self.split_clusters / self.measured_clusters if self.measured_clusters else 0.0

    @property
    def single_host_rate(self) -> float:
        if not self.single_host_measured:
            return 0.0
        return self.single_host_split / self.single_host_measured


def cluster_indices(
    matrix: NDArray[np.float32],
    threshold: float,
    *,
    epochs: NDArray[np.float64] | None = None,
    pair_window_seconds: float | None = None,
) -> list[list[int]]:
    """コサイン類似度 >= threshold の連結成分 (union-find)。行は L2 正規化して比較する。

    ``epochs`` (行と同順・昇順の UNIX 秒) と ``pair_window_seconds`` を渡すと、
    時刻差が窓を超えるペアは辺にしない (§10.2f と同じ「同一事象」定義 —
    蓄積期間を 90 日に広げても、事象のペア判定は 14 日のまま保つ)。
    """
    n = matrix.shape[0]
    if n == 0:
        return []
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    unit = matrix / norms
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    block = 512
    for s in range(0, n, block):
        sims = unit[s : s + block] @ unit.T
        rows, cols = np.nonzero(sims >= threshold)
        for r, c in zip(rows.tolist(), cols.tolist(), strict=True):
            i, j = s + r, c
            if i >= j:
                continue
            if (
                epochs is not None
                and pair_window_seconds is not None
                and abs(float(epochs[j]) - float(epochs[i])) > pair_window_seconds
            ):
                continue
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[ri] = rj
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return [g for g in groups.values() if len(g) >= 2]


def wilson_interval(successes: int, total: int, *, z: float = _WILSON_Z) -> tuple[float, float]:
    """二項比率の Wilson 区間。点推定だけ出すと少標本で錯覚を生む (§10.2e の教訓)。"""
    if total <= 0:
        return (0.0, 0.0)
    p = successes / total
    denom = 1.0 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    margin = (z * ((p * (1 - p) / total + z * z / (4 * total * total)) ** 0.5)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def compute_from_rows(
    rows: list[dict[str, Any]],
    *,
    threshold: float,
    pair_window_days: int | None = None,
) -> ConsistencyStats:
    """(intent, url, vector[, created_at]) の行群から不安定率を計算する純関数部。

    rows は created_at 昇順であること (repo がそう返す)。pair_window_days を渡すと
    時刻差がそれを超えるペアは同一事象と見なさない。
    """
    vectors: list[NDArray[np.float32]] = []
    intents: list[str] = []
    hosts: list[str] = []
    epoch_list: list[float] = []
    for r in rows:
        vec = np.frombuffer(r["vector"], dtype="<f4")
        if int(r.get("dim") or 0) and vec.shape[0] != int(r["dim"]):
            continue
        vectors.append(vec)
        intents.append(str(r["intent"]))
        hosts.append(normalize_host(str(r.get("url") or "")))
        epoch_list.append(_to_epoch(r.get("created_at")))
    if not vectors:
        return ConsistencyStats(0, 0, 0, 0, 0)
    matrix = np.vstack(vectors).astype(np.float32)
    epochs: NDArray[np.float64] | None = None
    window_seconds: float | None = None
    if pair_window_days is not None and any(e > 0 for e in epoch_list):
        epochs = np.asarray(epoch_list, dtype=np.float64)
        window_seconds = pair_window_days * 86400.0

    measured = split = sh_measured = sh_split = 0
    for group in cluster_indices(
        matrix, threshold, epochs=epochs, pair_window_seconds=window_seconds
    ):
        group_intents = {intents[i] for i in group}
        if len(group) < 2:
            continue
        measured += 1
        is_split = len(group_intents) > 1
        split += int(is_split)
        if len({hosts[i] for i in group if hosts[i]}) == 1:
            sh_measured += 1
            sh_split += int(is_split)
    return ConsistencyStats(
        articles=len(vectors),
        measured_clusters=measured,
        split_clusters=split,
        single_host_measured=sh_measured,
        single_host_split=sh_split,
    )


def compute_intent_instability(
    repo: RunHistoryRepository,
    *,
    now: datetime | None = None,
    window_days: int = WINDOW_DAYS,
    threshold: float = SIM_THRESHOLD,
) -> ConsistencyStats | None:
    """直近窓の intent 不安定率。標本不足 (< MIN_ROWS) なら None (測れないを明示)。"""
    now = now or datetime.now(UTC)
    since_iso = (now - timedelta(days=window_days)).isoformat()
    embed_model = os.environ.get("OLLAMA_EMBED_MODEL", "").strip() or _DEFAULT_EMBED_MODEL
    rows = repo.fetch_intent_embedding_window(since_iso, embed_model=embed_model)
    if len(rows) < MIN_ROWS:
        return None
    if len(rows) > MAX_ROWS:
        rows = rows[-MAX_ROWS:]
    return compute_from_rows(rows, threshold=threshold, pair_window_days=PAIR_WINDOW_DAYS)


def _to_epoch(value: Any) -> float:
    """created_at (ISO 文字列 or datetime) → UNIX 秒。解釈不能は 0 (時間窓を課さない)。"""
    if value is None:
        return 0.0
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def sentinel_line(stats: ConsistencyStats | None) -> str:
    """週次ガバナンス ops 本文の 1 行。番兵であり合否ではない (目標関数化の禁止)。

    少標本の点推定は毎週の錯覚を生むだけなので、クラスタ数が閾値未満なら数字を
    出さない (0 件と「測れない」を区別する — 本プロジェクトの作法)。CI は Wilson。
    """
    if stats is None:
        return "🧭 intent 不安定率: 標本不足で今週は測定なし"
    if stats.measured_clusters < MIN_CLUSTERS:
        return (
            f"🧭 intent 不安定率: クラスタ {stats.measured_clusters} 件 < {MIN_CLUSTERS}"
            " — 標本不足のため数字は出さない (蓄積待ち)"
        )
    lo, hi = wilson_interval(stats.split_clusters, stats.measured_clusters)
    line = (
        f"🧭 intent 不安定率 ({WINDOW_DAYS}日, t={SIM_THRESHOLD}): "
        f"{stats.rate * 100:.0f}% (CI {lo * 100:.0f}-{hi * 100:.0f}%,"
        f" {stats.split_clusters}/{stats.measured_clusters} クラスタ)"
    )
    if stats.single_host_measured >= MIN_SINGLE_HOST_CLUSTERS:
        slo, shi = wilson_interval(stats.single_host_split, stats.single_host_measured)
        line += (
            f" / 単一ホスト連報 {stats.single_host_rate * 100:.0f}%"
            f" (CI {slo * 100:.0f}-{shi * 100:.0f}%, {stats.single_host_split}/"
            f"{stats.single_host_measured})"
        )
    else:
        line += f" / 単一ホスト層は標本不足 ({stats.single_host_measured} 件)"
    return line + " — 番兵 (推移を見る)。目標関数にしない (§10.2f)"
