"""分析チャットの決定論 read-only データツール (2026-07-12)。

LLM (orchestrator) は「どのツールをどの引数で呼ぶか」を選ぶだけで、実行は本 module の
parameterized handler が行う。任意 SQL / shell / 書き込みは構造的に不可能 —
プロンプトインジェクションへの主防御は「ツール語彙が固定で read-only」であること。
引数はすべて defensive に clamp する (LLM 出力を信頼しない)。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository
from src.tools.embedding_client import EmbeddingClient

_log = get_logger(__name__)

# LLM plan プロンプトに注入するツールカタログ (語彙の SSoT はこの file)。
TOOL_CATALOG_PROMPT = """\
- search_articles: 収集記事の hybrid 検索 (キーワード+意味)。
  args: {"query": "検索語 (必須)", "days": 1-365 (既定30), "limit": 1-20 (既定12)}
- article_stats: 記事件数の集計。
  args: {"days": 1-365 (既定30), "group_by": "category" | "importance" | "intent"}
- list_situations: 状況台帳 (継続追跡中の情勢ラインと最新判定・確度)。
  args: {"query": "title 絞り込み語 (省略可)", "limit": 1-12 (既定8)}
- actor_activity: 脅威アクターの活動集約 (件数/意図/標的分野/spike)。
  args: {"days": 1-365 (既定30), "nation": "cn"|"kp"|"ru"|"ir"|"" (既定 = 全て)}
- actor_profile: 特定の脅威アクター 1 件の辞書情報 (別名/帰属/概説) + 活動状況 + 関連記事。
  args: {"name": "アクター名 (必須。別名でも可、例: Cicada)", "days": 1-365 (既定90)}\
