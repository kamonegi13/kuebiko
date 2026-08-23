"""事象単位ニュースの実行器 (v1 shadow — スケジューラ非接続、scripts から呼ぶ)。

記事を錨時刻順に 1 件ずつ処理する逐次適用 (リプレイと本番で同一コード)。
状態は in-memory registry に持ち、書込は repo へ委譲する。

生成の対象は **メンバー 2 件以上のアイテムのみ** (singleton の「ニュース」は
記事要約そのものなので LLM を呼ばない — 表示層が要約を出す)。§7 の
「version 0 は無条件再生成」もメンバー 2 件以上に限る。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from src.eventnews import generator as gen
from src.eventnews import grouping, identifier_gate, state
from src.eventnews.models import (
    UPDATE_DRIVER_TYPES,
    Assignment,
    EventNewsDraft,
    GateResult,
    ItemState,
    MemberArticle,
    SourceBreakdown,
)
from src.logging_config import get_logger
from src.storage.repo_eventnews import EventNewsMixin
from src.tools.llm_client import LLMClient, LLMError

_log = get_logger(__name__)

_IMPORTANCE_RANK = {"low": 0, "medium": 1, "high": 2}
_PROMPT_VERSION = "eventnews-v1"


@dataclass
class _LiveItem:
    """処理中のアイテムの in-memory 状態 (書込済みと同期)。"""

    snapshot: ItemState
    members: list[MemberArticle] = field(default_factory=list)
    breakdown: SourceBreakdown | None = None


@dataclass(frozen=True)
class ProcessStats:
    created: int
    updated: int
    reinforced: int
    generated: int
    generation_failures: int
    dropped_lines: int
    repaired_ids: int
    substituted_ids: int


def _item_id_for(article_id: str) -> str:
    return "ev-" + hashlib.sha256(article_id.encode("utf-8")).hexdigest()[:16]


def _driver_entities(members: list[MemberArticle]) -> dict[str, frozenset[str]]:
    out: dict[str, set[str]] = {t: set() for t in UPDATE_DRIVER_TYPES}
    for m in members:
        for etype, value in m.entities:
            if etype in out:
                out[etype].add(value)
    return {k: frozenset(v) for k, v in out.items()}


def _max_importance(a: str, b: str) -> str:
    return a if _IMPORTANCE_RANK.get(a, 0) >= _IMPORTANCE_RANK.get(b, 0) else b


def _allowed_identifiers_text(members: list[MemberArticle]) -> str:
    per_member = identifier_gate.extract_allowed_by_member(members)
    lines: list[str] = []
    for n, idents in enumerate(per_member, 1):
        if not idents:
            continue
        uniq = sorted({i.raw for i in idents})
        lines.append(f"[{n}] " + " / ".join(uniq[:40]))
    return "\n".join(lines) if lines else "(識別子なし)"


async def _generate_version(
    repo: EventNewsMixin,
    item: _LiveItem,
    llm: LLMClient,
    now: datetime,
    new_facts_json: str,
) -> tuple[GateResult | None, str | None]:
    """生成 + 関門 + 版記録。失敗は fail-open (None を返す)。

    関門の照合対象は build_prompt が番号を振ったのと同一の選抜列 (select_members は
    決定論 + 安定 sort なので、selected を渡せば番号と照合対象が一致する)。
    """
    members = item.members
    try:
        selected, _omitted = gen.select_members(members)
        allowed = _allowed_identifiers_text(selected)
        draft: EventNewsDraft = await gen.generate_draft(selected, allowed, llm)
        gate: GateResult = identifier_gate.verify_draft(draft, selected)
    except LLMError as exc:
        _log.warning("eventnews_generation_failed", item_id=item.snapshot.item_id, error=str(exc))
        return None, None
    version = item.snapshot.current_version + 1
    body_json = gate.draft.model_dump_json()
    repo.record_event_version(
        item_id=item.snapshot.item_id,
        version=version,
        generated_at=now,
        model=llm.model,
        prompt_version=_PROMPT_VERSION,
        headline=gate.draft.headline,
        body_json=body_json,
        new_facts_json=new_facts_json,
        verified_at=now if gate.verified else None,
        dropped_lines=gate.dropped_lines,
        repaired_ids=gate.repaired_ids,
    )
    return gate, body_json


async def process_candidates(
    repo: EventNewsMixin,
    candidates: list[MemberArticle],
    vectors: Mapping[str, np.ndarray],
    origin: str,
    llm_factory: Callable[[], LLMClient] | None,
    *,
    generate: bool = True,
) -> ProcessStats:
    """錨時刻順の逐次適用。candidates は anchor_ts 昇順であること。"""
    items: dict[str, _LiveItem] = {}
    stats = dict.fromkeys(
        (
            "created",
            "updated",
            "reinforced",
            "generated",
            "generation_failures",
            "dropped_lines",
            "repaired_ids",
            "substituted_ids",
        ),
        0,
    )
    llm = llm_factory() if (generate and llm_factory) else None

    for cand in candidates:
        vec = vectors.get(cand.article_id)
        if vec is None:
            continue
        now = cand.anchor_ts
        snapshot_list = [it.snapshot for it in items.values()]
        member_map = {it.snapshot.item_id: tuple(it.members) for it in items.values()}
        assignment: Assignment = grouping.assign_article(
            cand, vec, snapshot_list, member_map, vectors, now
        )

        if assignment.target_item_id is None:
            item_id = _item_id_for(cand.article_id)
            breakdown = state.compute_source_breakdown([cand])
            snap = ItemState(
                item_id=item_id,
                first_reported_at=cand.anchor_ts,
                last_reported_at=cand.anchor_ts,
                status="new",
                importance=cand.importance,
                current_version=0,
                member_ids=(cand.article_id,),
            )
            items[item_id] = _LiveItem(snapshot=snap, members=[cand], breakdown=breakdown)
            repo.create_event_item(
                item_id=item_id,
                origin=origin,
                first_reported_at=cand.anchor_ts,
                last_reported_at=cand.anchor_ts,
                importance=cand.importance,
                best_source_tier=breakdown.best_tier,
                independent_sources=breakdown.independent,
                state_media_count=breakdown.state_media,
                unclassified_sources=breakdown.unclassified,
                when=now,
            )
            repo.add_event_member(
                item_id=item_id,
                article_id=cand.article_id,
                joined_at=now,
                contributed_new_facts=1,
                join_signal="seed",
            )
            stats["created"] += 1
            continue

        item = items[assignment.target_item_id]
        breakdown_before = item.breakdown or state.compute_source_breakdown(item.members)
        drivers_before = _driver_entities(item.members)
        importance_before = item.snapshot.importance
        item.members.append(cand)
        breakdown_after = state.compute_source_breakdown(item.members)
        decision = state.decide_arrival(
            drivers_before, cand, breakdown_before, breakdown_after, importance_before
        )
        new_importance = _max_importance(importance_before, cand.importance)
        version = item.snapshot.current_version
        item.snapshot = ItemState(
            item_id=item.snapshot.item_id,
            first_reported_at=min(item.snapshot.first_reported_at, cand.anchor_ts),
            last_reported_at=max(item.snapshot.last_reported_at, cand.anchor_ts),
            status=decision.kind,
            importance=new_importance,
            current_version=version,
            member_ids=(*item.snapshot.member_ids, cand.article_id),
        )
        item.breakdown = breakdown_after
        repo.add_event_member(
            item_id=item.snapshot.item_id,
            article_id=cand.article_id,
            joined_at=now,
            contributed_new_facts=1 if decision.kind == "updated" else 0,
            join_signal=",".join(f"{t}:{v}" for t, v in assignment.shared_entities[:4]),
        )
        stats[decision.kind] += 1

        needs_generation = (
            generate
            and llm is not None
            and len(item.members) >= 2
            and (decision.kind == "updated" or item.snapshot.current_version == 0)
        )
        gate = None
        if needs_generation:
            assert llm is not None  # needs_generation が保証
            new_facts_json = json.dumps(decision.new_facts, ensure_ascii=False, default=str)
            gate, _ = await _generate_version(repo, item, llm, now, new_facts_json)
            if gate is None:
                stats["generation_failures"] += 1
            else:
                stats["generated"] += 1
                stats["dropped_lines"] += gate.dropped_lines
                stats["repaired_ids"] += gate.repaired_ids
                stats["substituted_ids"] += gate.substituted_ids
                item.snapshot = ItemState(
                    **{**item.snapshot.__dict__, "current_version": version + 1}
                )
        repo.update_event_item(
            item.snapshot.item_id,
            {
                "status": decision.kind,
                "change_kind": decision.change_kind,
                "current_version": item.snapshot.current_version,
                "importance": new_importance,
                "best_source_tier": breakdown_after.best_tier,
                "independent_sources": breakdown_after.independent,
                "state_media_count": breakdown_after.state_media,
                "unclassified_sources": breakdown_after.unclassified,
                "last_reported_at": item.snapshot.last_reported_at,
                "updated_at": now,
            },
        )

    return ProcessStats(**stats)
