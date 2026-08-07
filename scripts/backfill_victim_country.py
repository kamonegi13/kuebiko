#!/usr/bin/env python3
"""victim_country_iso が null の cyber 記事を、被害組織名からローカル LLM で国補完する。

地図 PoC (2026-06-18) で判明: Comcast / Isuzu / BELFOR 等の **被害組織は名指しされている
のに victim_country_iso が null** という記事が多い。原因は (1) 記事が国を明記せず
(2) 旧 summarizer プロンプトが「named org の本国から国を推定する」指示を欠いていたこと。
プロンプト側は修正済 (前方の新記事は埋まる)。本スクリプトは **既存バックログを救済** する。

設計 ([[geo_map_design_discussion]] / CLAUDE.md §4):
  - **ローカル LLM のみ** (Ollama)。外部 API・記事本文のクラウド送信は一切なし。
    送るのは headline + 被害組織名のみ。地図描画は従来どおり完全オフライン。
  - **保守的**: 本国が曖昧/無名/複数国の組織は ``UNKNOWN`` → ``normalize_country`` が
    None を返し skip。誤った国バブルを出さない (精度の捏造をしない)。
  - **additive only**: 既に iso がある record は触らない (``victim_country_iso IS NULL`` のみ)。
  - 既定は **dry-run** (LLM 判定して集計表示のみ)、``--apply`` で初めて UPDATE。
  - ``--limit`` で 1 回の処理件数を制限 (newest 優先)。off-hours 推奨 (Ollama 競合回避)。

Usage (production = PG、コンテナ内で実行し DATABASE_URL を解決):
    docker exec kuebiko /app/.venv/bin/python3 \\
        /app/scripts/backfill_victim_country.py --limit 150            # dry-run
    docker exec kuebiko /app/.venv/bin/python3 \\
        /app/scripts/backfill_victim_country.py --limit 150 --apply    # 実行
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config_loader import load_app_config  # noqa: E402
from src.cti.taxonomy_normalizer import load_normalizer  # noqa: E402
from src.storage.db_backend import connect  # noqa: E402
from src.tools.llm_client import OllamaClient  # noqa: E402

# 地図対象のサイバー攻撃カテゴリ (geo_cyber_map._CYBER_ATTACK_CATEGORIES と一致)。
_CYBER = "('breach','incident','apt','apt_leak','malware','phishing')"

# リークサイト等で伏字化された被害組織名 (例: "He..t S..it" / "K****** County")。
# 実体が伏せられているため国を推定できない → LLM に渡さず除外 (偽の国を当てない)。
_MASKED_RE = re.compile(r"\.{2,}|\*|█|…|x{3,}|X{3,}")


def _is_masked(org: str) -> bool:
    """組織名が伏字化されている (連続ドット/アスタリスク/伏せ字記号を含む) か。"""
    return bool(_MASKED_RE.search(org))


_SYSTEM = (
    "あなたは CTI アナリスト。サイバー事案の見出しと名指しの被害組織から、"
    "主たる被害組織が所在する単一の国だけを答える。"
    "回答は国名または ISO 3166-1 alpha-2 コードを 1 つだけ。"
    "複数国にまたがる/global/本国が確信できない場合は厳密に 'UNKNOWN' と答える。"
    "推測しない。説明や記号を付けない。"
)


def _fetch(limit: int) -> list[tuple[int, str, list[str]]]:
    """(article.id, title, victim_org のリスト) を新しい順で返す (iso=null の cyber 記事)。"""
    con = connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT a.id, a.title, ae.value "
            "FROM articles a JOIN article_entities ae ON ae.article_id = a.article_id "
            "WHERE ae.entity_type='victim_org' AND a.status='posted' "
            "AND (a.victim_country_iso IS NULL OR a.victim_country_iso='') "
            f"AND a.category IN {_CYBER} "
            "ORDER BY a.id DESC"
        )
        rows = cur.fetchall()
    finally:
        con.close()
    # article ごとに victim_org を集約 (id 降順を維持)。
    by_id: dict[int, tuple[str, list[str]]] = {}
    order: list[int] = []
    for aid, title, org in rows:
        org_s = str(org or "").strip()
        if not org_s or _is_masked(org_s):  # 伏字組織は国推定不可 → 除外
            continue
        if aid not in by_id:
            by_id[aid] = (str(title or ""), [])
            order.append(aid)
        if org_s.lower() not in {o.lower() for o in by_id[aid][1]}:
            by_id[aid][1].append(org_s)
    # org が全て伏字で空になった記事は対象外。
    out = [(aid, by_id[aid][0], by_id[aid][1]) for aid in order if by_id[aid][1]]
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
    normalizer = load_normalizer()
    client = OllamaClient(
        base_url=config.ollama_base_url,
        model=config.ollama_main_model,
        timeout_seconds=120.0,
    )
    items = _fetch(limit)
    print(f"=== victim_country LLM 補完: 候補 {len(items)} 記事 (iso=null / cyber) ===")
    print(f"{'id':>7}  {'判定':>4}  {'org → 国':40}")

    updates: list[tuple[int, str, str]] = []  # (id, iso, raw)
    skipped = 0
    for aid, title, orgs in items:
        org_line = ", ".join(orgs[:5])
        prompt = f"見出し: {title}\n被害組織: {org_line}\n所在国:"
        try:
            resp = await client.generate(
                prompt, system=_SYSTEM, temperature=0.0, max_tokens=24, think=False
            )
        except Exception as e:  # noqa: BLE001 — 1 件失敗は skip して継続
            skipped += 1
            print(f"{aid:>7}  ERR   {type(e).__name__}: {org_line[:36]}")
            continue
        answer = _first_line(resp.text)
        iso, raw = normalizer.normalize_country(answer)
        if not iso:
            skipped += 1
            print(f"{aid:>7}  skip  {org_line[:30]:30} → «{answer[:12]}»")
            continue
        updates.append((aid, iso, raw or answer))
        print(f"{aid:>7}  {iso:>4}  {org_line[:30]:30} → «{answer[:12]}»")

    print(
        f"\n解決 {len(updates)} / skip {skipped} / 候補 {len(items)}"
        f"  (解決率 {len(updates) * 100 // max(len(items), 1)}%)"
    )
    if not apply:
        print("\n[dry-run] 書き込みなし。--apply で additive UPDATE を実行する。")
        return 0

    con = connect()
    try:
        cur = con.cursor()
        for aid, iso, raw in updates:
            cur.execute(
                "UPDATE articles SET victim_country_iso=?, victim_country_raw=? "
                "WHERE id=? AND (victim_country_iso IS NULL OR victim_country_iso='')",
                (iso, raw, aid),
            )
        con.commit()
    finally:
        con.close()
    print(f"[applied] {len(updates)} 件の victim_country_iso を補完した。")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    lim = 150
    for i, a in enumerate(args):
        if a == "--limit" and i + 1 < len(args):
            lim = int(args[i + 1])
    sys.exit(asyncio.run(main(lim, apply="--apply" in args)))
