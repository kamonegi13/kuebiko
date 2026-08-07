"""subject-gate の shadow 計測 (2026-07-27)。

docs/body_extraction_and_entity_integrity_redesign.md §5 (D1) の完全 subject-gate を
**本番挙動を変えずに**評価する。攻撃帰属集計 (地図 flow / 情勢ボード攻撃元 / 概況 actor-nation)
の母集団で、現行 (mention 全数) と subject-gate (主題のみ・legacy は mention fallback) の
差分を出し、落とす mention-only ペアをサンプル抽出して目視検証する。

read-only (DB を変更しない)。使い方:
  docker exec kuebiko uv run python -m scripts.shadow_subject_gate --days 90 --sample 30
"""

from __future__ import annotations

import argparse

from src.storage.run_history import RunHistoryRepository

# 攻撃帰属集計の母集団 (geo_cyber_map / situation / overview と同じ cyber victim 記事)。
_CYBER_CATS = ("apt", "malware", "incident", "breach", "advisory")

# 主題メンバーシップ述語: actor id が記事の subject_actor_ids に含まれるか。
# **STRPOS (literal substring) を使う** — LIKE は (a) id 中の `_` がワイルドカードになり、
# (b) psycopg が `%,` を placeholder と誤解する二重の罠がある (2026-07-27 試行で判明)。
# STRPOS は PG。SQLite は INSTR。この診断は PG (コンテナ) 対象。本実装は translate_sql に
# INSTR→STRPOS を足して両対応にする (要領メモ)。
_MEMBER = "STRPOS(',' || COALESCE(a.subject_actor_ids,'') || ',', ',' || ae.value || ',') > 0"
# subject-gate: 評価済み行は主題メンバーシップ、legacy (source NULL) は mention fallback。
_SUBJECT_GATE = f"(a.subject_actor_source IS NULL OR {_MEMBER})"
_ATTR_WHERE = (
    "ae.entity_type='actor' "
    "AND a.victim_country_iso IS NOT NULL AND a.victim_country_iso <> '' "
    f"AND a.category IN ({','.join('?' for _ in _CYBER_CATS)}) "
    "AND datetime(a.created_at) >= datetime('now', ?)"
)


def _run(days: int, sample: int) -> None:
    repo = RunHistoryRepository()
    win = f"-{days} days"
    with repo._connect() as con:  # noqa: SLF001 — 診断スクリプト
        # 1) 母集団の内訳
        total = con.execute(
            "SELECT "
            "COUNT(*) AS pairs, "
            "SUM(CASE WHEN a.subject_actor_source IS NULL THEN 1 ELSE 0 END) AS legacy_kept, "
            f"SUM(CASE WHEN a.subject_actor_source IS NOT NULL AND {_MEMBER} "
            "  THEN 1 ELSE 0 END) AS evaluated_subject, "
            f"SUM(CASE WHEN a.subject_actor_source IS NOT NULL AND NOT {_MEMBER} "
            "  THEN 1 ELSE 0 END) AS dropped_mention_only "
            "FROM article_entities ae JOIN articles a ON a.article_id = ae.article_id "
            f"WHERE {_ATTR_WHERE}",
            (*_CYBER_CATS, win),
        ).fetchone()

        pairs = int(total["pairs"] or 0)
        kept = int(total["evaluated_subject"] or 0) + int(total["legacy_kept"] or 0)
        dropped = int(total["dropped_mention_only"] or 0)
        print(f"\n=== subject-gate shadow ({days}日、攻撃帰属ペア) ===")
        print(f"母集団ペア             : {pairs}")
        print(f"現行 (mention 全数)    : {pairs}")
        print(f"subject-gate 後 (残す) : {kept}  ({100 * kept / max(pairs, 1):.1f}%)")
        print(f"  └ 評価済み主題       : {int(total['evaluated_subject'] or 0)}")
        print(f"  └ legacy fallback    : {int(total['legacy_kept'] or 0)}")
        print(f"落とす (言及のみ)      : {dropped}  ({100 * dropped / max(pairs, 1):.1f}%)")

        # 2) 落とす分の actor 別 top (どのアクターの言及帰属が消えるか)
        rows = con.execute(
            "SELECT ae.value AS actor, COUNT(*) AS n "
            "FROM article_entities ae JOIN articles a ON a.article_id = ae.article_id "
            f"WHERE {_ATTR_WHERE} AND a.subject_actor_source IS NOT NULL AND NOT {_MEMBER} "
            "GROUP BY ae.value ORDER BY n DESC LIMIT 15",
            (*_CYBER_CATS, win),
        ).fetchall()
        print("\n=== 落とす言及帰属 (actor 別 top15) ===")
        for r in rows:
            print(f"  {str(r['actor']):24} {int(r['n'])}")

        # 3) 目視検証用サンプル: 落とす mention-only ペアの記事タイトル + 実際の主題
        samp = con.execute(
            "SELECT ae.value AS mention_actor, a.subject_actor_ids AS subject, "
            "a.victim_country_iso AS victim, a.title AS title "
            "FROM article_entities ae JOIN articles a ON a.article_id = ae.article_id "
            f"WHERE {_ATTR_WHERE} AND a.subject_actor_source IS NOT NULL AND NOT {_MEMBER} "
            "ORDER BY a.created_at DESC LIMIT ?",
            (*_CYBER_CATS, win, sample),
        ).fetchall()
        print(f"\n=== 目視サンプル (落とすペア {len(samp)} 件: 言及 actor → 実主題) ===")
        for r in samp:
            subj = str(r["subject"] or "(なし)")
            print(f"  [{str(r['victim']):3}] 言及={str(r['mention_actor']):20} 主題={subj:20}")
            print(f"        {str(r['title'])[:90]}")


def main() -> None:
    ap = argparse.ArgumentParser(description="subject-gate の shadow 計測 (read-only)")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--sample", type=int, default=30)
    args = ap.parse_args()
    _run(args.days, args.sample)


if __name__ == "__main__":
    main()
