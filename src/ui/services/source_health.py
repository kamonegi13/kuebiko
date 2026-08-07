"""feed 死活検知 + 日次 heartbeat の内容生成 (監査 2026-07-05 P2)。

「エラーにならない失敗」(feed の恒常無産出) は run の成否からは見えない —
CISA KEV advisories が 6 週間無音でも全 run が succeeded だった実測病理への対処:

1. per-feed fetch 結果の永続化 (``source_fetch_health``、main.py が run 毎に upsert)
2. enabled なのに N 日間 1 件も産出していない feed の検出 (本モジュール、決定論)
3. 毎日必ず届く 1 通の heartbeat — 「届かないこと自体が異常」の規約を作る
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository, SourceFetchHealth

_log = get_logger(__name__)

# enabled feed がこの日数 1 件も産出しなければ「沈黙」とみなす
SILENT_FEED_THRESHOLD_DAYS = 14
# 毎時 run で丸 1 日連続 fetch 失敗 = fetch 異常 (無産出との区別)
_FETCH_BROKEN_CONSECUTIVE = 24


@dataclass(frozen=True)
class SilentFeed:
    """沈黙している enabled feed 1 件。"""

    name: str
    url: str
    # fetch_error=取得自体が失敗 / no_entries=取得成功だが entry 0 件 (parse 異常疑い) /
    # no_yield=entry はあるが記事が入っていない (ソース側静穏 or 選別で落ちている)
    kind: str
    last_article_at: datetime | None
    consecutive_failures: int


def detect_silent_feeds(
    enabled_feeds: list[tuple[str, str]],
    latest_article_by_url: dict[str, datetime],
    health_by_url: dict[str, SourceFetchHealth],
    *,
    now: datetime,
    threshold_days: int = SILENT_FEED_THRESHOLD_DAYS,
) -> list[SilentFeed]:
    """enabled なのに直近 N 日産出ゼロの feed を決定論検出する (name 昇順)。"""
    cutoff = now - timedelta(days=threshold_days)
    out: list[SilentFeed] = []
    for name, url in enabled_feeds:
        latest = latest_article_by_url.get(url)
        if latest is not None and latest >= cutoff:
            continue
        health = health_by_url.get(url)
        failures = health.consecutive_failures if health else 0
        if failures >= _FETCH_BROKEN_CONSECUTIVE:
            kind = "fetch_error"
        elif health is not None and health.last_ok_at and health.last_article_count == 0:
            kind = "no_entries"
        else:
            kind = "no_yield"
        out.append(
            SilentFeed(
                name=name,
                url=url,
                kind=kind,
                last_article_at=latest,
                consecutive_failures=failures,
            )
        )
    return sorted(out, key=lambda s: s.name)


_MAX_NAMES_IN_HEARTBEAT = 8


def build_heartbeat_text(
    *,
    run_counts: dict[str, int],
    silent: list[SilentFeed],
    feeds_total: int,
    fill_line: str | None = None,
    standing_line: str | None = None,
    products_line: str | None = None,
    proposals_line: str | None = None,
) -> tuple[str, str, str]:
    """heartbeat の (title, body, importance) を組み立てる (純粋関数)。

    fill_line = 抽出 fill-rate 番兵 (供給網ヘルス、有機的結合監査 R1)。
    standing_line = standing 常設情報要求のサマリ (収穫数 / 最終評価日)。
    products_line = 製品鮮度 dead-man (週次総括/recap/spotlight/月次、監査 2026-07-16:
    weekly 9 日間欠落を誰も検知できなかった穴の閉鎖)。
    proposals_line = アクター提案の滞留 (人承認キューは pull 専用のため注意誘導が必要)。
    """
    ok = run_counts.get("succeeded", 0)
    partial = run_counts.get("partial_failure", 0)
    failed = run_counts.get("failed", 0)
    parts = [
        f"24h run: ✅{ok} ⚠️{partial} ❌{failed}",
        f"RSS feeds: 沈黙 {len(silent)}/{feeds_total}",
    ]
    if fill_line:
        parts.append(fill_line)
    if standing_line:
        parts.append(standing_line)
    if products_line:
        parts.append(products_line)
    if proposals_line:
        parts.append(proposals_line)
    if silent:
        names = ", ".join(s.name for s in silent[:_MAX_NAMES_IN_HEARTBEAT])
        if len(silent) > _MAX_NAMES_IN_HEARTBEAT:
            names += f" …他 {len(silent) - _MAX_NAMES_IN_HEARTBEAT} 件"
        parts.append(f"⚠️ {SILENT_FEED_THRESHOLD_DAYS}日以上無産出: {names}")
    has_line_warn = any(line and "⚠️" in line for line in (fill_line, products_line, proposals_line))
    importance = "medium" if (silent or failed or has_line_warn) else "low"
    return ("💓 daily heartbeat", " · ".join(parts), importance)


# 製品鮮度 dead-man の閾値 (日)。週次系はスケジュール間隔 7 日 + 猶予 2 日。
_PRODUCT_FRESHNESS_LIMITS: tuple[tuple[str, str, str, int], ...] = (
    ("週次総括", "status_synthesis", "WHERE period_type='weekly'", 9),
    ("recap", "weekly_recaps", "", 9),
    ("spotlight", "pir_spotlight", "", 9),
    ("月次総括", "status_synthesis", "WHERE period_type='monthly'", 35),
)


def _build_product_freshness_line(repo: RunHistoryRepository) -> str | None:
    """製品鮮度 dead-man 1 行 (監査 2026-07-16: weekly 9 日間欠落の無検知への恒久対処)。

    定期製品 (週次総括 / recap / spotlight / 月次総括) の最終生成からの経過日数を表示し、
    閾値超に ⚠️ を付ける (importance 昇格は build_heartbeat_text 側)。失敗は None。
    """
    try:
        now = datetime.now(UTC)
        frags: list[str] = []
        with repo._connect() as conn:  # noqa: SLF001 — 読み取り専用
            for label, table, where, limit_days in _PRODUCT_FRESHNESS_LIMITS:
                row = conn.execute(
                    f"SELECT MAX(generated_at) FROM {table} {where}"  # noqa: S608 — 定数表
                ).fetchone()
                latest = row[0] if row else None
                if not latest:
                    frags.append(f"⚠️{label} 未生成")
                    continue
                age_days = (now - datetime.fromisoformat(str(latest)).astimezone(UTC)).days
                mark = "⚠️" if age_days > limit_days else ""
                frags.append(f"{mark}{label} {age_days}d")
        return "製品鮮度: " + " / ".join(frags)
    except Exception as e:  # noqa: BLE001 — heartbeat 本体を止めない
        _log.warning("product_freshness_line_failed", error=str(e))
        return None


# アクター提案滞留の WARN 閾値 (件数 / 最古日数)
_PROPOSALS_WARN_COUNT = 10
_PROPOSALS_WARN_AGE_DAYS = 14


def _build_proposals_line(repo: RunHistoryRepository) -> str | None:
    """アクター提案 (人承認キュー) の滞留 1 行 (pending 0 件なら None)。

    確定帰属は人承認のみ (自動化しない) — キューの成長を注意経路に載せるのが対処
    (監査 2026-07-16: pending 33 件が 23 日間無決裁で堆積していた)。
    """
    try:
        with repo._connect() as conn:  # noqa: SLF001 — 読み取り専用
            row = conn.execute(
                "SELECT COUNT(*), MIN(created_at) FROM actor_update_proposals"
                " WHERE status='pending'"
            ).fetchone()
        pending = int(row[0] or 0)
        if pending == 0:
            return None
        oldest_days = 0
        if row[1]:
            oldest = datetime.fromisoformat(str(row[1])).astimezone(UTC)
            oldest_days = (datetime.now(UTC) - oldest).days
        warn = pending >= _PROPOSALS_WARN_COUNT or oldest_days >= _PROPOSALS_WARN_AGE_DAYS
        mark = "⚠️" if warn else ""
        return f"{mark}アクター提案 pending {pending} 件 (最古 {oldest_days}d、要レビュー)"
    except Exception as e:  # noqa: BLE001 — heartbeat 本体を止めない
        _log.warning("proposals_line_failed", error=str(e))
        return None


def _build_standing_line() -> str | None:
    """standing 常設のサマリ 1 行 (flag off / 未開設 / 失敗は None)。"""
    try:
        from src.ui.services.standing_posture import build_standing_posture

        cards = build_standing_posture()
        if not cards:
            return None
        evidence = sum(int(c.get("evidence_related_30d") or 0) for c in cards)
        assessed = [str(c.get("assessed_at") or "") for c in cards if c.get("assessed_at")]
        last = max(assessed)[:10] if assessed else "未評価"
        return f"standing: {len(cards)}件 証拠30d {evidence} / 最終評価 {last}"
    except Exception as e:  # noqa: BLE001 — heartbeat 本体を壊さない
        _log.warning("heartbeat_standing_line_failed", error=str(e)[:80])
        return None


def _enabled_rss_feeds() -> list[tuple[str, str]]:
    """enabled な RSS feed の (name, url) 一覧 (SSoT=DB config)。"""
    from src.sources import source_store

    entries = source_store.load_entries("rss")
    return [
        (str(e.get("name") or e.get("url") or ""), str(e.get("url") or ""))
        for e in entries
        if e.get("enabled", True) and e.get("url")
    ]


async def run_daily_heartbeat() -> None:
    """日次 heartbeat: run サマリ + 沈黙 feed を ops チャンネルに 1 通投稿する。

    失敗しても例外は上げない (scheduler job を殺さない)。
    """
    try:
        repo = RunHistoryRepository()
        run_counts = repo.count_runs_by_status_since(hours=24)
        feeds = _enabled_rss_feeds()
        silent = detect_silent_feeds(
            feeds,
            repo.latest_article_time_by_feed_url(),
            {h.source_key: h for h in repo.list_source_fetch_health()},
            now=datetime.now(UTC),
        )
        from src.ui.services.fill_rate_audit import build_heartbeat_fill_line

        title, body, importance = build_heartbeat_text(
            run_counts=run_counts,
            silent=silent,
            feeds_total=len(feeds),
            fill_line=build_heartbeat_fill_line(),
            standing_line=_build_standing_line(),
            products_line=_build_product_freshness_line(repo),
            proposals_line=_build_proposals_line(repo),
        )
        from src.ui.services.ops_notify import post_ops_message

        sent = await post_ops_message(title=title, body=body, importance=importance)
        _log.info(
            "daily_heartbeat",
            sent=sent,
            silent_feeds=len(silent),
            run_counts=run_counts,
        )
    except Exception as e:  # noqa: BLE001 — heartbeat 自体の失敗で scheduler を汚さない
        _log.error("daily_heartbeat_failed", error=str(e))
