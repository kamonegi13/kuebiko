"""過剰統合された事象を、ペア判定に基づいて割り直す (遡及分割)。

遡及統合の対称操作。群内の全ペアを本番と同じ判定 (``pair_shadow.judge_pairs``) に
通し、承認辺の連結成分に割る。最大成分が既存の id と URL を保つ本体で、
外れたメンバーは単独の事象として立て直す。

⭐ 較正 (2026-09-02、監査済み 106 群・過剰統合 43・群内 1,250 ペアを本番判定に通した):
   連結成分 t=0.70 が頂点 — 群 P 100% / R 70%、記事 P 99% / R 76%、誤除去 1 件
   (その 1 件はデイリーダイジェストの分離で、運用原則ではむしろ正しい側)。
   t を上げると誤 split が 2〜6 件出て一括適用に向かない。quorum 系 (平均確率 /
   承認率) は R 84-90% まで届くが誤除去 8〜24 件で不採用 — 正しい群を壊す方が
   残る過剰統合より高くつく。
⚠ 統合と同じく、適用は本文の再生成までがセット (--no-generate で外せる)。

使い方:
    python scripts/retro_split_events.py [--min-members 3] [--apply] [--limit N]
既定は dry-run。
"""

import argparse
import asyncio
import sys
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

sys.path.insert(0, "/app")

import hashlib

import numpy as np

from src.config_loader import AppConfig, load_app_config
from src.eventnews import pair_shadow
from src.eventnews.models import ENTITY_FREQ_WINDOW_HOURS, MemberArticle
from src.eventnews.runner import _max_importance
from src.eventnews.split import split_components
from src.eventnews.state import compute_source_breakdown
from src.storage.repo_eventnews import EventItemRecord
from src.storage.run_history import RunHistoryRepository
from src.tools.model_tiers import Step, build_llm_for
from src.ui.services.eventnews_hourly_job import (
    _embed_summaries,
    _entity_counts,
    _load_members,
    _load_vectors,
    regenerate_pending_bodies,
)

#: 承認辺の閾値。較正 (2026-09-02) の結果で確定する。
DEFAULT_EDGE_THRESHOLD = 0.7


def _split_target_id(repo: RunHistoryRepository, article_id: str, current_item: str) -> str:
    """外すメンバーの新しい事象 id。創設者を外す場合は群 id と衝突するため別名にする。"""
    base = "ev-" + hashlib.sha256(article_id.encode("utf-8")).hexdigest()[:16]
    if base != current_item:
        existing = repo.get_event_item(base)
        if existing is None:
            return base
        if existing.merged_into:
            # 過去に吸収された自分の事象を蘇生する (URL が戻る)
            return base
    return "ev-" + hashlib.sha256(f"{article_id}:split".encode()).hexdigest()[:16]


def _refresh_item_state(
    repo: RunHistoryRepository, item_id: str, members: list[MemberArticle]
) -> None:
    breakdown = compute_source_breakdown(members)
    importance = ""
    for m in members:
        importance = _max_importance(importance, m.importance)
    repo.update_event_item(
        item_id,
        {
            "current_version": 0,  # メンバーが変わったので本文を作り直す
            "importance": importance,
            "best_source_tier": breakdown.best_tier,
            "independent_sources": breakdown.independent,
            "state_media_count": breakdown.state_media,
            "unclassified_sources": breakdown.unclassified,
            "first_reported_at": min(m.anchor_ts for m in members),
            "last_reported_at": max(m.anchor_ts for m in members),
            "merged_into": None,
            "updated_at": datetime.now(UTC),
        },
    )


