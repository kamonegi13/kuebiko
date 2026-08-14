"""誤って expired (=外れ) と採点された「未照会」予測を unevaluated へ再分類する one-shot backfill。

較正バグ (2026-08-14): forecast の hit は「後続 revision の fired_indicators」経由でしか
立たない。後続 revision はその情勢に新しい記事が来たときにしか生まれないため、
**予測を開いた後に revision が 1 本も無い予測は構造的に hit が不可能**。旧実装はこれを
expired と採点していたため、的中率が体系的に過小評価されていた。

コード側 (src/assessment/forecast.py `_terminal_status`) は是正済み。本スクリプトは
既に採点済みの行だけを同じ規則で再分類する。

- 判定規則はコードと同一: 後続 revision (created_at > opened_at) の有無のみ。
  ただし窓は **採点時点まで** に限る (opened_at < created_at <= scored_at)。live 経路は
  採点 = now なので自然にこの窓になるが、遡及採点では採点後の revision まで数えてしまい
  「採点時に知り得なかった再評価」を根拠にしてしまう。
- hit は一切触らない (的中の取り消しはしない)。
- note を上書きし、backfill 由来であることを明示する (透明・追跡可能)。

usage (コンテナ内実行 — DATABASE_URL が compose 内でのみ解決するため):
  docker exec kuebiko python -m scripts.backfill_forecast_unevaluated            # dry-run
  docker exec kuebiko python -m scripts.backfill_forecast_unevaluated --apply
"""

from __future__ import annotations

import argparse

from src.assessment.situation_store import SituationStore

_NOTE = "情勢が再評価されず指標を一度も照会できなかった (未照会・2026-08-14 再分類)"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="再分類を書き込む")
    args = parser.parse_args()

    store = SituationStore()

    with store._repo._connect() as conn:  # noqa: SLF001
        total = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM situation_forecasts WHERE status = 'expired'"
            ).fetchone()["n"]
        )
        # 採点窓 (opened_at, scored_at] に revision が 1 本も無い = 一度も照会されなかった
        rows = conn.execute(
            "SELECT id, situation_id, indicator FROM situation_forecasts f"
            " WHERE f.status = 'expired' AND NOT EXISTS ("
            "   SELECT 1 FROM situation_revisions r"
            "   WHERE r.situation_id = f.situation_id"
            "     AND r.created_at > f.opened_at"
            "     AND r.created_at <= COALESCE(f.scored_at, f.opened_at))"
        ).fetchall()

    targets: list[int] = []
    for r in rows:
        targets.append(int(r["id"]))
        print(f"  {r['situation_id']} :: {str(r['indicator'])[:60]}")

    print(f"\nexpired {total} 件中 {len(targets)} 件が未照会 (再分類対象)")
    kept = total - len(targets)
    if kept:
        print(f"的中率の分母: {kept} 件が真の未発現として残る")

    if not args.apply:
        print("\n(dry-run — 書き込むには --apply)")
        return

    with store._repo._connect() as conn:  # noqa: SLF001
        for fid in targets:
            conn.execute(
                "UPDATE situation_forecasts SET status = 'unevaluated', note = ?"
                " WHERE id = ? AND status = 'expired'",
                (_NOTE, fid),
            )
    print(f"\n{len(targets)} 件を unevaluated へ再分類した")


if __name__ == "__main__":
    main()
