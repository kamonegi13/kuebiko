#!/usr/bin/env python3
"""victim_city が無い cyber 記事に、被害組織の所在都市をローカル LLM で補完する (3D-c)。

地図の精密点 (CITY tier) は ``article_entities(entity_type='victim_city')`` を GeoNames で
解決する。既存記事の多くは victim_city 未抽出のため、被害国 (victim_country_iso) を持つ
cyber 記事に対し title+summary から **明記された都市のみ** を抽出して補完する。

設計 (CLAUDE.md §4 / summarizer.j2 の victim_city 規約と同じ):
  - **ローカル LLM のみ** (Ollama)。外部送信なし。送るのは見出し+要約のみ。
  - **保守的**: 記事に都市が **明記されている時だけ** 補完。本社所在地を推測で埋めない
    (偽の点を出さない)。明記が無ければ ``NONE`` → skip。
  - 対象は victim_country_iso を持つ記事のみ (geocoder.city は国で曖昧性を解消するため)。
  - 既に victim_city entity を持つ記事は対象外 (additive)。
  - 既定は **dry-run**、``--apply`` で初めて entity 追加。``--limit`` で件数制限。off-hours 推奨。

Usage (production = PG、コンテナ内で実行):
    docker exec kuebiko /app/.venv/bin/python3 \\
        scripts/backfill_victim_city.py --limit 150            # dry-run
    docker exec kuebiko /app/.venv/bin/python3 \\
        scripts/backfill_victim_city.py --limit 150 --apply    # 実行
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config_loader import load_app_config  # noqa: E402
from src.storage.db_backend import connect  # noqa: E402
from src.storage.run_history import RunHistoryRepository  # noqa: E402
from src.tools.llm_client import OllamaClient  # noqa: E402

# 地図対象の cyber カテゴリ (geo_cyber_map._CYBER_ATTACK_CATEGORIES と一致)。
_CYBER = "('breach','incident','apt','apt_leak','malware','phishing')"

_SYSTEM = (
    "あなたは CTI アナリスト。サイバー事案の見出しと要約から、被害組織の所在都市が"
    "**明記されている時だけ** その都市名を 1 つだけ答える (原文表記のまま、例: Osaka / 大阪)。"
    "都市が明記されていない、または国までしか分からない場合は厳密に 'NONE' と答える。"
    "本社所在地を推測で補完しない。説明や記号を付けない。"
)

# 都市でない/不明を表す LLM 応答 (skip)。
_NONE_TOKENS = {"none", "n/a", "na", "-", "不明", "なし", "unknown", "null"}


def _fetch(limit: int) -> list[tuple[str, str, str, str]]:
    """(article_id, title, summary, country_iso) を返す。

    victim_country_iso を持つ cyber 記事で、victim_city entity をまだ持たないもの (新しい順)。
    """
    con = connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT a.article_id, a.title, COALESCE(a.summary, '') AS summary, "
            "a.victim_country_iso "
            "FROM articles a "
            "WHERE a.status IN ('posted', 'collected') "
            "AND a.victim_country_iso IS NOT NULL AND a.victim_country_iso != '' "
            f"AND a.category IN {_CYBER} "
            # ransomware.live は org レベルで都市が記載されないため除外 (LLM 無駄打ち回避)。
            "AND (a.feed_title IS NULL OR a.feed_title <> 'ransomware.live') "
            "AND a.article_id NOT IN ("
            "  SELECT article_id FROM article_entities WHERE entity_type='victim_city') "
            "ORDER BY a.id DESC"
        )
        rows = cur.fetchall()
    finally:
        con.close()
    out: list[tuple[str, str, str, str]] = []
    for aid, title, summary, iso in rows:
        if aid:
            out.append((str(aid), str(title or ""), str(summary or ""), str(iso or "")))
    return out[:limit]


def _first_line(text: str) -> str:
    """LLM 出力の 1 行目を取り出す (think 漏れ/余計な行への保険)。"""
    for line in (text or "").splitlines():
        s = line.strip().strip("\"'`").strip()
        if s:
            return s
    return ""


async def main(limit: int, apply: bool) -> int:
    config = load_app_config()
    repo = RunHistoryRepository()
    client = OllamaClient(
        base_url=config.ollama_base_url,
        model=config.ollama_main_model,
        timeout_seconds=120.0,
    )
    items = _fetch(limit)
    print(f"=== victim_city LLM 補完: 候補 {len(items)} 記事 (国あり / city未設定 / cyber) ===")

    updates: list[tuple[str, str]] = []  # (article_id, city)
    skipped = 0
    for aid, title, summary, iso in items:
        prompt = f"見出し: {title}\n要約: {summary[:400]}\n国: {iso}\n被害組織の所在都市:"
        try:
            resp = await client.generate(
                prompt, system=_SYSTEM, temperature=0.0, max_tokens=24, think=False
            )
        except Exception as e:  # noqa: BLE001 — 1 件失敗は skip して継続
            skipped += 1
            print(f"  ERR {type(e).__name__}: {title[:40]}")
            continue
        city = _first_line(resp.text)
        if not city or city.lower() in _NONE_TOKENS or len(city) > 40:
            skipped += 1
            continue
        updates.append((aid, city))
        print(f"  {iso}  {city[:20]:20} ← {title[:40]}")

    print(
        f"\n解決 {len(updates)} / skip {skipped} / 候補 {len(items)}"
        f"  (解決率 {len(updates) * 100 // max(len(items), 1)}%)"
    )
    if not apply:
        print("\n[dry-run] 書き込みなし。--apply で victim_city entity を追加する。")
        return 0

    added = 0
    for aid, city in updates:
        try:
            added += repo.add_article_entities(aid, [("victim_city", city)])
        except Exception as e:  # noqa: BLE001
            print(f"  persist ERR {type(e).__name__}: {aid}")
    print(f"[applied] victim_city entity を {added} 件追加した。")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    lim = 150
    for i, a in enumerate(args):
        if a == "--limit" and i + 1 < len(args):
            lim = int(args[i + 1])
    sys.exit(asyncio.run(main(lim, apply="--apply" in args)))
