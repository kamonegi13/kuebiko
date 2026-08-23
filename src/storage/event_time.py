"""事象時刻の錨 — 共有 SSoT (2026-08-23 に repo_synthesis から昇格)。

2026-08-22 の根本修正: **DB へ書いた時刻を事象時刻として使わない。**
article_entities.created_at は「entity 行を書いた時刻」であり、バックフィル
(再抽出 / 別名昇格 / intent・axes backfill) は過去記事へ当日の日付で書くため
事象時刻とは無関係になる (実測: 言及の 44.3% が 1 日超・34.4% が 7 日超ずれ、
週次 FC3 spike の 44% が偽陽性、日次バーストは単日最大 42 件の幻)。

昇格の理由 (2026-08-23、事象単位ニュース設計レビュー C3): 錨の規律を「踏襲」と
書くと複製が生まれ、複製は移植漏れを生む (ACH 2 経路の前科)。consumer は
repo_synthesis と src/eventnews の両方で、**錨式のリテラルはこのモジュールにのみ
存在する** (tests/unit/test_event_time_anchor.py が固定)。
"""

from __future__ import annotations

# 錨は「公開時刻。ただし取込より後にはならない (実測 0.5% が不正)。欠損は取込時刻」。
# 利用側は EVENT_TS_EXPR.format(a="<articles の別名>")。
EVENT_TS_EXPR = (
    "CASE WHEN {a}.published_at IS NOT NULL AND {a}.published_at <= {a}.created_at"
    " THEN {a}.published_at ELSE {a}.created_at END"
)

# articles は同一 article_id が複数行ありうる (実測 3,593 行 / 最大 7 行)。
# join 前に 1 行へ畳んで言及・媒体数の水増し (fan-out) を防ぐ。
DEDUP_ARTICLES = (
    "(SELECT article_id, MIN(created_at) AS created_at,"
    " MIN(published_at) AS published_at FROM articles GROUP BY article_id)"
)

__all__ = ["DEDUP_ARTICLES", "EVENT_TS_EXPR"]
