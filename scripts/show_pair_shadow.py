"""群化シャドー観測の記録を読む (食い違いの確認 / 評価セットへの書き出し)。

⚠ この script は **event_pair_shadow の消費者**。CLAUDE.md §7 の 3 点セット
「消費者を 1 つ以上同時に実装 (write-only 列を作らない)」に対応する。

使い方:
    python scripts/show_pair_shadow.py              # 集計 + 食い違いを表示
    python scripts/show_pair_shadow.py --all        # 全件
    python scripts/show_pair_shadow.py --export out.json   # 採点用に書き出し
"""

import argparse
import json
import sys

sys.path.insert(0, "/app")

from src.storage.db_backend import connect


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="一致した組も表示する")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--export", default="", help="採点用 JSON の書き出し先")
    args = ap.parse_args()

    with connect() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT s.observed_at, s.left_id, s.right_id, s.features_json,
                   s.llm_same, s.rule_joined, s.cos,
                   la.title AS left_title, ra.title AS right_title,
                   la.feed_title AS left_feed, ra.feed_title AS right_feed
            FROM event_pair_shadow s
            JOIN articles la ON la.article_id = s.left_id
            JOIN articles ra ON ra.article_id = s.right_id
            ORDER BY s.id DESC
        """)
        rows = [dict(r) for r in cur.fetchall()]

    if not rows:
        print("記録がありません (EVENTNEWS_PAIR_SHADOW=1 で有効化してください)")
        return

    judged = [r for r in rows if r["llm_same"] is not None]
    disagree = [r for r in judged if bool(r["llm_same"]) != bool(r["rule_joined"])]
    print(f"記録 {len(rows)} 組 / 判定できた {len(judged)} / **食い違い {len(disagree)}**")
    if judged:
        pct = 100 * len(disagree) / len(judged)
        print(
            f"食い違い率 {pct:.0f}%  (LLM が繋ぐ {sum(bool(r['llm_same']) for r in judged)} / "
            f"規則が繋ぐ {sum(bool(r['rule_joined']) for r in judged)})"
        )

    target = rows if args.all else disagree
    print(f"\n{'── 全件 ──' if args.all else '── 食い違った組 (ここを読む) ──'}")
    for r in target[: args.limit]:
        llm = "繋ぐ" if r["llm_same"] else ("繋がない" if r["llm_same"] is not None else "判定不可")
        rule = "繋ぐ" if r["rule_joined"] else "繋がない"
        print(f"\ncos={r['cos']:.3f}  LLM={llm} / 規則={rule}")
        print(f"  A {r['left_title'][:58]} [{r['left_feed'][:18]}]")
        print(f"  B {r['right_title'][:58]} [{r['right_feed'][:18]}]")

    if args.export:
        out = [
            {
                "a": r["left_id"],
                "b": r["right_id"],
                "cos": r["cos"],
                "title_a": r["left_title"],
                "title_b": r["right_title"],
                "features": json.loads(r["features_json"]),
                "llm_same": None if r["llm_same"] is None else bool(r["llm_same"]),
                "rule_joined": bool(r["rule_joined"]),
            }
            for r in rows
        ]
        with open(args.export, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        print(f"\n{len(out)} 組を {args.export} へ書き出しました (ラベルを付ければ採点できます)")


main()
