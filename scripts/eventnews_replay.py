"""事象単位ニュースのリプレイ評価 (v1 shadow、設計 §12/§13)。

本番 PG を read-only で読み、過去 N 日を錨時刻順に逐次適用して
ローカル SQLite (data/eventnews_replay.db 既定) へ origin='replay' で書く。
評価レポート (E1'-E6 の材料) を markdown で出力する。

実行例 (host):
  DATABASE_URL=postgresql://kuebiko:cti_local_dev@127.0.0.1:5433/kuebiko \
  uv run python scripts/eventnews_replay.py --days 10 --generate --out /tmp/eventnews_eval.md
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config_loader import load_app_config
from src.eventnews.grouping import build_join_entities
from src.eventnews.models import JOIN_ENTITY_TYPES, MemberArticle
from src.eventnews.runner import ProcessStats, process_candidates
from src.storage.event_time import DEDUP_ARTICLES, EVENT_TS_EXPR
from src.storage.run_history import RunHistoryRepository
from src.tools.llm_client import LLMClient
from src.tools.model_tiers import Step, build_llm_for

_TS = EVENT_TS_EXPR.format(a="a")

_SQL_ARTICLES = f"""
select a.article_id, {_TS} as anchor_ts, x.importance, x.category, x.status,
       x.title, x.url, coalesce(x.feed_title,''), coalesce(x.feed_url,''),
       coalesce(x.summary,''), coalesce(x.body,''), e.vector, e.dim
from {DEDUP_ARTICLES} a
join (
  select article_id, max(importance) importance, max(category) category,
         max(status) status, max(title) title, max(url) url,
         max(feed_title) feed_title, max(feed_url) feed_url,
         max(summary) summary, max(body) body
  from articles group by article_id
) x on x.article_id = a.article_id
join article_embeddings e on e.url = x.url
where {_TS} > now() - make_interval(days => %s)
  and x.importance in ('high','medium')
  and x.status in ('posted','skipped_duplicate')
order by 2
"""

_SQL_ENTITIES = """
select ae.article_id, ae.entity_type, lower(trim(ae.value))
from article_entities ae
where ae.entity_type = any(%s) and length(trim(ae.value)) >= 4
"""


def _fetch(days: int) -> tuple[list[MemberArticle], dict[str, np.ndarray]]:
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        rows = conn.execute(_SQL_ARTICLES, (days,)).fetchall()
        ents = conn.execute(_SQL_ENTITIES, (list(JOIN_ENTITY_TYPES),)).fetchall()

    raw_entities = [(aid, et, v) for aid, et, v in ents]
    counts: collections.Counter[tuple[str, str]] = collections.Counter(
        (et, v) for _, et, v in raw_entities
    )
    join_ents = build_join_entities(raw_entities, counts)

    members: list[MemberArticle] = []
    vectors: dict[str, np.ndarray] = {}
    for aid, ts, imp, cat, status, title, url, ft, fu, summary, body, vec, dim in rows:
        v = np.frombuffer(bytes(vec), dtype=np.float32)
        if v.shape[0] != dim:
            continue
        anchor = ts if ts.tzinfo else ts.replace(tzinfo=UTC)
        host = url.split("/")[2] if "://" in url else ""
        vectors[aid] = v / np.linalg.norm(v)
        members.append(
            MemberArticle(
                article_id=aid,
                title=title,
                url=url,
                feed_title=ft,
                feed_url=fu,
                host=host,
                importance=imp,
                category=cat,
                status=status,
                anchor_ts=anchor,
                summary=summary,
                body=body,
                entities=join_ents.get(aid, frozenset()),
            )
        )
    return members, vectors


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--generate", action="store_true", help="実 LLM で生成まで行う")
    ap.add_argument("--db", default="data/eventnews_replay.db")
    ap.add_argument("--out", default="/tmp/eventnews_eval.md")
    args = ap.parse_args()

    members, vectors = _fetch(args.days)
    print(f"候補 {len(members)} 記事 ({args.days} 日)", flush=True)

    db_path = Path(args.db)
    if db_path.exists():
        db_path.unlink()
    repo = RunHistoryRepository(db_path)

    llm_factory = None
    if args.generate:
        config = load_app_config()

        def llm_factory() -> LLMClient:  # type: ignore[misc]
            return build_llm_for(Step.EVENT_NEWS, config)

    t0 = datetime.now(UTC)
    stats: ProcessStats = asyncio.run(
        process_candidates(repo, members, vectors, "replay", llm_factory, generate=args.generate)
    )
    elapsed = (datetime.now(UTC) - t0).total_seconds()

    report = _build_report(repo, stats, args, len(members), elapsed)
    Path(args.out).write_text(report, encoding="utf-8")
    print(f"wrote {args.out} (elapsed {elapsed:.0f}s)", flush=True)


def _build_report(
    repo: RunHistoryRepository,
    stats: ProcessStats,
    args: argparse.Namespace,
    n_candidates: int,
    elapsed: float,
) -> str:
    items = repo.list_event_items(origin="replay", limit=100000)
    multi = [i for i in items if len(i.state.member_ids) >= 2]
    lines = [
        "# 事象単位ニュース リプレイ評価 (v1 shadow)",
        "",
        f"- 窓: {args.days} 日 / 候補 {n_candidates} 記事 / 実行 {elapsed:.0f}s",
        f"- アイテム: {len(items)} (複数メンバー {len(multi)})",
        f"- 遷移: created {stats.created} / updated {stats.updated}"
        f" / reinforced {stats.reinforced}",
        f"- E4' reinforced 比率 (updated+reinforced 中): "
        f"{stats.reinforced / max(stats.updated + stats.reinforced, 1) * 100:.0f}% (合格線 >=30%)",
        f"- 生成: {stats.generated} (失敗 {stats.generation_failures})",
        f"- 関門: 落下 facts 行 {stats.dropped_lines} / 修復 {stats.repaired_ids}"
        f" / 置換 {stats.substituted_ids}",
        "",
        "## 複数メンバーのアイテム (E3 目視対象)",
        "",
    ]
    for it in sorted(multi, key=lambda x: -len(x.state.member_ids)):
        lines.append(
            f"### {it.state.item_id} — {len(it.state.member_ids)} 記事 /"
            f" 独立 {it.independent_sources} 媒体"
            f" (国営 {it.state_media_count}・未分類 {it.unclassified_sources})"
            f" / {it.state.importance} / {it.state.status}"
        )
        versions = repo.list_event_versions(it.state.item_id)
        if versions:
            latest = versions[-1]
            lines.append(f"**{latest.headline}** (v{latest.version})")
            body = json.loads(latest.body_json)
            lines.append("")
            lines.append(f"> {body.get('bluf', '')}")
            for f in body.get("facts", []):
                lines.append(f"> - {f['text']} [{f['source_index']}]")
            for d in body.get("discrepancies", []):
                lines.append(f"> - (相違) {d['text']}")
            for u in body.get("unknowns", []):
                lines.append(f"> - (未確認) {u}")
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
