"""持ち越し導入に伴う ``presented_count`` の初期化 (one-shot backfill)。

2026-08-14 の第 2 弾で、forecast の採点は「指標を LLM に提示したか」
(``situation_forecasts.presented_count``) だけを見るようになった。列は
DEFAULT 0 で追加されるため、**既に一度以上提示されていた既存の open 予測**も
0 のまま残る。そのまま horizon を迎えると「未照会」と誤判定し、今度は逆に
的中率を過大評価してしまう (過確信側の誤り = より悪い)。

移行時点で照会済みだったものを 1 に初期化して連続性を保つ。判定は旧実装と同じ
推測規則 — 予測を開いた後にその情勢の revision が 1 本でもあれば、直前 revision の
indicators として提示されていた:

    opened_at < revision.created_at

冪等 (presented_count = 0 の行だけを 1 にする)。

採点済み行も整合のため合わせて初期化する (hit = 発火したのだから提示済み、
expired = 照会済みと判定したから expired になった)。採点は open 行しか読まないので
動作には影響しないが、0 のままだと「hit なのに未照会」という自己矛盾した
データに見えて監査時に誤読を招く。

usage (コンテナ内実行 — DATABASE_URL が compose 内でのみ解決するため):
  docker exec kuebiko python -m scripts.backfill_forecast_presented            # dry-run
  docker exec kuebiko python -m scripts.backfill_forecast_presented --apply
"""

from __future__ import annotations

import argparse

from src.assessment.situation_store import SituationStore

_TARGET_SQL = """
    FROM situation_forecasts f
   WHERE f.status = 'open' AND f.presented_count = 0
     AND EXISTS (SELECT 1 FROM situation_revisions r
                  WHERE r.situation_id = f.situation_id
                    AND r.created_at > f.opened_at)
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="presented_count を 1 にする")
    args = parser.parse_args()

    store = SituationStore()
    with store._repo._connect() as conn:  # noqa: SLF001
        total = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM situation_forecasts WHERE status='open'"
            ).fetchone()["n"]
        )
        # 現状 (再実行時に「未初期化」と「元から未照会」を取り違えないよう実測で出す)
        already = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM situation_forecasts"
                " WHERE status='open' AND presented_count > 0"
            ).fetchone()["n"]
        )
        target = int(conn.execute(f"SELECT COUNT(*) AS n {_TARGET_SQL}").fetchone()["n"])

    print(f"open {total} 件 (うち照会済み {already} 件)")
    print(f"今回初期化する: {target} 件")
    print(f"初期化後も未照会: {total - already - target} 件 (次回 synthesis で提示されれば加算)")

    if not args.apply:
        print("\n(dry-run — 書き込むには --apply)")
        return

    with store._repo._connect() as conn:  # noqa: SLF001
        cur = conn.execute(
            f"UPDATE situation_forecasts SET presented_count = 1"  # noqa: S608 — 定数 SQL
            f" WHERE id IN (SELECT f.id {_TARGET_SQL})"
        )
        n = int(cur.rowcount if cur.rowcount is not None else 0)
        # 採点済みの整合合わせ (動作影響なし・監査時の誤読防止)
        cur2 = conn.execute(
            "UPDATE situation_forecasts SET presented_count = 1"
            " WHERE presented_count = 0 AND status IN ('hit', 'expired')"
        )
        n2 = int(cur2.rowcount if cur2.rowcount is not None else 0)
    print(f"\nopen {n} 件 / 採点済み {n2} 件を初期化した")


if __name__ == "__main__":
    main()
