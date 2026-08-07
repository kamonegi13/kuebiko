"""情勢ボード (Situation Ledger) の読み取り API (段D)。

「推論の可視化が最終保証」を時間軸に拡張する: 追跡中の情勢一覧 (salience 順) と、
各 Situation の revision 履歴 (確度・見立ての推移)・証拠台帳・関係を開示する。
read-only (台帳の書込は synthesis run のみ)。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException

from src.assessment.salience import salience
from src.assessment.situation_store import RevisionRow, SituationStore
from src.assessment.stateful import _revision_to_judgment

situations_api = APIRouter(prefix="/api/v1/situations", tags=["situations"])


def _rev_dict(r: RevisionRow) -> dict[str, Any]:
    return {
        "rev": r.rev,
        "claim": r.claim,
        "claim_type": r.claim_type,
        "leading_hypothesis": r.leading_hypothesis,
        "confidence": r.confidence,
        "confidence_basis": r.confidence_basis,
        "hypotheses": json.loads(r.hypotheses_json or "[]"),
        "assumptions": json.loads(r.assumptions_json or "[]"),
        "missing": json.loads(r.missing_json or "[]"),
        "indicators": json.loads(r.indicators_json or "[]"),
        "implication": r.implication,
        "delta_type": r.delta_type,
        "delta_note": r.delta_note,
        "created_at": r.created_at,
    }


@situations_api.get("")
async def list_situations(status: str = "active,dormant") -> dict[str, Any]:
    """追跡中の情勢一覧 (salience 降順)。status は CSV (active/dormant/closed)。"""
    statuses = tuple(s.strip() for s in status.split(",") if s.strip())
    store = SituationStore()
    rows = store.load_situations(statuses or ("active", "dormant"))  # type: ignore[arg-type]
    # 観測と判断は別状態: 総件数 (割当) と評価済み件数を分けて返す (UI が正直に表示する)。
    state_counts = store.evidence_state_counts([r.situation_id for r in rows])
    items: list[dict[str, Any]] = []
    for r in rows:
        latest = store.latest_revision(r.situation_id)
        sal = 0.0
        if latest is not None:
            j = _revision_to_judgment(r.situation_id, latest, domain=r.domain)
            sal = salience(j)
        counts = state_counts.get(r.situation_id, {})
        items.append(
            {
                "situation_id": r.situation_id,
                "title": r.title,
                "domain": r.domain,
                "status": r.status,
                "kind": r.kind,
                "pir_ids": list(r.pir_ids),
                "opened_at": r.opened_at,
                "last_evidence_at": r.last_evidence_at,
                "evidence_count": counts.get("total", 0),
                "assessed_count": counts.get("assessed", 0),
                "salience": round(sal, 2),
                "latest": _rev_dict(latest) if latest else None,
            }
        )
    items.sort(key=lambda x: (-x["salience"], x["situation_id"]))
    return {"situations": items, "total": len(items)}


@situations_api.get("/{situation_id}")
async def situation_detail(situation_id: str) -> dict[str, Any]:
    """1 情勢の全 revision 履歴 + 証拠台帳 + 関係 (検証可能性の開示)。"""
    store = SituationStore()
    row = store.get_situation(situation_id)
    if row is None:
        raise HTTPException(status_code=404, detail="該当する状況が見つかりません")
    repo = store._repo  # noqa: SLF001 — 読み取り専用の意図的共有
    with repo._connect() as conn:  # noqa: SLF001
        rev_rows = conn.execute(
            "SELECT * FROM situation_revisions WHERE situation_id=? ORDER BY rev",
            (situation_id,),
        ).fetchall()
        # 評価済み (ACH 引用) を先頭に、未評価の割当は added_at 降順で続ける。
        # UI は assessed_at の有無で「接地証拠」と「未評価の割当」を分けて描画する。
        ev_rows = conn.execute(
            "SELECT article_id, polarity, attribution_basis, excerpt, source_tier,"
            " added_at, assigned_by, read_at, assessed_at"
            " FROM situation_evidence WHERE situation_id=?"
            " ORDER BY CASE WHEN assessed_at IS NULL THEN 1 ELSE 0 END,"
            " COALESCE(assessed_at, added_at) DESC LIMIT 100",
            (situation_id,),
        ).fetchall()
        # 記事 title/url の一括解決 (article_id は run 横断で重複しうるため GROUP BY)。
        # 未評価の割当行は excerpt を持たない — title が無いと UI で内容が示せない。
        aids = sorted({str(e["article_id"]) for e in ev_rows if e["article_id"]})
        article_meta: dict[str, dict[str, str]] = {}
        if aids:
            ph = ",".join("?" for _ in aids)
            meta_rows = conn.execute(
                "SELECT article_id, MAX(title) AS title, MAX(url) AS url"
                f" FROM articles WHERE article_id IN ({ph})"  # noqa: S608 — ph は ? 固定
                " GROUP BY article_id",
                aids,
            ).fetchall()
            article_meta = {
                str(m["article_id"]): {
                    "title": str(m["title"] or ""),
                    "url": str(m["url"] or ""),
                }
                for m in meta_rows
            }
    relations = store.relations_for([situation_id])
    # 相手側の title を引いて表示可能にする
    other_ids = sorted(
        {r["a_id"] for r in relations} | {r["b_id"] for r in relations} - {situation_id}
    )
    titles = {
        s.situation_id: s.title
        for s in (store.get_situation(oid) for oid in other_ids)
        if s is not None
    }
    return {
        "situation": {
            "situation_id": row.situation_id,
            "title": row.title,
            "domain": row.domain,
            "status": row.status,
            "anchors": sorted(row.anchors),
            "pir_ids": list(row.pir_ids),
            "opened_at": row.opened_at,
            "last_evidence_at": row.last_evidence_at,
        },
        "revisions": [_rev_dict(store._row_to_revision(r)) for r in rev_rows],  # noqa: SLF001
        "evidence": [
            {
                "article_id": str(e["article_id"]),
                "polarity": str(e["polarity"]),
                "attribution_basis": str(e["attribution_basis"]),
                "excerpt": str(e["excerpt"]),
                "source_tier": str(e["source_tier"]),
                "added_at": str(e["added_at"]),
                "assigned_by": str(e["assigned_by"]),
                "read_at": str(e["read_at"] or ""),
                "assessed_at": str(e["assessed_at"] or ""),
                "article_title": article_meta.get(str(e["article_id"]), {}).get("title", ""),
                "article_url": article_meta.get(str(e["article_id"]), {}).get("url", ""),
            }
            for e in ev_rows
        ],
        "relations": [
            {
                **r,
                "other_title": titles.get(
                    r["b_id"] if r["a_id"] == situation_id else r["a_id"], ""
                ),
            }
            for r in relations
        ],
    }
