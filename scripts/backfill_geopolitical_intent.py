#!/usr/bin/env python3
"""socio_political_intent が null の地政学/政策記事を、ローカル LLM で動機補完する。

Phase Geopolitical-Intent (2026-06-21): intent 軸をサイバー↔地政学を貫く統一軸に拡張し、
briefing/summarizer.j2 も「地政学事案でも intent を埋めよ」に変更した (前方の新記事は埋まる)。
本スクリプトは **地図に乗る既存バックログ** を救済して脅威マップを即座に動機色分けする。

設計 ([[diamond_axes_design]] / [[news_search_consolidation]] / CLAUDE.md §4):
  - **ローカル LLM のみ** (Ollama)。外部 API・記事本文のクラウド送信は一切なし。
    送るのは見出し + 要約 (≤600字) のみ。
  - **対象 = 地図に乗る記事優先**: 既定で ``victim_country_iso`` がある geopolitical/policy 記事
    (= 地図のダイヤ) のうち intent 未判定のものだけ。``--all-geo`` で国なしも含む全件に拡大。
  - **保守的**: 当てはまる動機が無ければ ``unknown`` (色は出さない)。捏造しない。
  - **additive only**: 既に intent がある record は触らない。既定 dry-run、``--apply`` で UPDATE。
  - ``--limit`` で 1 回の処理件数を制限 (newest 優先)。off-hours 推奨。

Usage (production = PG、コンテナ内で実行):
    docker exec kuebiko /app/.venv/bin/python3 \\
        /app/scripts/backfill_geopolitical_intent.py --limit 200            # dry-run
    docker exec kuebiko /app/.venv/bin/python3 \\
        /app/scripts/backfill_geopolitical_intent.py --limit 600 --apply    # 実行
"""

from __future__ import annotations

import asyncio
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config_loader import load_app_config  # noqa: E402
from src.cti.diamond_model import INTENT_LABELS_JA, normalize_intent  # noqa: E402
from src.storage.db_backend import connect  # noqa: E402
from src.tools.llm_client import OllamaClient  # noqa: E402

_GEO = "('geopolitical','policy')"
# --cyber: サイバー被害カテゴリ (geo_cyber_map._CYBER_ATTACK_CATEGORIES と一致)。
# 地図の「動機」色分け統一で cyber バブルも intent パイにするため、cyber backlog も補完する。
_CYBER = "('breach','incident','apt','apt_leak','malware','phishing')"
_SUMMARY_MAX = 600

# 統一 intent 軸の動機 (unknown 除く)。プロンプトに enum と短い定義を渡す。
_SYSTEM = (
    "あなたは CTI/地政学アナリスト。記事の見出しと要約から、主体 (国家等) の **戦略的動機** を "
    "次の語から 1 つだけ選び、その英語の語だけを答える (説明・記号なし):\n"
    "espionage(諜報) / financial(金銭) / prepositioning(事前配置) / disruption(破壊妨害) / "
    "influence(世論操作) / hacktivism(主義主張) / coercion(威圧・制裁・示威で行動を変えさせる) / "
    "deterrence(抑止=行動を思いとどまらせる防御的シグナル) / territorial(領土・主権・係争) / "
    "subversion(代理勢力/扇動で内部の安定を崩す) / diplomacy(同盟・条約・正常化の協調)。\n"
    "境界: coercion=行動を変えさせる / deterrence=止めさせる / disruption=機能を壊す / "
    "influence=世論を変える / subversion=政治秩序を崩す。当てはまらなければ unknown。"
)


def _fetch(limit: int, include_no_country: bool, cyber: bool) -> list[tuple[int, str, str]]:
    """(id, title, summary) を新しい順で返す (intent 未判定の対象記事)。

    cyber=False: geopolitical/policy、cyber=True: サイバー被害カテゴリ。
    既定 (include_no_country=False) は地図に乗る victim_country あり記事のみ。
    """
    cats = _CYBER if cyber else _GEO
    has_country = "AND victim_country_iso IS NOT NULL AND victim_country_iso != '' "
    country_clause = "" if include_no_country else has_country
    con = connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT id, title, summary FROM articles "
            "WHERE status='posted' "
            "AND (socio_political_intent IS NULL OR socio_political_intent='') "
            f"AND category IN {cats} "
            f"{country_clause}"
            "ORDER BY id DESC"
        )
        rows = cur.fetchall()
    finally:
        con.close()
    return [(int(r[0]), str(r[1] or ""), str(r[2] or "")) for r in rows][:limit]


def _first_word(text: str) -> str:
    """LLM 出力から最初の語を取り出す (think 漏れ/余計な語への保険)。"""
    for line in (text or "").splitlines():
        s = line.strip().strip("\"'`.,!").strip()
        if s:
            return s.split()[0] if s.split() else ""
    return ""


async def main(limit: int, apply: bool, include_no_country: bool, cyber: bool) -> int:
    config = load_app_config()
    client = OllamaClient(
        base_url=config.ollama_base_url,
        model=config.ollama_main_model,
        timeout_seconds=120.0,
    )
    items = _fetch(limit, include_no_country, cyber)
    domain = "サイバー" if cyber else "地政学"
    scope = "全件" if include_no_country else "地図対象 (国あり)"
    print(f"=== {domain} intent LLM 補完: 候補 {len(items)} 記事 ({scope} / intent=null) ===")

    updates: list[tuple[int, str]] = []  # (id, intent)
    dist: Counter[str] = Counter()
    skipped = 0
    for aid, title, summary in items:
        prompt = f"見出し: {title}\n要約: {summary[:_SUMMARY_MAX]}\n動機:"
        try:
            resp = await client.generate(
                prompt, system=_SYSTEM, temperature=0.0, max_tokens=16, think=False
            )
        except Exception as e:  # noqa: BLE001 — 1 件失敗は skip して継続
            skipped += 1
            print(f"{aid:>7}  ERR   {type(e).__name__}: {title[:40]}")
            continue
        intent = normalize_intent(_first_word(resp.text))
        if intent == "unknown":
            skipped += 1
            continue
        updates.append((aid, intent))
        dist[intent] += 1
        print(f"{aid:>7}  {intent:14} {INTENT_LABELS_JA.get(intent, ''):8} {title[:38]}")

    print(f"\n解決 {len(updates)} / unknown・err {skipped} / 候補 {len(items)}")
    print("動機分布: " + ", ".join(f"{k}={v}" for k, v in dist.most_common()))
    if not apply:
        print("\n[dry-run] 書き込みなし。--apply で additive UPDATE を実行する。")
        return 0

    con = connect()
    try:
        cur = con.cursor()
        for aid, intent_val in updates:
            cur.execute(
                "UPDATE articles SET socio_political_intent=? "
                "WHERE id=? AND (socio_political_intent IS NULL OR socio_political_intent='')",
                (intent_val, aid),
            )
        con.commit()
    finally:
        con.close()
    print(f"[applied] {len(updates)} 件の socio_political_intent を補完した。")
    return 0


if __name__ == "__main__":
    argv = sys.argv[1:]
    lim = 200
    for i, a in enumerate(argv):
        if a == "--limit" and i + 1 < len(argv):
            lim = int(argv[i + 1])
    sys.exit(
        asyncio.run(
            main(
                lim,
                apply="--apply" in argv,
                include_no_country="--all" in argv or "--all-geo" in argv,
                cyber="--cyber" in argv,
            )
        )
    )
