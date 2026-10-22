"""本日の高脅威 Recall 安全網 (2026-07-16)。

分類と配信の断絶への恒久対処。triage は importance=high を「即時通知」と定義するが、
通知再設計 (2026-06-28) 以降、KEV/0day/apt_leak/japan-critical **以外** の high サイバー
脅威 (日 ~10 件) は routing で web-only (watch/japan_watch、push=false) に回り、Discord に
届かない。朝夕ブリーフのダイジェストは synthesis 上位 6 + PIR focus 上位 3/PIR の
**salience 競争**であり、これらを確実には拾わない (実測 14 日で 138 件中 28 件は PIR
マッチすら無し) — ツール自身が「即対応級」と判定した脅威が沈黙していた。

本モジュールは「要点射影」が約束していた安全網を完成させる: ブリーフに**決定論の
高脅威リスト** (期間内の high サイバー脅威で alert 未 push のもの全件) を 1 セクション
追加し、high 判定が必ず担当者の日次通読に載ることを保証する (Recall 完全性)。
LLM 追加呼出なし。個別 push は復活させない (再設計の 1 通ダイジェスト方針は維持)。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.cti.japan_relevance import is_japan_targeted_row
from src.cti.source_basis import classify_source_tier
from src.logging_config import get_logger
from src.storage.run_history import DEFAULT_DB_PATH

_log = get_logger(__name__)

# 高脅威リストに含める category = 実 actionable なサイバー脅威種別。
# geopolitical/policy/research は「状況認識」であり即対応脅威ではない (web-only 継続が正)。
# source_quality.high_threat_brief_categories と同一の設計思想 (routing R2/R3.5 と整合)。
_ACTIONABLE_CATEGORIES: tuple[str, ...] = (
    "apt",
    "malware",
    "incident",
    "advisory",
    "vulnerability",
    "breach",
)
# Discord 1 通に載せる上限 (超過は "+N 件 → web")。日 ~10 件なので通常は全件載る。
DISCORD_MAX_ITEMS = 12
_MAX_ITEMS = DISCORD_MAX_ITEMS
#: Web の日次ブリーフに載せる上限 (2026-10-02)。Discord の "+N 件 → web" の先
WEB_MAX_ITEMS = 50
_TITLE_MAX = 90


@dataclass(frozen=True)
class HighThreatItem:
    """高脅威 1 件 (Discord リスト行)。"""

    article_id: str
    title: str
    category: str
    url: str
    is_japan: bool
    #: 出典の信頼度ティア (classify_source_tier)。SNS 由来の目印用。空 = 不明
    source_tier: str = ""


def _connect(db_path: Path) -> Any:
    from src.storage.db_backend import connect as backend_connect

    con = backend_connect(db_path)
    if hasattr(con, "row_factory"):
        import sqlite3

        con.row_factory = sqlite3.Row
    return con


def collect_high_threats(
    *,
    lookback_hours: int,
    db_path: Path = DEFAULT_DB_PATH,
    now: datetime | None = None,
    limit: int = _MAX_ITEMS,
) -> tuple[list[HighThreatItem], int]:
    """期間内の high サイバー脅威で **alert 未 push** のものを返す (items, total)。

    - alert (即応 push 済み) / vuln (M3b 段1、2026-10-10、現時点は投稿先として未使用) は
      除外 — 本リストは「どこにも速報として push されなかった high」の安全網。
    - dedup_key で重複排除 (最新 1 件)。japan 標的を先頭、以降 created_at 降順。
    - total = 上限適用前の件数 ("+N 件" 表示用)。
    """
    since = (now or datetime.now(UTC)) - timedelta(hours=lookback_hours)
    placeholders = ", ".join("?" for _ in _ACTIONABLE_CATEGORIES)
    sql = (
        "SELECT article_id, title, url, category, victim_country_iso, posted_channel,"
        " dedup_key, created_at, feed_title, feed_url, account_class"
        " FROM articles"
        " WHERE status='posted' AND importance='high'"
        " AND posted_channel NOT IN ('alert', 'vuln')"
        f" AND category IN ({placeholders})"
        " AND created_at >= ?"
        " ORDER BY created_at DESC"
    )
    params: list[object] = [*_ACTIONABLE_CATEGORIES, since.isoformat()]
    con = _connect(db_path)
    try:
        rows = con.execute(sql, params).fetchall()
    finally:
        con.close()

    seen_keys: set[str] = set()
    items: list[HighThreatItem] = []
    for r in rows:
        key = str(r["dedup_key"] or r["article_id"])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        items.append(
            HighThreatItem(
                article_id=str(r["article_id"]),
                title=str(r["title"] or "").strip(),
                category=str(r["category"] or ""),
                url=str(r["url"] or ""),
                is_japan=is_japan_targeted_row(r["victim_country_iso"], r["posted_channel"]),
                source_tier=classify_source_tier(
                    str(r["feed_title"] or ""),
                    str(r["feed_url"] or ""),
                    account_class=str(r["account_class"] or ""),
                ),
            )
        )
    # japan 標的を先頭に安定ソート (以降は created_at 降順を維持)。
    items.sort(key=lambda it: 0 if it.is_japan else 1)
    total = len(items)
    return items[:limit], total


_CATEGORY_JA: dict[str, str] = {
    "apt": "APT",
    "malware": "マルウェア",
    "incident": "インシデント",
    "advisory": "アドバイザリ",
    "vulnerability": "脆弱性",
    "breach": "漏洩",
}


#: 事象の統合 (merged_into) をたどる最大段数 (実測の最大は 2-3 段)
_MERGE_HOPS = 6
#: 別チャンネルへ流れた件数を数える投稿先 (薄い日の補足行用)
OTHER_CHANNELS: tuple[str, ...] = ("alert", "japan_watch")


def _in_clause(values: Sequence[str]) -> str:
    return ", ".join("?" for _ in values)


def collect_event_info(
    article_ids: Sequence[str], *, db_path: Path = DEFAULT_DB_PATH
) -> dict[str, tuple[str, str]]:
    """記事 id → (事象 id, 事象の日本語見出し)。事象ニュースの群に入っていない記事は含めない。

    統合された事象 (merged_into) は根まで追う。同じ事象の記事を 1 行にまとめるための材料で、
    読めないときは空 (呼び出し側は従来どおり記事ごとに出す)。
    """
    ids = [a for a in dict.fromkeys(article_ids) if a]
    if not ids:
        return {}
    con = _connect(db_path)
    try:
        member_rows = con.execute(
            "SELECT article_id, item_id FROM event_item_members"
            f" WHERE article_id IN ({_in_clause(ids)}) ORDER BY joined_at ASC",
            ids,
        ).fetchall()
        item_of = {str(r["article_id"]): str(r["item_id"]) for r in member_rows}
        root_of = _resolve_roots(con, set(item_of.values()))
        roots = sorted(set(root_of.values()))
        headlines = _current_headlines(con, roots)
    finally:
        con.close()
    return {
        aid: (root_of[iid], headlines.get(root_of[iid], ""))
        for aid, iid in item_of.items()
        if iid in root_of
    }


def _resolve_roots(con: Any, item_ids: set[str]) -> dict[str, str]:
    """item id → 統合先の根 id (統合されていなければ自分自身)。"""
    root_of = {i: i for i in item_ids}
    for _ in range(_MERGE_HOPS):
        pending = sorted(set(root_of.values()))
        rows = con.execute(
            f"SELECT id, merged_into FROM event_items WHERE id IN ({_in_clause(pending)})",
            pending,
        ).fetchall()
        nxt = {str(r["id"]): str(r["merged_into"]) for r in rows if r["merged_into"]}
        if not nxt:
            break
        root_of = {k: nxt.get(v, v) for k, v in root_of.items()}
    return root_of


def _current_headlines(con: Any, item_ids: Sequence[str]) -> dict[str, str]:
    if not item_ids:
        return {}
    rows = con.execute(
        "SELECT i.id AS item_id, v.headline AS headline FROM event_items i"
        " JOIN event_item_versions v ON v.item_id = i.id AND v.version = i.current_version"
        f" WHERE i.id IN ({_in_clause(list(item_ids))})",
        list(item_ids),
    ).fetchall()
    return {str(r["item_id"]): str(r["headline"] or "").strip() for r in rows}


def collect_other_channel_counts(
    *,
    lookback_hours: int,
    db_path: Path = DEFAULT_DB_PATH,
    now: datetime | None = None,
) -> dict[str, int]:
    """同じ時間窓で別チャンネル (alert / japan_watch) へ配信した件数 (dedup_key 単位)。

    高脅威の節は alert / vuln を設計上除くため、薄い回には「他へ流れた」ことが見えない。
    その補足行の材料。
    """
    end = now or datetime.now(UTC)
    since = end - timedelta(hours=lookback_hours)
    sql = (
        "SELECT posted_channel, COUNT(DISTINCT COALESCE(dedup_key, article_id)) AS n"
        " FROM articles WHERE status = 'posted'"
        f" AND posted_channel IN ({_in_clause(OTHER_CHANNELS)})"
        " AND created_at >= ? AND created_at <= ?"
        " GROUP BY posted_channel"
    )
    con = _connect(db_path)
    try:
        rows = con.execute(sql, [*OTHER_CHANNELS, since.isoformat(), end.isoformat()]).fetchall()
    finally:
        con.close()
    found = {str(r["posted_channel"]): int(r["n"]) for r in rows}
    return {ch: found.get(ch, 0) for ch in OTHER_CHANNELS}


def high_threats_payload(
    items: list[HighThreatItem],
    *,
    total: int,
    event_info: Mapping[str, tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Web の日次ブリーフに載せる形 (daily_briefs.payload["high_threats"]、2026-10-02)。

    従来は Discord の要点にだけ出ていた。alert に流れなかった high の脅威は、ここが
    唯一の通読面なので Web にも残す。event_info (``collect_event_info``) があれば
    事象 id と日本語見出しを付ける (Discord が同じ事象を 1 行にまとめる材料、2026-10-22)。
    """
    info = event_info or {}
    return {
        "total": total,
        "items": [
            {
                "article_id": it.article_id,
                "title": it.title,
                "category": it.category,
                "category_label": _CATEGORY_JA.get(it.category, it.category),
                "url": it.url,
                "is_japan": it.is_japan,
                "source_tier": it.source_tier,
                "event_id": info.get(it.article_id, ("", ""))[0],
                "event_headline": info.get(it.article_id, ("", ""))[1],
            }
            for it in items
        ],
    }


def format_high_threat_compact(
    items: list[HighThreatItem], *, total: int, base_url: str | None
) -> str:
    """Discord 用の高脅威リスト射影 (決定論、LLM 追加呼出なし)。空なら ""。"""
    if not items:
        return ""
    lines = [f"🔴 **本日の高脅威 (要確認 · {total} 件)**"]
    for it in items:
        cat = _CATEGORY_JA.get(it.category, it.category)
        title = it.title if len(it.title) <= _TITLE_MAX else it.title[: _TITLE_MAX - 1] + "…"
        flag = "🇯🇵 " if it.is_japan else ""
        line = f"・{flag}{title} ({cat})"
        if it.url:
            line += f" <{it.url}>"
        lines.append(line)
    remaining = total - len(items)
    if remaining > 0:
        suffix = f"…他 {remaining} 件"
        if base_url:
            suffix += f" → {base_url}/app/news?importance=high"
        lines.append(suffix)
    return "\n".join(lines)
