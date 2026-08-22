#!/usr/bin/env python3
"""C1-③ (同一事象クラスタ内の判定不整合) のオフライン実測 (2026-08-22)。

一度きりの分析スクリプト。較正格子の存廃判断 (docs/self_evolving_tuning_design.md §4
C1-③、独立レビューを受けた再評価) の材料であり、**本番配線はしない**。

背景: intent (socio_political_intent) / editorial_stance / importance には正解ラベルが
0 件。唯一の機械的検証手段は「同一事象を扱う複数記事の間で判定が一貫しているか」
(不整合 = 少なくとも一方が誤り)。victim_org 共有 (粗い代理) の予備実測では 613 組中
215 組 (35%) で intent 不一致だった。embedding クラスタは victim_org 一致よりも厳密な
「同一事象」定義であり、本スクリプトはその数字がどう動くかを見る。

設計:
- クラスタは articles.created_at (記事自身の処理時刻) で時系列ソートし、**14 日窓内**
  のペアのみコサイン類似度を計算する (時間ブロッキング。全対全は articles 数の 2 乗で
  高コストなうえ、article_embeddings.created_at は backfill バッチ日にスパイクがあり
  事象の実時間軸を表さないため、articles.created_at を使う)。
- 同一 URL の articles 行は fan-out する (毎時の再 triage で新規行が積まれるが、
  socio_political_intent / editorial_stance は status='posted' の行にしか書き戻されない
  — scripts/backfill_intent.py の `WHERE status='posted'` 参照)。url ごとに
  「posted 優先 → intent 非 NULL 優先 → 最新 created_at」で 1 行へ折り畳んでから使う。
- 閾値 t ∈ {0.80, 0.85, 0.90} で全指標をスイープ (dedup 運用: hard=0.92 / cluster=0.78 —
  その中間帯が「同一事象・別記事」)。

usage (host から。読み取りのみ):
    DATABASE_URL=postgresql://kuebiko:cti_local_dev@127.0.0.1:5433/kuebiko \\
        uv run python scripts/measure_cluster_consistency.py
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np
import psycopg
from psycopg.rows import dict_row

RUNS_DIR = Path("data/eval/runs")
EMBED_MODEL = "snowflake-arctic-embed2"
EMBED_DIM = 1024
DEFAULT_DATABASE_URL = "postgresql://kuebiko:cti_local_dev@127.0.0.1:5433/kuebiko"
DEFAULT_THRESHOLDS = (0.80, 0.85, 0.90)
DEFAULT_WINDOW_DAYS = 14.0
DEFAULT_BLOCK_SIZE = 2000
_EPOCH = datetime(2020, 1, 1, tzinfo=UTC)
_TRACKED_FIELDS = ("intent", "editorial_stance", "importance", "victim_sector")

_LOAD_SQL = """
WITH ranked AS (
    SELECT
        a.url,
        a.title,
        a.status,
        a.socio_political_intent,
        a.editorial_stance,
        a.importance,
        a.category,
        a.victim_sector_canonical,
        a.created_at,
        ROW_NUMBER() OVER (
            PARTITION BY a.url
            ORDER BY (a.status = 'posted') DESC,
                     (a.socio_political_intent IS NOT NULL) DESC,
                     a.created_at DESC
        ) AS rn
    FROM articles a
)
SELECT
    e.url_hash,
    e.vector,
    r.url,
    r.title,
    r.category,
    r.socio_political_intent AS intent,
    r.editorial_stance,
    r.importance,
    r.victim_sector_canonical AS victim_sector,
    r.created_at
