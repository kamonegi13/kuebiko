"""事象 (事象ニュース 1 件) → STIX 2.1 bundle (2026-09-27)。

事象は kuebiko が複数の記事から書いた「何が起きたか」の説明 → kuebiko を作成者とする
``report`` にする (見出し・BLUF・要点)。構成記事はそれぞれの report (記事の組み立てと同じ) を
含め、事象の report の ``object_refs`` から指す。

事実・食い違い・但し書きは、出典の記事 id つきで kuebiko 拡張に入れる (本文の [N] は
構成記事の並び = list_event_members の順)。独立した媒体数・変化の種別・未解明の点も拡張へ。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from src.cti.stix.article import build_article_bundle
from src.cti.stix.core import PRODUCER_ID, bundle, now_ts, sdo, to_ts, with_extension
from src.cti.stix.facts import facts_from_db

if TYPE_CHECKING:
    from src.cti.actor_normalizer import ActorAliasRegistry
    from src.storage.run_history import RunHistoryRepository

#: 1 事象で書き出す構成記事の上限 (並びの先頭から)
MAX_MEMBERS = 30
_HEADER_TYPES = frozenset({"marking-definition", "extension-definition"})


def _sourced(items: list[Any], member_ids: list[str]) -> list[dict[str, str]]:
    """{text, source_index (1 始まり)} → {text, article_id}。番号が範囲外なら article_id は空。"""
    out: list[dict[str, str]] = []
    for it in items:
        if not isinstance(it, dict) or not it.get("text"):
            continue
        idx = it.get("source_index")
        aid = member_ids[idx - 1] if isinstance(idx, int) and 1 <= idx <= len(member_ids) else ""
        out.append({"text": str(it["text"]), "article_id": aid})
    return out


def build_event_bundle(
    repo: RunHistoryRepository, registry: ActorAliasRegistry, item_id: str
) -> dict[str, Any] | None:
    """事象 1 件の STIX 2.1 bundle (無い・統合済みなら None)。"""
    record = repo.get_event_item(item_id)
    if record is None or record.merged_into:
        return None
    versions = repo.list_event_versions(item_id)
    latest = versions[0] if versions else None
    body: dict[str, Any] = json.loads(latest.body_json) if latest and latest.body_json else {}
    member_ids = [m.article_id for m in repo.list_event_members(item_id)]
    objs: list[dict[str, Any]] = []
    report_ids: list[str] = []
    for aid in member_ids[:MAX_MEMBERS]:
        facts = facts_from_db(repo, aid)
        if facts is None:
            continue
        for x in build_article_bundle(facts, registry)["objects"]:
            if x["type"] in _HEADER_TYPES or x["id"] == PRODUCER_ID:
                continue
            objs.append(x)
            if x["type"] == "report":
                report_ids.append(x["id"])
    first = to_ts(record.state.first_reported_at) or now_ts()
    last = max(first, to_ts(record.state.last_reported_at) or first)
    key_points = [str(p) for p in body.get("key_points", []) if p]
    bluf = str(body.get("bluf") or "")
    description = "\n".join([bluf, *(f"- {p}" for p in key_points)]).strip() or None
    event_report = sdo(
        "report",
        f"event|{item_id}",
        _ts=first,
        name=(latest.headline if latest else "") or item_id,
        description=description,
        published=last,
        report_types=["threat-report"],
        object_refs=report_ids or [PRODUCER_ID],
    )
    event_report = with_extension(
        {**event_report, "modified": last},
        {
            "event_id": item_id,
            "event_status": record.state.status,
            "change_kind": record.change_kind,
            "importance": record.state.importance or None,
            "independent_sources": record.independent_sources,
            "version": latest.version if latest else None,
            "key_points": key_points,
            "facts": _sourced(body.get("facts", []), member_ids),
            "discrepancies": _sourced(body.get("discrepancies", []), member_ids),
            "caveats": _sourced(body.get("caveats", []), member_ids),
            "unknowns": [str(u) for u in body.get("unknowns", []) if u],
        },
    )
    return bundle([event_report, *objs], key=f"event|{item_id}")