"""

_DAYS_DEFAULT = 30
_DAYS_MAX = 365
_SEARCH_LIMIT_DEFAULT = 12
_SEARCH_LIMIT_MAX = 20
_SITUATIONS_LIMIT_DEFAULT = 8
_SITUATIONS_LIMIT_MAX = 12
_ACTORS_MAX = 10
_SUMMARY_CLIP = 240
_QUERY_CLIP = 200
_STATS_GROUPS: dict[str, str] = {
    "category": "category",
    "importance": "importance",
    "intent": "socio_political_intent",
}
_NATIONS = ("cn", "kp", "ru", "ir")


@dataclass(frozen=True)
class ToolContext:
    """handler が使う read-only リソース束。"""

    repo: RunHistoryRepository
    embedder: EmbeddingClient | None
    db_path: Path


def _clamp_int(value: object, *, lo: int, hi: int, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


async def _search_articles(args: dict[str, Any], ctx: ToolContext) -> tuple[dict[str, Any], str]:
    from src.search.service import search as run_search

    query = str(args.get("query") or "").strip()[:_QUERY_CLIP]
    if not query:
        return {"error": "query が空"}, "記事検索: query なし (skip)"
    days = _clamp_int(args.get("days"), lo=1, hi=_DAYS_MAX, default=_DAYS_DEFAULT)
    limit = _clamp_int(args.get("limit"), lo=1, hi=_SEARCH_LIMIT_MAX, default=_SEARCH_LIMIT_DEFAULT)

    resp = await run_search(
        ctx.repo, query=query, mode="quick", embedder=ctx.embedder, llm=None, limit=limit * 2
    )
    cutoff = datetime.now(UTC) - timedelta(days=days)
    rows: list[dict[str, Any]] = []
    for h in resp.results:
        if h.created_at:
            try:
                if datetime.fromisoformat(h.created_at) < cutoff:
                    continue
            except ValueError:
                pass  # 日付が読めない行は落とさない (recall 優先)
        rows.append(
            {
                "title": h.title,
                "url": h.url,
                "importance": h.importance,
                "category": h.category,
                "intent": h.socio_political_intent,
                "created_at": h.created_at,
                "summary": (h.summary or "")[:_SUMMARY_CLIP],
            }
        )
        if len(rows) >= limit:
            break
    payload: dict[str, Any] = {"query": query, "window_days": days, "articles": rows}
    # 別名⇔正規名 bridge: query が既知アクター名 (別名込み) を含むなら等価情報を添付する。
    # これが無いと回答 LLM は「Cicada の言及なし」と誤答する (記事側は APT10 表記のため)。
    try:
        from src.cti.actor_normalizer import load_actor_aliases

        matched = load_actor_aliases().find_all(query)
        if matched:
            payload["actor_name_resolution"] = [
                {"canonical": m.canonical, "aliases": list(m.aliases[:6]), "nation": m.nation}
                for m in matched[:3]
            ]
    except Exception as e:  # noqa: BLE001 — 解決失敗でも検索結果自体は返す
        _log.warning("assistant_actor_resolution_failed", error=str(e))
    return payload, f"記事検索「{query}」({days}日) {len(rows)} 件"


async def _article_stats(args: dict[str, Any], ctx: ToolContext) -> tuple[dict[str, Any], str]:
    from src.storage.db_backend import connect

    group_by = str(args.get("group_by") or "category")
    col = _STATS_GROUPS.get(group_by)
    if col is None:
        return {"error": f"group_by は {sorted(_STATS_GROUPS)} のみ"}, "集計: group_by 不正 (skip)"
    days = _clamp_int(args.get("days"), lo=1, hi=_DAYS_MAX, default=_DAYS_DEFAULT)

    conn = connect(ctx.db_path)
    try:
        rows = conn.execute(
            f"SELECT COALESCE({col}, '(none)') AS g, COUNT(*) AS n"  # noqa: S608 — col は whitelist
            " FROM articles WHERE status = 'posted'"
            " AND created_at > datetime('now', ?)"
            " GROUP BY 1 ORDER BY 2 DESC",
            (f"-{days} days",),
        ).fetchall()
    finally:
        conn.close()
    buckets: list[dict[str, Any]] = [{"value": str(r[0]), "count": int(r[1])} for r in rows]
    total = sum(int(r[1]) for r in rows)
    return (
        {"group_by": group_by, "window_days": days, "total": total, "buckets": buckets},
        f"件数集計 ({group_by}, {days}日) 計 {total} 件",
    )


async def _list_situations(args: dict[str, Any], ctx: ToolContext) -> tuple[dict[str, Any], str]:
    from src.assessment.situation_store import SituationStore

    query = str(args.get("query") or "").strip()[:_QUERY_CLIP]
    limit = _clamp_int(
        args.get("limit"), lo=1, hi=_SITUATIONS_LIMIT_MAX, default=_SITUATIONS_LIMIT_DEFAULT
    )
    store = SituationStore(db_path=ctx.db_path)
    rows = store.load_situations(("active", "dormant"))
    tokens = [t for t in query.casefold().split() if t]
    if tokens:
        rows = [r for r in rows if all(t in r.title.casefold() for t in tokens)]
    rows = sorted(rows, key=lambda r: r.last_evidence_at, reverse=True)[:limit]

    items: list[dict[str, Any]] = []
    for r in rows:
        rev = store.latest_revision(r.situation_id)
        items.append(
            {
                "title": r.title,
                "status": r.status,
                "kind": r.kind,
                "last_evidence_at": r.last_evidence_at,
                "claim": rev.claim if rev else "",
                "confidence": rev.confidence if rev else "",
                "leading_hypothesis": rev.leading_hypothesis if rev else "",
                "last_delta": rev.delta_type if rev else "",
                "assessed_at": rev.created_at if rev else "",
            }
        )
    label = f"状況台帳{f'「{query}」' if query else ''} {len(items)} 件"
    return {"query": query, "situations": items}, label


async def _actor_activity(args: dict[str, Any], ctx: ToolContext) -> tuple[dict[str, Any], str]:
    from src.ui.services.threat_operations import fetch_threat_operations_snapshot

    days = _clamp_int(args.get("days"), lo=1, hi=_DAYS_MAX, default=_DAYS_DEFAULT)
    nation = str(args.get("nation") or "").strip().lower()
    if nation not in _NATIONS:
        nation = ""
    snap = fetch_threat_operations_snapshot(
        lookback_days=days, nation_filter=nation or None, db_path=ctx.db_path
    )
    actors = sorted(snap.actors, key=lambda a: a.total_articles, reverse=True)[:_ACTORS_MAX]
    items = [
        {
            "actor": a.canonical,
            "nation": a.nation,
            "articles": a.total_articles,
            "top_intents": list(a.top_intents[:3]),
            "top_sectors": list(a.top_sectors[:3]),
            "japan_targeted": a.japan_targeted_count,
            "spike": a.is_spike,
            "new": a.is_new,
        }
        for a in actors
    ]
    label = f"アクター活動 ({days}日{f', {nation}' if nation else ''}) {len(items)} 件"
    return {"window_days": days, "nation": nation, "actors": items}, label


_PROFILE_DAYS_DEFAULT = 90
_PROFILE_ARTICLES_MAX = 5
_PROFILE_LIST_CLIP = 8
_PROFILE_SUMMARY_CLIP = 600


async def _actor_profile(args: dict[str, Any], ctx: ToolContext) -> tuple[dict[str, Any], str]:
    """特定アクター 1 件の辞書情報 (knowledge) + 活動状況 (observation) + 関連記事。

    name は canonical でも別名 (Cicada / Stone Panda 等) でもよい — 辞書で解決してから
    引く。文字列一致の記事検索では拾えない別名質問への正攻法 (辞書が bridge)。
    """
    from src.cti.actor_normalizer import load_actor_aliases
    from src.ui.services.threat_operations import DEFAULT_DB_PATH, fetch_actor_detail

    name = str(args.get("name") or "").strip()[:_QUERY_CLIP]
    if not name:
        return {"error": "name が空"}, "アクター照会: name なし (skip)"
    days = _clamp_int(args.get("days"), lo=1, hi=_DAYS_MAX, default=_PROFILE_DAYS_DEFAULT)

    meta = load_actor_aliases().find(name)
    if meta is None:
        return (
            {"name": name, "found": False, "note": "アクター辞書に未収載 (別名でも一致せず)"},
            f"アクター照会「{name}」辞書未収載",
        )

    knowledge: dict[str, Any] = {
        "canonical": meta.canonical,
        "aliases": list(meta.aliases),
        "nation": meta.nation,
        "mitre_group": meta.mitre_group,
        "kind": meta.kind,
        "family": meta.family,
        "summary": (meta.summary or meta.description)[:_PROFILE_SUMMARY_CLIP],
        "motivation": meta.motivation,
        "first_seen": meta.first_seen,
        "target_sectors": list(meta.target_sectors[:_PROFILE_LIST_CLIP]),
        "associated_malware": list(meta.associated_malware[:_PROFILE_LIST_CLIP]),
    }

    activity: dict[str, Any] = {}
    articles: list[dict[str, Any]] = []
    detail = fetch_actor_detail(meta.id, lookback_days=days, db_path=ctx.db_path)
    if detail is not None:
        a = detail.activity
        activity = {
            "window_days": days,
            "articles": a.total_articles,
            "last_seen": a.last_seen_iso,
            "top_intents": [list(t) for t in a.top_intents[:3]],
            "top_sectors": [list(t) for t in a.top_sectors[:3]],
            "top_countries": [list(t) for t in a.top_countries[:3]],
            "japan_targeted": a.japan_targeted_count,
            "spike": a.is_spike,
        }
        articles = [
            {
                "title": r.title,
                "url": r.url,
                "importance": r.importance,
                "created_at": r.created_at,
            }
            for r in detail.recent_articles[:_PROFILE_ARTICLES_MAX]
        ]

    threat: dict[str, Any] | None = None
    try:
        from src.ui.services.actor_threat import fetch_mission_threat_assessments

        assessment = fetch_mission_threat_assessments(
            db_path=ctx.db_path,
            # 評価 cache は db_path を key に持たないため、既定 DB 以外 (tests) では使わない
            use_cache=ctx.db_path == DEFAULT_DB_PATH,
        ).get(meta.id)
        if assessment is not None:
            threat = {
                "tier": assessment.tier,
                "tier_rule": assessment.tier_rule,
                "activity_state": assessment.activity_state,
            }
    except Exception as e:  # noqa: BLE001 — 評価障害でも辞書+活動は返す
        _log.warning("assistant_actor_threat_failed", error=str(e))

    payload = {
        "name": name,
        "found": True,
        "knowledge": knowledge,
        "activity": activity,
        "recent_articles": articles,
        "mission_threat": threat,
    }
    n = activity.get("articles", 0)
    return payload, f"アクター照会「{name}」→ {meta.canonical} ({days}日 {n} 件)"


ToolHandler = Callable[[dict[str, Any], ToolContext], Awaitable[tuple[dict[str, Any], str]]]

TOOL_HANDLERS: dict[str, ToolHandler] = {
    "search_articles": _search_articles,
    "article_stats": _article_stats,
    "list_situations": _list_situations,
    "actor_activity": _actor_activity,
    "actor_profile": _actor_profile,
}


async def execute_tool(
    name: str, args: dict[str, Any], ctx: ToolContext
) -> tuple[dict[str, Any], str]:
    """ツール 1 件を実行する。未知ツール / 実行時エラーは payload の error で返す (落とさない)。"""
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return {"error": f"未知のツール: {name}"}, f"未知のツール {name} (skip)"
    try:
        result: tuple[dict[str, Any], str] = await handler(args, ctx)
    except Exception as e:  # noqa: BLE001 — 1 ツールの失敗で turn 全体を落とさない
        return {"error": f"{type(e).__name__}: {e}"}, f"{name} 実行エラー"
    return result
