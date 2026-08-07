#!/usr/bin/env python3
"""intent 暗黒期間の backfill (2026-07-12 土台修復)。

b8b4e7dd (2026-06-30) が summarizer の intent 指示を「証拠で裏付く時のみ」へ変えた結果、
弱シグナルの intent が unknown に倒れて被覆が 75%→3% に崩壊した (prepositioning は 2 週間 0 件)。
プロンプト側は「確度つき記録」(過剰確信のみ禁止) に修復済み。本スクリプトは DB に保持済みの
原文 (body 残存 97%) へ intent のみを再抽出し、暗黒期間 + 90 日サイバー系サブセットを
修復後と同一レジームに揃える。

対象 (冪等 — 処理済みは intent_backfill_log が記憶):
  - 暗黒期間 (2026-06-29 以降) の posted 全件で intent_confidence IS NULL
  - それ以前 90 日の posted × サイバー系カテゴリ (apt/malware/incident/advisory/
    vulnerability/breach)。旧レジーム tag (confidence NULL) は上書きして単一レジームにする

書き込み規約 (ingest と同一):
  - intent != unknown → articles.socio_political_intent / intent_confidence /
    socio_political_rationale を UPDATE
  - unknown → articles は触らない (NULL のまま)。処理済みの事実だけ log に残す
  - 全処理行を intent_backfill_log (scratch table、app schema 外) に記録 = 冪等 + provenance
    (intent_confidence が NULL でない行は「新レジームで評価済み」の印)

運用ガード:
  - LLM は ingest と同一の Step.ARTICLE_SUMMARY tier (fast) / think=False
  - cron guard: 毎時 :57-:16 (hourly rss triage) と 06:15-08:05 / 19:25-20:05
    (朝刊 / 夕方 synthesis) は LLM 呼び出しを一時停止 (Ollama 直列キューの奪い合い回避)

Usage (production PG、コンテナ内。dev SQLite は対象外):
    docker cp scripts/backfill_intent.py kuebiko:/tmp/backfill_intent.py
    docker exec kuebiko python /tmp/backfill_intent.py --limit 20 --dry-run
    docker exec kuebiko python /tmp/backfill_intent.py --limit 500
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO if (_REPO / "src").is_dir() else Path("/app")))

from src.config_loader import load_app_config  # noqa: E402
from src.cti.diamond_model import parse_diamond_axes  # noqa: E402
from src.storage.db_backend import connect  # noqa: E402
from src.tools.model_tiers import Step, build_llm_for  # noqa: E402

# 暗黒期間の開始 = b8b4e7dd デプロイ日 (これ以降は全カテゴリが対象)
DARK_START = "2026-06-29"
# それ以前の再抽出対象 = 推論の主原料になるサイバー系カテゴリ (audit_triage_calibration と同一)
CYBER_CATEGORIES: tuple[str, ...] = (
    "apt",
    "malware",
    "incident",
    "advisory",
    "vulnerability",
    "breach",
)
LOOKBACK_DAYS = 90
BODY_MAX_CHARS = 8000  # intent 判定には十分な長さ。呼出時間を 5-15s/件 に保つ
PROGRESS_EVERY = 25
MAX_CONSECUTIVE_ERRORS = 5  # Ollama 停止等の連続失敗でアボート

# cron guard: 毎時 :57-:16 は hourly rss triage と重なるため停止
GUARD_MINUTE_FROM = 57
GUARD_MINUTE_TO = 16
# 固定の重量 cron 窓 (HH:MM 文字列比較)。朝刊 (06:30 起動) / 夕方 synthesis (19:30 起動)
QUIET_WINDOWS: tuple[tuple[str, str, str], ...] = (
    ("06:15", "08:05", "morning-brief"),
    ("19:25", "20:05", "evening-synthesis"),
)
GUARD_POLL_SECONDS = 60.0

_PROMPT_TEMPLATE = """あなたは CTI アナリスト。以下の記事について、主体 (攻撃者・国家等) の\
戦略的 intent (動機 = 何を達成しようとしているか) を 1 つ判定し、JSON のみで出力する。

