"""週次深掘りの閲覧 API (2026-09-27)。

深掘りの本文 (weekly_recaps) は「ブリーフ・振り返り」の週次表示に埋もれていて、
実質読む場所が無かった。本文と、選んだ記事 (f1_selections の得点つき) を週ごとに返す。
読み取り専用。内容は配信済みの記事の要約から作ったもので、公開面でも読めてよい。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from src.storage.run_history import RunHistoryRepository

deep_dives_api = APIRouter(prefix="/api/v1/deep-dives", tags=["deep-dives"])

#: 一覧で返す週の既定数と上限
_DEFAULT_WEEKS = 8
_MAX_WEEKS = 52


def _recaps(repo: RunHistoryRepository, limit: int) -> list[dict[str, Any]]:
    with repo._connect() as conn:  # noqa: SLF001 — 読み取り専用の集約
        rows = conn.execute(
            "SELECT run_id, period_label, recap_text, candidate_count, generated_at"
            " FROM weekly_recaps ORDER BY generated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def _selections(repo: RunHistoryRepository, run_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    if not run_ids:
        return {}
    ph = ",".join("?" for _ in run_ids)
    with repo._connect() as conn:  # noqa: SLF001
        rows = conn.execute(
            "SELECT s.run_id, s.article_id, s.composite_score, s.pir, s.roi,"  # noqa: S608
            " a.title, a.url, a.feed_title, a.importance"
            " FROM f1_selections s"
            " LEFT JOIN (SELECT article_id, MAX(title) AS title, MAX(url) AS url,"
            "   MAX(feed_title) AS feed_title, MAX(importance) AS importance"
            "   FROM articles GROUP BY article_id) a ON a.article_id = s.article_id"
            f" WHERE s.run_id IN ({ph}) ORDER BY s.run_id, s.composite_score DESC",
            run_ids,
        ).fetchall()
    out: dict[int, list[dict[str, Any]]] = {}
    for r in rows:
        d = dict(r)
        out.setdefault(int(d["run_id"]), []).append(
            {
                "article_id": str(d["article_id"]),
                "title": str(d["title"] or ""),
                "url": str(d["url"] or ""),
                "feed_title": str(d["feed_title"] or ""),
                "importance": str(d["importance"] or ""),
                "composite": round(float(d["composite_score"]), 2),
                "pir": round(float(d["pir"]), 1),
                "roi": round(float(d["roi"]), 1),
            }
        )
    return out


@deep_dives_api.get("")
def list_deep_dives(
    weeks: int = Query(default=_DEFAULT_WEEKS, ge=1, le=_MAX_WEEKS),
) -> dict[str, Any]:
    """直近の週次深掘り (新しい順)。各週 = 本文 + 選んだ記事 (得点の高い順)。

    ⚠ 重い集約を含むため def (同期) で定義する (async だと event loop を塞ぐ)。
    """
    repo = RunHistoryRepository()
    recaps = _recaps(repo, weeks)
    run_ids = [int(r["run_id"]) for r in recaps if r.get("run_id") is not None]
    sel = _selections(repo, run_ids)
    items = [
        {
            "period_label": str(r["period_label"]),
            "generated_at": str(r["generated_at"]),
            "candidate_count": int(r["candidate_count"] or 0),
            "recap_text": str(r["recap_text"] or ""),
            "selections": sel.get(int(r["run_id"]), []) if r.get("run_id") is not None else [],
        }
        for r in recaps
    ]
    return {"items": items, "total": len(items)}
