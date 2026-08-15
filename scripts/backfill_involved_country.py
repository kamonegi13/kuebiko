#!/usr/bin/env python3
"""geopolitical/policy 記事に「関与国 (involved_country)」をローカル LLM で補完する。

情勢ブリッジ (2026-06-22): サイバー↔地政学を「国家」で相関するには、地政学事象に
**当事国 (主体国)** のタグが要る (victim_country=被害国・サイバー用とは別軸)。これが
無いと地政学記事の ~2% (サイバーAPT actor 経由) しか国家に紐づかず、相関が始まらない。
briefing/summarizer.j2 に involved_countries を追加済 (前方の新記事は埋まる)。本スクリプトは
**既存バックログ**を救済し、actor.nation 経由のサイバー↔地政学相関を解禁する。

設計 ([[news_search_consolidation]] / CLAUDE.md §4):
  - **ローカル LLM のみ** (Ollama)。送るのは見出し + 要約のみ。
  - **当事国を全て** (二国間/多国間は両方)。原文の国名/ISO を taxonomy で ISO 正規化。
  - **保守的**: 曖昧/該当無しは付けない (偽の相関を作らない)。未知国名は skip。
  - **additive only**: 既に involved_country を持つ記事は触らない。dry-run 既定、apply で永続化。
  - ``--limit`` で件数制限 (newest 優先)。entity_type='involved_country' に ISO で保存。

Usage (production = PG、コンテナ内):
    docker exec kuebiko /app/.venv/bin/python3 \\
        /app/scripts/backfill_involved_country.py --limit 200            # dry-run
    docker exec kuebiko /app/.venv/bin/python3 \\
        /app/scripts/backfill_involved_country.py --limit 800 --apply    # 実行
"""

from __future__ import annotations

import asyncio
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config_loader import load_app_config  # noqa: E402
from src.cti.taxonomy_normalizer import load_normalizer  # noqa: E402
from src.storage.db_backend import connect  # noqa: E402
from src.storage.run_history import RunHistoryRepository  # noqa: E402
from src.tools.llm_client import OllamaClient  # noqa: E402

_GEO = "('geopolitical','policy')"
_SUMMARY_MAX = 600
_MAX_COUNTRIES = 4  # 1記事あたり保存上限 (多国間でも 4 まで)

_SYSTEM = (
    "あなたは地政学アナリスト。記事の見出しと要約から、その事象の **当事国 (主体国)** を "
    "挙げる。行為主体と対象の **両方** を含める (例: ロシアのウクライナ侵攻 → Russia, Ukraine / "
    "中台緊張 → China, Taiwan / 米イラン → US, Iran)。"
    "回答は **国名または ISO 3166-1 alpha-2 をカンマ区切りで 1〜3 個** だけ。"
    "記事が明確に扱う国のみ。曖昧/該当なしは厳密に 'NONE'。説明や記号を付けない。推測で広げない。"
)


def _fetch(limit: int) -> list[tuple[str, str, str]]:
    """(article_id, title, summary) を新しい順で返す (involved_country 未付与の geopol 記事)。"""
    con = connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT article_id, title, summary FROM articles a "
            "WHERE a.status='posted' "
            f"AND a.category IN {_GEO} "
            "AND NOT EXISTS (SELECT 1 FROM article_entities ae "
            "  WHERE ae.article_id = a.article_id AND ae.entity_type='involved_country') "
            "ORDER BY a.id DESC"
        )
        rows = cur.fetchall()
    finally:
        con.close()
    return [(str(r[0]), str(r[1] or ""), str(r[2] or "")) for r in rows][:limit]


def _parse_countries(text: str) -> list[str]:
    """LLM 出力 (カンマ区切り) を国名/ISO のリストに (NONE/空は []、前後ノイズ除去)。"""
    line = ""
    for ln in (text or "").splitlines():
        s = ln.strip().strip("\"'`")
        if s:
            line = s
            break
    if not line or line.upper() == "NONE":
        return []
    return [c.strip().strip("[]'\"") for c in line.split(",") if c.strip()][:_MAX_COUNTRIES]


async def main(limit: int, apply: bool) -> int:
    config = load_app_config()
    normalizer = load_normalizer()
    repo = RunHistoryRepository()
    client = OllamaClient(
        base_url=config.ollama_base_url,
        model=config.ollama_main_model,
        timeout_seconds=120.0,
    )
    items = _fetch(limit)
    print(f"=== 関与国 LLM 補完: 候補 {len(items)} 記事 (geopol / involved_country 未付与) ===")

    updates: list[tuple[str, list[str]]] = []  # (article_id, [iso,...])
    dist: Counter[str] = Counter()
    skipped = 0
    for aid, title, summary in items:
        prompt = f"見出し: {title}\n要約: {summary[:_SUMMARY_MAX]}\n当事国:"
        try:
            resp = await client.generate(
                prompt, system=_SYSTEM, temperature=0.0, max_tokens=32, think=False
            )
        except Exception as e:  # noqa: BLE001 — 1 件失敗は skip
            skipped += 1
            print(f"  ERR  {type(e).__name__}: {title[:42]}")
            continue
        isos: list[str] = []
        seen: set[str] = set()
        for raw in _parse_countries(resp.text):
            iso, _ = normalizer.normalize_country(raw)
            if iso and iso.lower() not in seen:
                seen.add(iso.lower())
                isos.append(iso)
        if not isos:
            skipped += 1
            continue
        updates.append((aid, isos))
        for i in isos:
            dist[i] += 1
        print(f"  {','.join(isos):16} {title[:46]}")

    print(f"\n解決 {len(updates)} / skip {skipped} / 候補 {len(items)}")
    print("関与国 top: " + ", ".join(f"{k}={v}" for k, v in dist.most_common(12)))
    if not apply:
        print("\n[dry-run] 書き込みなし。--apply で additive 永続化を実行する。")
        return 0

    for aid, isos in updates:
        repo.add_article_entities(aid, [("involved_country", i) for i in isos])
    print(f"[applied] {len(updates)} 記事に involved_country を付与した。")
    return 0


if __name__ == "__main__":
    argv = sys.argv[1:]
    lim = 200
    for i, a in enumerate(argv):
        if a == "--limit" and i + 1 < len(argv):
            lim = int(argv[i + 1])
    sys.exit(asyncio.run(main(lim, apply="--apply" in argv)))