# intent の選択肢 (いずれか 1 つ)
- espionage: 諜報・情報窃取 (機密/知財/個人情報の標的的窃取)
- financial: 金銭目的 (ransomware / 窃取 / 詐欺 / 暗号資産。DPRK 外貨稼ぎ含む)
- prepositioning: 事前配置 (有事に備えた重要インフラへの足場確保・潜伏。破壊や窃取をまだ行わず\
 access を維持。Volt Typhoon 型)
- disruption: 破壊・妨害 (wiper / sabotage / OT 物理影響 / DDoS。機能を損なうこと自体が目的)
- influence: 影響工作・認知戦 (世論操作 / disinformation / hack-and-leak)
- hacktivism: 主義主張・抗議 (イデオロギー動機)
- coercion: 威圧・強要 (圧力/威嚇/制裁/示威で相手の行動・政策を変えさせる)
- deterrence: 抑止 (能力・決意を示し相手の行動を思いとどまらせる。防御的)
- territorial: 領土・主権 (領土/主権/係争海域の主張・奪取・越境)
- subversion: 体制動揺・転覆 (対象国の内部の安定・正統性を内側から崩す)
- diplomacy: 外交・同盟 (同盟・条約・正常化・連携の協調行動)
- unknown: 動機を推定する材料が本文に実質ない

# 判定規則
- confidence は証拠の強さの正直な申告: high=標的・動機が本文で実証 / medium=強い状況証拠・\
推定混じり / low=弱いシグナルからの仮説 (ツール類似のみ・コモディティ malware・帰属未確定)。
- 過剰確信の禁止: 禁じるのは確度の水増しであって、仮説の記録ではない。弱いシグナルでも方向が\
読み取れるなら最有力の intent + confidence=low で記録し、rationale に根拠の弱さを明示する\
 (例: 「ツール類似のみ・帰属未確定」)。low 相当の根拠を medium/high と申告しない。
- unknown は「材料が実質ない」場合のみ (製品リリース / 一般調査・統計レポート / 主体の意図が\
論点にならない記事)。弱い証拠を unknown に倒さない。
- rationale: 判定根拠の日本語 1 行 (80 字以内)。

# 出力 (JSON のみ、他のテキスト禁止)
{{"intent": "...", "confidence": "high|medium|low", "rationale": "..."}}

