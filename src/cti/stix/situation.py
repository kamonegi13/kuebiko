"""台帳 (situation) 1 件 → STIX 2.1 bundle (2026-09-27)。

台帳の型 (situation_track) で中心のオブジェクトを変える:
- ``campaign`` の台帳 → STIX ``campaign`` (first_seen = 開設、last_seen = 最終証拠)。
  鍵に辞書の group があれば ``attributed-to`` で intrusion-set に結ぶ (帰属は任意)
- ``actor`` の台帳 → 鍵の group の ``intrusion-set`` (持続的な主体の追跡)

どちらも、証拠の記事 (report) と中心のオブジェクトを ``grouping`` で束ね、ACH の判断
(先頭仮説・仮説ごとの整合/不整合・前提・欠けている証拠・兆候) を ``note`` にして確度を付ける。
証拠は評価済み (ACH が引用した) ものだけ — 弱い証拠 (weak_at) は store が外す。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from src.cti.stix import objects as o
from src.cti.stix.article import build_article_bundle
from src.cti.stix.core import (
    PRODUCER_ID,
    bundle,
    confidence_value,
    now_ts,
    sdo,
    to_ts,
    with_extension,
)
from src.cti.stix.facts import facts_from_db

if TYPE_CHECKING:
    from src.assessment.situation_store import RevisionRow, SituationRow, SituationStore
    from src.cti.actor_normalizer import ActorAlias, ActorAliasRegistry
    from src.storage.run_history import RunHistoryRepository

#: 1 台帳で書き出す証拠記事の上限 (新しい順)
MAX_EVIDENCE = 30
_HEADER_TYPES = frozenset({"marking-definition", "extension-definition"})


def _json_list(raw: str) -> list[Any]:
    try:
        v = json.loads(raw or "[]")
    except ValueError:
        return []
    return v if isinstance(v, list) else []


def _anchor_groups(row: SituationRow, registry: ActorAliasRegistry) -> list[ActorAlias]:
    out: list[ActorAlias] = []
    for a in sorted(row.anchors):
        if not a.lower().startswith("actor:"):
            continue
        actor = registry.by_id(registry.resolve_actor_id(a.split(":", 1)[1]))
        if actor is not None and not o.is_org(actor) and actor not in out:
            out.append(actor)
    return out


def _evidence_objects(
    repo: RunHistoryRepository, registry: ActorAliasRegistry, article_ids: list[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    """証拠記事の bundle の中身 (見出し系を除く) と report の ID。"""
    objs: list[dict[str, Any]] = []
    report_ids: list[str] = []
    for aid in article_ids:
        facts = facts_from_db(repo, aid)
        if facts is None:
            continue
        for x in build_article_bundle(facts, registry)["objects"]:
            # 見出し (TLP・拡張定義・作成者) は外側の bundle() が 1 度だけ入れる
            if x["type"] in _HEADER_TYPES or x["id"] == PRODUCER_ID:
                continue
            objs.append(x)
            if x["type"] == "report":
                report_ids.append(x["id"])
    return objs, report_ids


def _ach_note(rev: RevisionRow, target_refs: list[str], sid: str) -> dict[str, Any]:
    hyps = [h for h in _json_list(rev.hypotheses_json) if isinstance(h, dict)]
    lines = [f"先頭仮説: {rev.leading_hypothesis} (確度 {rev.confidence})"]
    lines += [
        f"- {h.get('hypothesis', '')} — 整合 {h.get('consistent', 0)} / 不整合 "
        f"{h.get('inconsistent', 0)} ({h.get('verdict', '')})"
        for h in hyps
    ]
    if rev.implication:
        lines.append(f"含意: {rev.implication}")
    ts = to_ts(rev.created_at) or now_ts()
    note = sdo(
        "note",
        f"situation|{sid}|rev{rev.rev}",
        _ts=ts,
        abstract=f"ACH: {rev.leading_hypothesis}"[:250],
        content="\n".join(lines),
        object_refs=target_refs,
        confidence=confidence_value(rev.confidence),
    )
    return with_extension(
        note,
        {
            "situation_id": sid,
            "revision": rev.rev,
            "claim_type": rev.claim_type,
            "leading_hypothesis": rev.leading_hypothesis,
            "confidence_level": rev.confidence,
            "confidence_basis": rev.confidence_basis,
            "hypotheses": hyps,
            "assumptions": _json_list(rev.assumptions_json),
            "missing_evidence": _json_list(rev.missing_json),
            "indicators": _json_list(rev.indicators_json),
        },
    )


def _core_objects(
    row: SituationRow, rev: RevisionRow | None, groups: list[ActorAlias]
) -> list[dict[str, Any]]:
    """中心のオブジェクト (campaign か intrusion-set) と、その帰属。"""
    sid = row.situation_id
    opened = to_ts(row.opened_at) or now_ts()
    last = max(opened, to_ts(row.last_evidence_at) or opened)
    if row.track == "actor" and groups:
        return [o.actor_object(g) for g in groups]
    camp = sdo(
        "campaign",
        f"situation|{sid}",
        _ts=opened,
        name=row.title[:250],
        description=rev.implication if rev else None,
        first_seen=opened,
        last_seen=last,
        objective=(rev.claim if rev else row.title)[:500],
        confidence=confidence_value(rev.confidence) if rev else None,
    )
    camp = {**camp, "modified": last}
    out = [camp, *(o.actor_object(g) for g in groups)]
    out += [
        o.relationship(
            camp["id"], "attributed-to", o.actor_ref(g), context=f"situation|{sid}", created=opened
        )
        for g in groups
    ]
    return out


def build_situation_bundle(
    store: SituationStore,
    repo: RunHistoryRepository,
    registry: ActorAliasRegistry,
    situation_id: str,
    *,
    max_evidence: int = MAX_EVIDENCE,
) -> dict[str, Any] | None:
    """台帳 1 件の STIX 2.1 bundle (台帳が無ければ None)。"""
    row = store.get_situation(situation_id)
    if row is None:
        return None
    rev = store.latest_revision(situation_id)
    items = store.evidence_items(situation_id, limit=max_evidence)
    evidence_ids = [i["article_id"] for i in items]
    groups = _anchor_groups(row, registry)
    core = _core_objects(row, rev, groups)
    core_refs = [x["id"] for x in core if x["type"] in ("campaign", "intrusion-set")]
    ev_objs, report_ids = _evidence_objects(repo, registry, evidence_ids)
    opened = to_ts(row.opened_at) or now_ts()
    grouping = with_extension(
        sdo(
            "grouping",
            f"situation|{situation_id}",
            _ts=opened,
            name=row.title[:250],
            description=rev.claim if rev else None,
            context="suspicious-activity",
            object_refs=[*core_refs, *report_ids] or [x["id"] for x in core],
        ),
        {
            "situation_id": situation_id,
            "track": row.track,
            "kind": row.kind,
            "domain": row.domain,
            "status": row.status,
            "pir_ids": list(row.pir_ids),
            "evidence": [{"article_id": i["article_id"], "polarity": i["polarity"]} for i in items],
        },
    )
    objs: list[dict[str, Any]] = [grouping, *core, *ev_objs]
    if rev is not None:
        objs.append(_ach_note(rev, [*core_refs, grouping["id"]], situation_id))
    return bundle(objs, key=f"situation|{situation_id}")
