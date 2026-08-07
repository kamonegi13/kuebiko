#!/usr/bin/env python3
"""分析軸 (intent / technical / event_date) 暗黒期間の backfill (2026-07-13)。

summarizer 過負荷による末尾フィールド枯死 (intent 75%→2% / technical 16%→1% /
event_date 45%→5%) の恒久修復として、取込側を focused 分析軸分類器
(src/cti/analysis_axes_classifier.py) に切り替えた。本スクリプトは暗黒期間の posted
記事へ同じ分類器を適用し、取込側と単一レジームに揃える。backfill_intent.py の後継
(intent は intent_backfill_log 済み分を尊重して上書きしない)。

対象 (冪等 — 処理済みは axes_backfill_log が記憶):
  - 暗黒期間 (2026-06-29 以降) の posted 全件で、technical_axis_summary IS NULL
    OR event_date IS NULL OR intent_confidence IS NULL

書き込み規約 (ingest と同一の「空欄のみ埋める」additive):
  - intent: intent_confidence IS NULL の行のみ書く (backfill_intent 済みは保持)。
    unknown は書かない。
  - technical: 現在 NULL の行のみ書く。
  - event_date/basis/compromise: 現在 event_date IS NULL の行のみ書く。
    _normalize_temporal (報道日+1d 上限) を ingest と同一に適用。

運用ガード: backfill_intent.py と同一 (cron guard / 連続失敗アボート / dry-run)。

Usage (production PG、コンテナ内):
    docker cp scripts/backfill_axes.py kuebiko:/tmp/backfill_axes.py
    docker exec kuebiko python /tmp/backfill_axes.py --limit 20 --dry-run
    docker exec -d kuebiko sh -c "python /tmp/backfill_axes.py > /tmp/backfill_axes.log 2>&1"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO if (_REPO / "src").is_dir() else Path("/app")))

from src.config_loader import load_app_config  # noqa: E402
from src.cti.analysis_axes_classifier import AnalysisAxesOut, build_axes_prompt  # noqa: E402
from src.cti.diamond_model import parse_diamond_axes  # noqa: E402
from src.pipeline.summary import _normalize_temporal  # noqa: E402
from src.storage.db_backend import connect  # noqa: E402
from src.tools.model_tiers import Step, build_llm_for  # noqa: E402

# 暗黒期間の開始 = b8b4e7dd デプロイ日 (intent/technical の崩壊起点。event_date の
# 崩壊起点 7/04 も内包する)
DARK_START = "2026-06-29"
PROGRESS_EVERY = 25
MAX_CONSECUTIVE_ERRORS = 5

# cron guard: backfill_intent.py と同一
GUARD_MINUTE_FROM = 57
GUARD_MINUTE_TO = 16
QUIET_WINDOWS: tuple[tuple[str, str, str], ...] = (
    ("06:15", "08:05", "morning-brief"),
    ("19:25", "20:05", "evening-synthesis"),
)
GUARD_POLL_SECONDS = 60.0

_LOG_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS axes_backfill_log (
    article_pk BIGINT PRIMARY KEY,
    intent TEXT,
    confidence TEXT,
    technical_written SMALLINT NOT NULL DEFAULT 0,
    event_date TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def guard_reason(now: datetime) -> str | None:
    """LLM 呼び出しを止めるべき cron 窓なら理由ラベルを返す (通常運転は None)。"""
    if now.minute >= GUARD_MINUTE_FROM or now.minute <= GUARD_MINUTE_TO:
        return "hourly-rss"
    hhmm = now.strftime("%H:%M")
    for start, end, label in QUIET_WINDOWS:
        if start <= hhmm <= end:
            return label
    return None


# (id, title, category, body, summary, published_at, has_intent, has_technical, has_event)
_TargetRow = tuple[int, str, str | None, str | None, str | None, str | None, bool, bool, bool]


def _select_targets(conn: object, limit: int | None) -> list[_TargetRow]:
    """対象記事を新しい順に取得する (どの列が埋まっているかも同時に取る)。"""
    sql = """
        SELECT id, title, category, body, summary, published_at,
               (intent_confidence IS NOT NULL) AS has_intent,
               (technical_axis_summary IS NOT NULL) AS has_technical,
               (event_date IS NOT NULL) AS has_event
        FROM articles
        WHERE status = 'posted'
          AND created_at >= ?
          AND (
                intent_confidence IS NULL
                OR technical_axis_summary IS NULL
                OR event_date IS NULL
          )
          AND id NOT IN (SELECT article_pk FROM axes_backfill_log)
        ORDER BY created_at DESC
    """
    params: list[object] = [DARK_START]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, params).fetchall()  # type: ignore[attr-defined]
    return [
        (
            int(r[0]),
            str(r[1]),
            r[2],
            r[3],
            r[4],
            str(r[5]) if r[5] is not None else None,
            bool(r[6]),
            bool(r[7]),
            bool(r[8]),
        )
        for r in rows
    ]


def _published_date(published_at: str | None) -> date | None:
    """published_at (ISO TEXT) を date に安全に変換する。"""
    if not published_at:
        return None
    try:
        return datetime.fromisoformat(published_at.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def normalize_axes(
    out: AnalysisAxesOut, *, published: date | None
) -> tuple[str | None, str | None, str | None, str | None, str | None, str | None, str | None]:
    """LLM 出力を ingest と同一の防御パーサで正規化する。

    Returns: (intent, confidence, rationale, technical, event_date, basis, compromise)。
    intent=unknown は (None, None, None, ...) — articles には書かない。
    """
    axes = parse_diamond_axes(out.to_diamond_dict())
    sp = axes.socio_political
    intent = None if sp.intent == "unknown" else sp.intent
    confidence = None if intent is None else sp.confidence
    rationale = None if intent is None else (sp.rationale or None)
    technical = axes.technical or None
    ref = (published or datetime.now().date()) + timedelta(days=1)
    ev, basis, comp = _normalize_temporal(
        out.event_date, out.event_date_basis, out.compromise_date, reference=ref
    )
    return (intent, confidence, rationale, technical, ev, basis, comp)


async def _wait_for_clear_window(*, cron_guard: bool) -> None:
    """cron guard 窓の間は待機する (窓の外に出たら戻る)。"""
    if not cron_guard:
        return
    while (reason := guard_reason(datetime.now())) is not None:
        print(f"[guard] {reason} 窓のため待機中 ({datetime.now():%H:%M})", flush=True)
        await asyncio.sleep(GUARD_POLL_SECONDS)


async def run(limit: int | None, *, dry_run: bool, sleep_seconds: float, cron_guard: bool) -> int:
    """backfill 本体。処理件数を返す。"""
    config = load_app_config()
    llm = build_llm_for(Step.ARTICLE_SUMMARY, config)
    conn = connect()
    conn.execute(_LOG_TABLE_DDL)

    targets = _select_targets(conn, limit)
    print(f"対象: {len(targets)} 件 (dry_run={dry_run}, model={llm.model})", flush=True)

    stats: Counter[str] = Counter()
    consecutive_errors = 0
    for i, (pk, title, category, body, summary, published_at, has_i, has_t, has_e) in enumerate(
        targets, start=1
    ):
        await _wait_for_clear_window(cron_guard=cron_guard)
        pub = _published_date(published_at)
        prompt = build_axes_prompt(
            title, category, body, summary, pub.isoformat() if pub else None
        )
        if prompt is None:
            stats["no_text"] += 1
            if not dry_run:
                conn.execute(
                    "INSERT INTO axes_backfill_log (article_pk) VALUES (?)"
                    " ON CONFLICT (article_pk) DO NOTHING",
                    (pk,),
                )
            continue
        try:
            out = await llm.generate_structured(prompt, schema=AnalysisAxesOut, think=False)
            consecutive_errors = 0
        except Exception as exc:  # noqa: BLE001 (個別失敗は skip、連続失敗のみアボート)
            consecutive_errors += 1
            stats["error"] += 1
            print(f"[error] id={pk}: {type(exc).__name__}", flush=True)
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                print(
                    f"連続 {MAX_CONSECUTIVE_ERRORS} 回失敗 — Ollama 停止の疑い。アボート",
                    flush=True,
                )
                break
            continue

        intent, confidence, rationale, technical, ev, basis, comp = normalize_axes(
            out, published=pub
        )
        # 空欄のみ埋める (additive)。intent は backfill_intent 済み (has_i) を尊重。
        write_intent = intent is not None and not has_i
        write_technical = technical is not None and not has_t
        write_event = ev is not None and not has_e
        stats[intent or "unknown"] += 1
        if write_technical:
            stats["technical"] += 1
        if write_event:
            stats["event_date"] += 1

        if dry_run:
            print(
                f"[dry] id={pk} cat={category} -> intent={intent}({confidence})"
                f" tech={'y' if technical else '-'} ev={ev or '-'}({basis or '-'})",
                flush=True,
            )
        else:
            if write_intent:
                conn.execute(
                    "UPDATE articles SET socio_political_intent = ?, intent_confidence = ?,"
                    " socio_political_rationale = ? WHERE id = ?",
                    (intent, confidence, rationale, pk),
                )
            if write_technical:
                conn.execute(
                    "UPDATE articles SET technical_axis_summary = ? WHERE id = ?",
                    (technical, pk),
                )
            if write_event:
                conn.execute(
                    "UPDATE articles SET event_date = ?, event_date_basis = ?,"
                    " compromise_date = ? WHERE id = ?",
                    (ev, basis, comp, pk),
                )
            conn.execute(
                "INSERT INTO axes_backfill_log"
                " (article_pk, intent, confidence, technical_written, event_date)"
                " VALUES (?, ?, ?, ?, ?) ON CONFLICT (article_pk) DO NOTHING",
                (pk, intent, confidence, 1 if write_technical else 0, ev),
            )
        if i % PROGRESS_EVERY == 0:
            print(f"[progress] {i}/{len(targets)} {dict(stats)}", flush=True)
        if sleep_seconds > 0:
            await asyncio.sleep(sleep_seconds)

    processed = sum(stats.values())
    print(f"完了: {processed} 件処理 / 内訳 {dict(stats)}", flush=True)
    return processed


def main() -> None:
    parser = argparse.ArgumentParser(description="分析軸暗黒期間の backfill (production PG 用)")
    parser.add_argument("--limit", type=int, default=None, help="処理上限 (既定: 全対象)")
    parser.add_argument("--dry-run", action="store_true", help="書き込みなしで判定結果のみ表示")
    parser.add_argument(
        "--sleep", type=float, default=1.0, help="LLM 呼び出し間の待機秒 (既定 1.0)"
    )
    parser.add_argument(
        "--no-cron-guard",
        action="store_true",
        help="cron guard (毎時 :57-:16 / 朝夕の重量 cron 窓での待機) を無効化",
    )
    args = parser.parse_args()
    asyncio.run(
        run(
            args.limit,
            dry_run=args.dry_run,
            sleep_seconds=args.sleep,
            cron_guard=not args.no_cron_guard,
        )
    )


if __name__ == "__main__":
    main()