# 記事
カテゴリ: {category}
タイトル: {title}
本文:
{text}
"""

_LOG_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS intent_backfill_log (
    article_pk BIGINT PRIMARY KEY,
    intent TEXT,
    confidence TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


class _IntentOut(BaseModel):
    """LLM 構造化出力 (防御は parse_diamond_axes 側で行うため str のまま受ける)。"""

    model_config = ConfigDict(extra="ignore")

    intent: str = "unknown"
    confidence: str = "low"
    rationale: str | None = None


def build_prompt(title: str, category: str | None, body: str | None, summary: str | None) -> str:
    """intent 専用プロンプトを組み立てる (body 優先、なければ summary)。"""
    text = (body or "").strip()
    if len(text) < 200:
        text = (summary or "").strip()
    return _PROMPT_TEMPLATE.format(
        category=category or "unknown",
        title=title.strip(),
        text=text[:BODY_MAX_CHARS],
    )


def normalize_result(out: _IntentOut) -> tuple[str | None, str | None, str | None]:
    """LLM 出力を ingest と同じ防御パーサで正規化する。

    unknown (未知語含む) は (None, None, None) — articles には書かない。
    """
    axes = parse_diamond_axes(
        {
            "socio_political": {
                "intent": out.intent,
                "confidence": out.confidence,
                "rationale": out.rationale,
            }
        }
    )
    sp = axes.socio_political
    if sp.intent == "unknown":
        return (None, None, None)
    return (sp.intent, sp.confidence, sp.rationale or None)


def guard_reason(now: datetime) -> str | None:
    """LLM 呼び出しを止めるべき cron 窓なら理由ラベルを返す (通常運転は None)。"""
    if now.minute >= GUARD_MINUTE_FROM or now.minute <= GUARD_MINUTE_TO:
        return "hourly-rss"
    hhmm = now.strftime("%H:%M")
    for start, end, label in QUIET_WINDOWS:
        if start <= hhmm <= end:
            return label
    return None


_TargetRow = tuple[int, str, str | None, str | None, str | None]


def _select_targets(conn: object, limit: int | None) -> list[_TargetRow]:
    """対象記事 (id, title, category, body, summary) を新しい順に取得する。"""
    placeholders = ", ".join("?" for _ in CYBER_CATEGORIES)
    sql = f"""
        SELECT id, title, category, body, summary
        FROM articles
        WHERE status = 'posted'
          AND intent_confidence IS NULL
          AND (
                created_at >= ?
                OR (
                    created_at > NOW() - INTERVAL '{LOOKBACK_DAYS} days'
                    AND category IN ({placeholders})
                )
          )
          AND id NOT IN (SELECT article_pk FROM intent_backfill_log)
        ORDER BY created_at DESC
    """
    params: list[object] = [DARK_START, *CYBER_CATEGORIES]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, params).fetchall()  # type: ignore[attr-defined]
    return [(int(r[0]), str(r[1]), r[2], r[3], r[4]) for r in rows]


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
    # dry-run でも作成する (SELECT の NOT IN が参照する。作成自体は記事データを書かない)
    conn.execute(_LOG_TABLE_DDL)

    targets = _select_targets(conn, limit)
    print(f"対象: {len(targets)} 件 (dry_run={dry_run}, model={llm.model})", flush=True)

    stats: Counter[str] = Counter()
    consecutive_errors = 0
    for i, (article_pk, title, category, body, summary) in enumerate(targets, start=1):
        await _wait_for_clear_window(cron_guard=cron_guard)
        prompt = build_prompt(title, category, body, summary)
        try:
            out = await llm.generate_structured(prompt, schema=_IntentOut, think=False)
            consecutive_errors = 0
        except Exception as exc:  # noqa: BLE001 (個別失敗は skip、連続失敗のみアボート)
            consecutive_errors += 1
            stats["error"] += 1
            print(f"[error] id={article_pk}: {type(exc).__name__}", flush=True)
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                print(
                    f"連続 {MAX_CONSECUTIVE_ERRORS} 回失敗 — Ollama 停止の疑い。アボート",
                    flush=True,
                )
                break
            continue

        intent, confidence, rationale = normalize_result(out)
        stats[intent or "unknown"] += 1
        if dry_run:
            print(f"[dry] id={article_pk} cat={category} -> {intent} ({confidence})", flush=True)
        else:
            if intent is not None:
                conn.execute(
                    "UPDATE articles SET socio_political_intent = ?, intent_confidence = ?,"
                    " socio_political_rationale = ? WHERE id = ?",
                    (intent, confidence, rationale, article_pk),
                )
            conn.execute(
                "INSERT INTO intent_backfill_log (article_pk, intent, confidence)"
                " VALUES (?, ?, ?) ON CONFLICT (article_pk) DO NOTHING",
                (article_pk, intent, confidence),
            )
        if i % PROGRESS_EVERY == 0:
            print(f"[progress] {i}/{len(targets)} {dict(stats)}", flush=True)
        if sleep_seconds > 0:
            await asyncio.sleep(sleep_seconds)

    processed = sum(stats.values())
    print(f"完了: {processed} 件処理 / 内訳 {dict(stats)}", flush=True)
    return processed


def main() -> None:
    parser = argparse.ArgumentParser(description="intent 暗黒期間の backfill (production PG 用)")
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
