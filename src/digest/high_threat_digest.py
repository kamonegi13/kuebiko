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

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.cti.japan_relevance import is_japan_targeted_row
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
_MAX_ITEMS = 12
_TITLE_MAX = 90


@dataclass(frozen=True)
class HighThreatItem:
    """高脅威 1 件 (Discord リスト行)。"""

    article_id: str
    title: str
    category: str
    url: str
    is_japan: bool


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

    - alert (即応 push 済み) は除外 — 本リストは「push されなかった high」の安全網。
    - dedup_key で重複排除 (最新 1 件)。japan 標的を先頭、以降 created_at 降順。
    - total = 上限適用前の件数 ("+N 件" 表示用)。
    """
    since = (now or datetime.now(UTC)) - timedelta(hours=lookback_hours)
    placeholders = ", ".join("?" for _ in _ACTIONABLE_CATEGORIES)
    sql = (
        "SELECT article_id, title, url, category, victim_country_iso, posted_channel,"
        " dedup_key, created_at"
        " FROM articles"
        " WHERE status='posted' AND importance='high'"
        " AND posted_channel <> 'alert'"
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
