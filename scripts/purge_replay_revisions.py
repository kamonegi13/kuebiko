#!/usr/bin/env python3
"""過去日付リプレイ (2026-09-06 の教師収穫) が台帳に書いた汚染行を除去する。

汚染の署名: ``created_at`` / ``read_at`` / ``assessed_at`` / ``run_at`` が **UTC 00:00:00 ちょうど**
(リプレイは ``today.replace(hour=0, ...) - timedelta(days=d)`` を now に渡した)。本番 run は
06:30/19:30 JST 起動でマイクロ秒まで入るため、この署名は本番行と重ならない。

対象 (2026-09-15 実測): situation_revisions 95 件 (うち 41 件が situation の最新 rev)、
situation_evidence の read_at 133 / assessed_at 72 / added_at 10、situation_detection_log 166。
最新 rev が汚染行だと、以後の daily 増分 ACH はその行を prev として delta を計算する
(= 過去窓の証拠で作った判定に対する差分)。

方針: 復元可能に消す。削除前に ``_backup_replay_purge_<種別>_<date>`` へ丸ごと退避し、既定は
``--dry-run`` (件数を出すだけ)。``--apply`` で実行。revision の rev 番号は詰めない
(UNIQUE(situation_id, rev) の採番は max(rev)+1 なので欠番があっても壊れない)。

使い方 (コンテナ内):
    docker exec kuebiko python scripts/purge_replay_revisions.py            # dry-run
    docker exec kuebiko python scripts/purge_replay_revisions.py --apply
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.storage.run_history import RunHistoryRepository  # noqa: E402

# 署名: 'YYYY-MM-DDT00:00:00+00:00' (マイクロ秒なし)。LIKE で末尾を固定する。
_SIGNATURE = "%T00:00:00+00:00"

_COUNTS = {
    "revisions": "SELECT COUNT(*) FROM situation_revisions WHERE created_at LIKE ?",
    "revisions_latest": """
        SELECT COUNT(*) FROM situation_revisions r
         WHERE r.created_at LIKE ?
           AND r.rev = (SELECT MAX(rev) FROM situation_revisions x
                         WHERE x.situation_id = r.situation_id)
    """,
    "evidence_read": "SELECT COUNT(*) FROM situation_evidence WHERE read_at LIKE ?",
    "evidence_assessed": "SELECT COUNT(*) FROM situation_evidence WHERE assessed_at LIKE ?",
    "evidence_added": "SELECT COUNT(*) FROM situation_evidence WHERE added_at LIKE ?",
    "detection_log": "SELECT COUNT(*) FROM situation_detection_log WHERE run_at LIKE ?",
}


def counts(repo: RunHistoryRepository) -> dict[str, int]:
    out: dict[str, int] = {}
    with repo._connect() as conn:  # noqa: SLF001 — 監査スクリプトの接続 seam 共有
        for name, sql in _COUNTS.items():
            row = conn.execute(sql, (_SIGNATURE,)).fetchone()
            out[name] = int(row[0]) if row else 0
    return out


def apply(repo: RunHistoryRepository, *, tag: str) -> None:
    """退避 → 削除/リセット。1 トランザクション。"""
    with repo._connect() as conn:  # noqa: SLF001
        conn.execute(
            f"CREATE TABLE _backup_replay_purge_rev_{tag} AS "
            "SELECT * FROM situation_revisions WHERE created_at LIKE ?",
            (_SIGNATURE,),
        )
        conn.execute(
            f"CREATE TABLE _backup_replay_purge_ev_{tag} AS "
            "SELECT * FROM situation_evidence "
            "WHERE read_at LIKE ? OR assessed_at LIKE ? OR added_at LIKE ?",
            (_SIGNATURE, _SIGNATURE, _SIGNATURE),
        )
        conn.execute(
            f"CREATE TABLE _backup_replay_purge_det_{tag} AS "
            "SELECT * FROM situation_detection_log WHERE run_at LIKE ?",
            (_SIGNATURE,),
        )
        conn.execute("DELETE FROM situation_revisions WHERE created_at LIKE ?", (_SIGNATURE,))
        # 既読/評価のマークは NULL に戻す (未読キューへ復帰 = 次の run が正規に読み直す)。
        conn.execute(
            "UPDATE situation_evidence SET read_at = NULL WHERE read_at LIKE ?", (_SIGNATURE,)
        )
        conn.execute(
            "UPDATE situation_evidence SET assessed_at = NULL WHERE assessed_at LIKE ?",
            (_SIGNATURE,),
        )
        # リプレイが新規に割り当てた行 (added_at が署名) は割当ごと消す。
        conn.execute("DELETE FROM situation_evidence WHERE added_at LIKE ?", (_SIGNATURE,))
        conn.execute("DELETE FROM situation_detection_log WHERE run_at LIKE ?", (_SIGNATURE,))
        conn.commit()


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--apply", action="store_true", help="実際に退避 + 削除する (既定は dry-run)")
    args = ap.parse_args()
    repo = RunHistoryRepository()
    before = counts(repo)
    print("汚染行 (署名 = UTC 00:00:00 ちょうど):")
    for k, v in before.items():
        print(f"  {k:18s} {v}")
    if not args.apply:
        print("\n(dry-run — 変更なし。実行は --apply)")
        return 0
    tag = datetime.now(UTC).strftime("%Y%m%d")
    apply(repo, tag=tag)
    after = counts(repo)
    print(f"\n退避テーブル _backup_replay_purge_{{rev,ev,det}}_{tag} を作成し除去した。残存:")
    for k, v in after.items():
        print(f"  {k:18s} {v}")
    return 0 if all(v == 0 for v in after.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