async def _judge_all(
    records: list[EventItemRecord],
    repo: RunHistoryRepository,
    counts: Mapping[tuple[str, str], int],
    config: AppConfig,
    threshold: float,
) -> list[tuple[EventItemRecord, list[MemberArticle], list[str], list[list[str]]]]:
    """全群の判定と成分分解。⚠ イベントループは **1 つ** — 群ごとに asyncio.run すると
    2 群目以降で LLM クライアントの接続が `Event loop is closed` で死ぬ
    (2026-09-03 の dry-run で発覚。判定失敗 → 中立 → 切れやすい、まで連鎖した)。"""
    llm = build_llm_for(Step.TRIAGE, config)
    out: list[tuple[EventItemRecord, list[MemberArticle], list[str], list[list[str]]]] = []
    failed_pairs = 0
    for gi, r in enumerate(records, 1):
        members_map = _load_members(repo, list(r.state.member_ids), counts)
        ordered = sorted(members_map.values(), key=lambda m: m.anchor_ts)
        vecs: dict[str, np.ndarray] = {}
        for aid, v in _load_vectors(repo, [m.article_id for m in ordered]).items():
            n = float(np.linalg.norm(v))
            if n:
                vecs[aid] = v / n
        pairs = [
            (ordered[i], ordered[j])
            for i in range(len(ordered))
            for j in range(i + 1, len(ordered))
            if ordered[i].article_id in vecs and ordered[j].article_id in vecs
        ]
        verdicts = await pair_shadow.judge_pairs(
            pairs,
            vecs,
            llm=llm,
            embed_summary=lambda arts: _embed_summaries(config, arts),
        )
        probas = {v.key: v.ml_proba for v in verdicts}
        failed_pairs += sum(1 for v in verdicts if v.ml_proba is None)
        ids = [m.article_id for m in ordered]
        main_ids, rest = split_components(ids, probas, edge_threshold=threshold)
        if rest:
            out.append((r, ordered, main_ids, rest))
        if gi % 25 == 0:
            print(f"  … {gi}/{len(records)} 判定済み", flush=True)
    if failed_pairs:
        print(f"⚠ 判定できなかったペア {failed_pairs} 件 (繋がっている扱い)", flush=True)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-members", type=int, default=3)
    ap.add_argument("--threshold", type=float, default=DEFAULT_EDGE_THRESHOLD)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=None, help="判定する群の上限 (LLM 節約)")
    ap.add_argument("--no-generate", action="store_true")
    ap.add_argument("--sleep", type=float, default=3.0)
    args = ap.parse_args()

    repo = RunHistoryRepository()
    records = [
        r
        for r in repo.list_event_items(origin="live", limit=20000)
        if not r.merged_into and len(r.state.member_ids) >= args.min_members
    ]
    records.sort(key=lambda r: r.state.last_reported_at, reverse=True)
    if args.limit:
        records = records[: args.limit]
    print(f"対象 (メンバー {args.min_members} 件以上): {len(records)} 群", flush=True)

    counts = _entity_counts(repo, datetime.now(UTC) - timedelta(hours=ENTITY_FREQ_WINDOW_HOURS))
    config = load_app_config()

    t0 = time.monotonic()
    proposals = asyncio.run(_judge_all(records, repo, counts, config, args.threshold))
    split_count = 0
    for r, ordered, main_ids, rest in proposals:
        split_count += 1
        by_id = {m.article_id: m for m in ordered}
        pieces = " ｜ ".join(" / ".join(by_id[a].title[:34] for a in comp) for comp in rest)
        print(
            f"  {r.state.item_id} 本体 {len(main_ids)} 件、分離 {len(rest)} 塊: {pieces}",
            flush=True,
        )
        if not args.apply:
            continue
        for comp in rest:
            lead = comp[0]
            target = _split_target_id(repo, lead, r.state.item_id)
            if repo.get_event_item(target) is None:
                member = by_id[lead]
                repo.create_event_item(
                    item_id=target,
                    origin="live",
                    first_reported_at=member.anchor_ts,
                    last_reported_at=member.anchor_ts,
                    importance=member.importance,
                    # ⭐ 分割の由来 = 「別事象だが関連」。読み手が失う一覧性を
                    #    関連事象欄で返すための決定論リンク (2026-09-03)。
                    related_to=r.state.item_id,
                )
            for aid in comp:
                repo.move_event_member(
                    article_id=aid,
                    from_item=r.state.item_id,
                    to_item=target,
                    join_signal="retro_split",
                )
            _refresh_item_state(repo, target, [by_id[a] for a in comp])
        _refresh_item_state(repo, r.state.item_id, [by_id[a] for a in main_ids])

    print(
        f"\n{'適用' if args.apply else 'dry-run'}: {split_count} 群を分割"
        f" ({time.monotonic() - t0:.0f}s)",
        flush=True,
    )
    if args.apply and split_count and not args.no_generate:
        regenerate_pending_bodies(repo, args.sleep)
    if not args.apply:
        print("書き込むには --apply を付ける", flush=True)


if __name__ == "__main__":
    main()
