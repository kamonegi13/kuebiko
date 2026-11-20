"""出力上限に当たった生成物の見張り (2026-11-20、11-19 監査 D1 P0-2c)。

上限に当たった構造化出力は ``llm_client`` の救済で括弧を閉じられ、後ろの欄が空のまま検証を
通る (週次の状況総括は 9 週続けて後ろの節が空のまま投稿された)。``llm_usage.hit_token_cap`` は
既に記録されているが、誰も見ていなかった。読み手に届く物語系の生成物について、直近 24 時間の
到達数を日次ハートビートの 1 行に出す。

⚠ の線 (事前登録・結果を見る前に決めた値): 状況総括 (``synthesis_narrative``) が 1 回でも、
またはそれ以外の物語系で到達率が ``RATE_WARN`` 以上 (かつ 2 回以上)。直近 7 日の実測は
状況総括 1/17・事象ニュース 5/841・日次の要点 2/114・深掘り 2/4・Spotlight 1/280。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository

_log = get_logger(__name__)

WINDOW_HOURS = 24
RATE_WARN = 0.05
MIN_HITS_FOR_RATE = 2
#: 読み手に届く物語系の step (表示名)。要約・翻訳は記事ごとの関門が別にあるので外す
PRODUCT_STEPS: dict[str, str] = {
    "synthesis_narrative": "状況総括",
    "event_news": "事象ニュース",
    "pir_daily_focus": "日次の要点",
    "pir_spotlight": "Spotlight",
    "digest_deep_dive": "深掘り",
}
ALWAYS_WARN_STEPS = frozenset({"synthesis_narrative"})


def _is_warning(step: str, calls: int, hits: int) -> bool:
    if hits == 0:
        return False
    if step in ALWAYS_WARN_STEPS:
        return True
    return hits >= MIN_HITS_FOR_RATE and hits / max(calls, 1) >= RATE_WARN


def build_token_cap_line(repo: RunHistoryRepository, *, now: datetime | None = None) -> str | None:
    """ハートビートの 1 行。失敗は None (ハートビート本体は止めない)。"""
    try:
        at = now or datetime.now(UTC)
        since = (at - timedelta(hours=WINDOW_HOURS)).isoformat()
        steps = tuple(PRODUCT_STEPS)
        marks = ", ".join("?" for _ in steps)
        with repo._connect() as conn:  # noqa: SLF001 — 読み取りのみ
            rows = conn.execute(
                "SELECT step, COUNT(*) AS n, SUM(COALESCE(hit_token_cap, 0)) AS cap"  # noqa: S608
                f" FROM llm_usage WHERE ts >= ? AND step IN ({marks}) GROUP BY step",
                (since, *steps),
            ).fetchall()
        stats = {str(r["step"]): (int(r["n"]), int(r["cap"] or 0)) for r in rows}
        hit = [(s, n, c) for s, (n, c) in stats.items() if c > 0]
        if not hit:
            return f"出力上限の到達 ({WINDOW_HOURS}h): なし"
        warn = any(_is_warning(s, n, c) for s, n, c in hit)
        frags = [f"{PRODUCT_STEPS[s]} {c}/{n}" for s, n, c in sorted(hit)]
        if warn:
            _log.warning("token_cap_products", hits={s: c for s, _n, c in hit})
        return f"{'⚠️' if warn else ''}出力上限の到達 ({WINDOW_HOURS}h): " + "、".join(frags)
    except Exception as e:  # noqa: BLE001 — heartbeat 本体を止めない
        _log.warning("token_cap_line_failed", error_type=type(e).__name__)
        return None


__all__ = ["PRODUCT_STEPS", "RATE_WARN", "build_token_cap_line"]
