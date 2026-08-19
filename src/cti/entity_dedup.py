"""victim_org × 日付 の同一被害組織照合 (Step 3, 2026-08-19)。

2 つの用途を持つ:

1. ``build_org_date_index`` / ``is_covered_within_window`` — ``±window_days`` の緩い
   窓での org × 日付 index 照合。``src/sources/ransomware_ingest.py`` の
   ``_build_news_index`` / ``_covered_by_news`` (ニュースが同一被害組織を扱っていれば
   ransomware.live 側を重複扱いする、一方向チェック) を汎用化したもの。ransomware_ingest
   は従来どおりこのモジュールの実装を呼ぶだけで、挙動 (±60日窓) は変えない。
2. ``find_recent_victim_org_duplicate`` — 上記の逆方向。RSS/Grok 経路の投稿直前ゲート
   (``src/cti/identity_dedup.py``) から呼ばれ、「同一 victim_org が直近 (既定 24h) に
   posted 済みか」を DB に直接問い合わせる。ransomware_ingest 側の ±60日窓とは別物
   (24h 超の続報は正当な情報として通す — memory [[dedup_window_48h_design_intent]] と
   同じ「Delta を意図的に捨てる」思想)。

victim_org の表記ゆれは完全一致 (lower/trim) のみで吸収する。部分一致・fuzzy 判定は
しない — 誤爆で別組織の事案を潰す方が害が大きい (CLAUDE.md の運用判断)。
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from src.storage.run_history import ArticleRecord, RunHistoryRepository

# ---------- ±window_days 窓の index 照合 (ransomware_ingest 汎用化) ----------


def _coerce_datetime(value: object) -> datetime | None:
    """DB 値 (datetime or ISO 文字列) → naive datetime。パース不能/欠損は None。

    ``src.sources.ransomware_ingest._to_dt`` と同等の変換 (独立実装、Step 3 で
    汎用ヘルパを ransomware_ingest から切り離すために複製。ransomware_ingest 側は
    reconcile 等 別用途にも ``_to_dt``/``_parse_dt`` を使い続けるため、そちらは変更しない)。
    """
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return None
    return None


def build_org_date_index(rows: list[tuple[str, Any]]) -> dict[str, list[datetime]]:
    """(victim_org小文字, 日付) の行群を org→日付 list の index にする (日付不明は除外)。

    ``src.sources.ransomware_ingest._build_news_index`` から汎用化して移設。
    """
    idx: dict[str, list[datetime]] = {}
    for org, d in rows:
        dt = _coerce_datetime(d)
        if dt is not None:
            idx.setdefault(org, []).append(dt)
    return idx


def is_covered_within_window(
    index: dict[str, list[datetime]],
    org: str,
    when: datetime | None,
    *,
    window_days: int,
) -> bool:
    """``org`` が index 内に ``when`` から ±``window_days`` 日以内で存在するか。

    ``when`` が None (日付不明) の場合は org 一致のみで保守的に True を返す
    (``src.sources.ransomware_ingest._covered_by_news`` と同じ仕様)。
    """
    dates = index.get(org.strip().lower())
    if not dates:
        return False
    if when is None:
        return True
    return any(abs((d - when).days) <= window_days for d in dates)


# ---------- 直近 within_hours の直接 DB 照合 (Step 3 投稿直前ゲート用) ----------

_VICTIM_ORG_DEDUP_ENV = "VICTIM_ORG_DEDUP"


def victim_org_dedup_enabled() -> bool:
    """Step 3 victim_org dedup が有効か。既定 ON、``VICTIM_ORG_DEDUP=0`` で無効化。"""
    raw = os.environ.get(_VICTIM_ORG_DEDUP_ENV, "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def find_recent_victim_org_duplicate(
    repo: RunHistoryRepository,
    victim_orgs: Sequence[str],
    *,
    within_hours: int,
    exclude_article_id: str | None = None,
) -> tuple[str, ArticleRecord] | None:
    """``victim_orgs`` のいずれかが直近 ``within_hours`` 以内に posted 済みの記事と
    完全一致 (lower/trim) すれば ``(一致した org, 先行 ArticleRecord)`` を返す。

    複数 org を持つ記事は先頭から順に照合し、最初に一致した org で確定する
    (host 記事は通常 1 org のみのため実質順序に意味はない)。org が空/空白のみの
    要素は skip する。
    """
    for org in victim_orgs:
        normalized = (org or "").strip()
        if not normalized:
            continue
        prior = repo.find_recent_posted_article_by_victim_org(
            normalized,
            within_hours=within_hours,
            exclude_article_id=exclude_article_id,
        )
        if prior is not None:
            return normalized, prior
    return None


__all__ = [
    "build_org_date_index",
    "find_recent_victim_org_duplicate",
    "is_covered_within_window",
    "victim_org_dedup_enabled",
]
