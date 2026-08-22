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

WINDOW_DAYS = 14
SIM_THRESHOLD = 0.85
# クラスタ統計が意味を持つ最小標本 (下回ったら「標本不足」— 0 件と測れないを区別)
MIN_ROWS = 50
# 週次ガバナンス内で走るため計算量の安全弁 (新しい方を優先)
MAX_ROWS = 8000
_DEFAULT_EMBED_MODEL = "snowflake-arctic-embed2"


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


def cluster_indices(matrix: NDArray[np.float32], threshold: float) -> list[list[int]]:
    """コサイン類似度 >= threshold の連結成分 (union-find)。行は L2 正規化して比較する。"""
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
            if i < j:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[ri] = rj
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return [g for g in groups.values() if len(g) >= 2]


def compute_from_rows(rows: list[dict[str, Any]], *, threshold: float) -> ConsistencyStats:
    """(intent, url, vector) の行群から不安定率を計算する純関数部。"""
    vectors: list[NDArray[np.float32]] = []
    intents: list[str] = []
    hosts: list[str] = []
    for r in rows:
        vec = np.frombuffer(r["vector"], dtype="<f4")
        if int(r.get("dim") or 0) and vec.shape[0] != int(r["dim"]):
            continue
        vectors.append(vec)
        intents.append(str(r["intent"]))
        hosts.append(normalize_host(str(r.get("url") or "")))
    if not vectors:
        return ConsistencyStats(0, 0, 0, 0, 0)
    matrix = np.vstack(vectors).astype(np.float32)

    measured = split = sh_measured = sh_split = 0
    for group in cluster_indices(matrix, threshold):
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
    return compute_from_rows(rows, threshold=threshold)


def sentinel_line(stats: ConsistencyStats | None) -> str:
    """週次ガバナンス ops 本文の 1 行。番兵であり合否ではない (目標関数化の禁止)。"""
    if stats is None:
        return "🧭 intent 不安定率: 標本不足で今週は測定なし"
    return (
        f"🧭 intent 不安定率 ({WINDOW_DAYS}日, t={SIM_THRESHOLD}): "
        f"{stats.rate * 100:.0f}% ({stats.split_clusters}/{stats.measured_clusters} クラスタ)"
        f" / 単一ホスト連報 {stats.single_host_rate * 100:.0f}%"
        f" ({stats.single_host_split}/{stats.single_host_measured})"
        " — 番兵 (推移を見る)。目標関数にしない (§10.2f)"
    )