FROM article_embeddings e
JOIN dedup_seen_urls d ON d.url_hash = e.url_hash
JOIN ranked r ON r.url = d.url AND r.rn = 1
WHERE e.model = %(model)s AND e.dim = %(dim)s
ORDER BY r.created_at
"""


@dataclass(frozen=True)
class ArticleRecord:
    """1 記事 (url 単位に折り畳み済み) の判定メタデータ。"""

    url_hash: str
    url: str
    host: str
    title: str
    category: str | None
    intent: str | None
    editorial_stance: str | None
    importance: str | None
    victim_sector: str | None
    created_at: datetime


@dataclass(frozen=True)
class FieldSplitStats:
    """1 フィールドの分裂率 (examined = 残り 2 記事以上の値が揃ったクラスタ)。"""

    examined_clusters: int
    split_clusters: int
    examined_articles: int

    @property
    def split_rate(self) -> float | None:
        if self.examined_clusters == 0:
            return None
        return self.split_clusters / self.examined_clusters


@dataclass(frozen=True)
class ClusterExample:
    """分裂クラスタの具体例 (stdout 表示用)。"""

    cluster_id: int
    size: int
    host_count: int
    split_fields: tuple[str, ...]
    members: tuple[dict[str, str | None], ...]


def _host(url: str) -> str:
    """URL からグルーピング用ホスト名を抽出 (www. は同一メディアとして畳む)。"""
    netloc = (urlsplit(url).hostname or "").lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def _field_value(record: ArticleRecord, field: str) -> str | None:
    """フィールド値取得。intent は unknown/NULL を除外、他は NULL のみ除外。"""
    raw = getattr(record, field)
    if raw is None:
        return None
    value = str(raw).strip()
    if not value:
        return None
    if field == "intent" and value == "unknown":
        return None
    return value


def _days_since_epoch(dt: datetime) -> float:
    return (dt - _EPOCH).total_seconds() / 86400.0


def load_records(database_url: str) -> tuple[list[ArticleRecord], np.ndarray]:
    """embedding のある記事を url 単位 1 行に折り畳んで読み込む (created_at 昇順)。"""
    records: list[ArticleRecord] = []
    vectors: list[np.ndarray] = []
    with (
        psycopg.connect(database_url, row_factory=dict_row) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(_LOAD_SQL, {"model": EMBED_MODEL, "dim": EMBED_DIM})
        for row in cur:
            buf = bytes(row["vector"])
            if len(buf) != EMBED_DIM * 4:
                # 破損/次元不一致行は分析対象から除外 (実測では未観測、防御的処理)
                continue
            vec = np.frombuffer(buf, dtype="<f4").astype(np.float32)
            norm = float(np.linalg.norm(vec))
            if norm == 0.0:
                continue
            vectors.append(vec / norm)
            records.append(
                ArticleRecord(
                    url_hash=row["url_hash"],
                    url=row["url"],
                    host=_host(row["url"]),
                    title=row["title"] or "",
                    category=row["category"],
                    intent=row["intent"],
                    editorial_stance=row["editorial_stance"],
                    importance=row["importance"],
                    victim_sector=row["victim_sector"],
                    created_at=row["created_at"],
                )
            )
    matrix = np.stack(vectors) if vectors else np.zeros((0, EMBED_DIM), dtype=np.float32)
    return records, matrix


def compute_edges(
    vectors: np.ndarray,
    days: np.ndarray,
    min_similarity: float,
    window_days: float,
    block_size: int,
) -> list[tuple[int, int, float]]:
    """時間ブロッキング付きペアワイズコサイン類似度 (i<j, days[j]-days[i]<=window_days)。

    vectors は L2 正規化済み、days は created_at 昇順に対応。全対全 (N^2) を避けるため、
    行ブロックごとに「窓内で必要な列範囲」だけを行列積で計算する。
    """
    n = vectors.shape[0]
    edges: list[tuple[int, int, float]] = []
    if n == 0:
        return edges
    # upper[i] = 排他的上限インデックス (days[upper[i]-1] - days[i] <= window_days)
    upper = np.searchsorted(days, days + window_days, side="right")
    for start in range(0, n, block_size):
        end = min(start + block_size, n)
        col_end = int(upper[end - 1])
        if col_end <= start:
            continue
        row_block = vectors[start:end]
        col_block = vectors[start:col_end]
        sims = row_block @ col_block.T
        for local_i in range(end - start):
            i = start + local_i
            j_hi = int(upper[i])
            j_lo = i + 1
            if j_hi <= j_lo:
                continue
            col_lo = j_lo - start
            col_hi = j_hi - start
            row = sims[local_i, col_lo:col_hi]
            hits = np.nonzero(row >= min_similarity)[0]
            for k in hits:
                j = j_lo + int(k)
                edges.append((i, j, float(row[k])))
    return edges


class UnionFind:
    """パス圧縮 + union by rank の素朴な実装。"""

    def __init__(self, n: int) -> None:
        self._parent = list(range(n))
        self._rank = [0] * n

    def find(self, x: int) -> int:
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self._rank[ra] < self._rank[rb]:
            ra, rb = rb, ra
        self._parent[rb] = ra
        if self._rank[ra] == self._rank[rb]:
            self._rank[ra] += 1


def build_clusters(
    n: int, edges: Iterable[tuple[int, int, float]], threshold: float
) -> list[list[int]]:
    """threshold 以上の辺で union-find し、サイズ >= 2 のクラスタのみ返す。"""
    uf = UnionFind(n)
    for i, j, sim in edges:
        if sim >= threshold:
            uf.union(i, j)
    groups: dict[int, list[int]] = {}
    for x in range(n):
        groups.setdefault(uf.find(x), []).append(x)
    return [members for members in groups.values() if len(members) >= 2]


def _host_bucket(members: Sequence[int], records: Sequence[ArticleRecord]) -> str:
    hosts = {records[i].host for i in members}
    return "single_host" if len(hosts) <= 1 else "multi_host"


def compute_field_stats(
    clusters: Sequence[Sequence[int]],
    records: Sequence[ArticleRecord],
    field: str,
) -> dict[str, FieldSplitStats]:
    """フィールド別分裂率を all / single_host / multi_host で層別に集計する。"""
    counters: dict[str, list[int]] = {
        "all": [0, 0, 0],
        "single_host": [0, 0, 0],
        "multi_host": [0, 0, 0],
    }  # [examined_clusters, split_clusters, examined_articles]
    for members in clusters:
        values = [v for i in members if (v := _field_value(records[i], field)) is not None]
        if len(values) < 2:
            continue
        is_split = len(set(values)) >= 2
        bucket = _host_bucket(members, records)
        for key in ("all", bucket):
            counters[key][0] += 1
            counters[key][1] += 1 if is_split else 0
            counters[key][2] += len(values)
    return {
        key: FieldSplitStats(examined_clusters=c[0], split_clusters=c[1], examined_articles=c[2])
        for key, c in counters.items()
    }


def _size_bucket(size: int) -> str:
    if size == 2:
        return "size_2"
    if size <= 5:
        return "size_3_5"
    return "size_6_plus"


_EXAMPLE_FIELDS = ("intent", "editorial_stance")


def _split_fields_for(members: Sequence[int], records: Sequence[ArticleRecord]) -> tuple[str, ...]:
    """cluster 内で分裂している _EXAMPLE_FIELDS を返す (examined 未達の field は含めない)。"""
    split: list[str] = []
    for field in _EXAMPLE_FIELDS:
        values = [v for i in members if (v := _field_value(records[i], field)) is not None]
        if len(values) >= 2 and len(set(values)) >= 2:
            split.append(field)
    return tuple(split)


def collect_examples(
    clusters: Sequence[Sequence[int]],
    records: Sequence[ArticleRecord],
    limit: int,
) -> list[ClusterExample]:
    """intent または editorial_stance が割れているクラスタの実例を集める。

    クロスソース (multi_host) かつ大きいクラスタを優先する — 単一メディアの連報内での
    ゆらぎより、独立ソース間の不一致のほうが判定品質監査の材料として本命のため。
    """
    candidates: list[tuple[bool, int, int, list[int], tuple[str, ...]]] = []
    for cid, members in enumerate(clusters):
        split_fields = _split_fields_for(members, records)
        if not split_fields:
            continue
        is_multi_host = _host_bucket(members, records) == "multi_host"
        candidates.append((is_multi_host, len(members), cid, list(members), split_fields))
    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)

    examples: list[ClusterExample] = []
    for _is_multi_host, size, cid, members, split_fields in candidates[:limit]:
        member_rows = tuple(
            {
                "title": records[i].title,
                "host": records[i].host,
                "intent": records[i].intent,
                "editorial_stance": records[i].editorial_stance,
                "importance": records[i].importance,
            }
            for i in members
        )
        host_count = len({records[i].host for i in members})
        examples.append(
            ClusterExample(
                cluster_id=cid,
                size=size,
                host_count=host_count,
                split_fields=split_fields,
                members=member_rows,
            )
        )
    return examples


def print_report(
    threshold: float,
    clusters: list[list[int]],
    records: Sequence[ArticleRecord],
    field_stats: dict[str, dict[str, FieldSplitStats]],
    examples: list[ClusterExample],
) -> None:
    covered = sum(len(c) for c in clusters)
    size_hist = {"size_2": 0, "size_3_5": 0, "size_6_plus": 0}
    host_hist = {"single_host": 0, "multi_host": 0}
    for members in clusters:
        size_hist[_size_bucket(len(members))] += 1
        host_hist[_host_bucket(members, records)] += 1

    print(f"\n{'=' * 72}")
    print(f"threshold t={threshold:.2f}")
    print(f"{'=' * 72}")
    print(f"  clusters (size>=2): {len(clusters)}   covered_articles: {covered}")
    print(
        "  size dist: "
        f"2件={size_hist['size_2']}  3-5件={size_hist['size_3_5']}  "
        f"6件+={size_hist['size_6_plus']}"
    )
    print(
        f"  host dist: 単一ホスト={host_hist['single_host']}  複数ホスト={host_hist['multi_host']}"
    )
    print("  field split rates (examined_clusters / split_clusters / rate):")
    for field in _TRACKED_FIELDS:
        stats = field_stats[field]
        for bucket_key in ("all", "single_host", "multi_host"):
            s = stats[bucket_key]
            rate_str = f"{s.split_rate:.1%}" if s.split_rate is not None else "n/a"
            print(
                f"    {field:16s} {bucket_key:12s}: examined={s.examined_clusters:5d}  "
                f"split={s.split_clusters:5d}  rate={rate_str}"
            )

    print(f"\n  分裂クラスタ実例 (上位 {len(examples)} 件, multi_host 優先):")
    for ex in examples:
        print(
            f"    --- cluster#{ex.cluster_id} size={ex.size} hosts={ex.host_count} "
            f"split_fields={ex.split_fields} ---"
        )
        for m in ex.members:
            print(
                f"      host={m['host']!s:24s} intent={m['intent']!s:12s} "
                f"stance={m['editorial_stance']!s:16s} title={m['title']}"
            )


def write_jsonl(
    path: Path,
    threshold: float,
    clusters: list[list[int]],
    records: Sequence[ArticleRecord],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for cid, members in enumerate(clusters):
            hosts = sorted({records[i].host for i in members})
            entry = {
                "threshold": threshold,
                "cluster_id": cid,
                "size": len(members),
                "host_count": len(hosts),
                "hosts": hosts,
                "members": [
                    {
                        "url_hash": records[i].url_hash,
                        "url": records[i].url,
                        "host": records[i].host,
                        "title": records[i].title,
                        "category": records[i].category,
                        "intent": records[i].intent,
                        "editorial_stance": records[i].editorial_stance,
                        "importance": records[i].importance,
                        "victim_sector": records[i].victim_sector,
                        "created_at": records[i].created_at.isoformat(),
                    }
                    for i in members
                ],
            }
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _summarize_field_stats(
    clusters: list[list[int]], records: Sequence[ArticleRecord]
) -> dict[str, dict[str, FieldSplitStats]]:
    return {field: compute_field_stats(clusters, records, field) for field in _TRACKED_FIELDS}


def _parse_thresholds(raw: str) -> tuple[float, ...]:
    return tuple(sorted(float(x) for x in raw.split(",") if x.strip()))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
        help="PostgreSQL 接続文字列 (読み取りのみ)",
    )
    parser.add_argument("--window-days", type=float, default=DEFAULT_WINDOW_DAYS)
    parser.add_argument(
        "--thresholds",
        default=",".join(str(t) for t in DEFAULT_THRESHOLDS),
        help="カンマ区切りのコサイン類似度閾値スイープ",
    )
    parser.add_argument("--block-size", type=int, default=DEFAULT_BLOCK_SIZE)
    parser.add_argument("--examples", type=int, default=10)
    parser.add_argument("--out-dir", default=str(RUNS_DIR))
    args = parser.parse_args()

    thresholds = _parse_thresholds(args.thresholds)
    print(f"loading article_embeddings (model={EMBED_MODEL}, dim={EMBED_DIM}) ...")
    records, vectors = load_records(args.database_url)
    print(f"loaded {len(records)} articles with embeddings")
    if not records:
        print("no records loaded — aborting")
        return 1

    days = np.array([_days_since_epoch(r.created_at) for r in records], dtype=np.float64)

    min_threshold = min(thresholds)
    print(
        f"computing time-blocked pairwise cosine similarity "
        f"(window={args.window_days}d, min_sim={min_threshold}) ..."
    )
    edges = compute_edges(vectors, days, min_threshold, args.window_days, args.block_size)
    print(f"candidate edges (sim>={min_threshold}): {len(edges)}")

    out_dir = Path(args.out_dir)
    for threshold in thresholds:
        clusters = build_clusters(len(records), edges, threshold)
        field_stats = _summarize_field_stats(clusters, records)
        examples = collect_examples(clusters, records, args.examples)
        print_report(threshold, clusters, records, field_stats, examples)
        out_path = out_dir / f"cluster-consistency-t{threshold:.2f}.jsonl"
        write_jsonl(out_path, threshold, clusters, records)
        print(f"\n  -> クラスタ明細を保存: {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
