#!/usr/bin/env python3
"""過去日付リプレイ (2026-09-06 の教師収穫) が台帳に書いた汚染行を除去・復元する。

汚染の署名: ``created_at`` / ``read_at`` / ``assessed_at`` / ``run_at`` / ``last_evidence_at`` が
**UTC 00:00:00 ちょうど** (リプレイは ``today.replace(hour=0, ...) - timedelta(days=d)`` を now に
渡した)。本番 run は 06:30/19:30 JST 起動でマイクロ秒まで入るため、この署名は本番行と重ならない。

対象 (2026-09-15 実測):

| 表 | 汚染 | 処置 |
|---|---|---|
| situation_revisions | 95 (41 件が最新 rev) | 削除 (rev 番号は詰めない — 採番は max+1) |
| situation_evidence.read_at / assessed_at | 133 / 72 | NULL へ戻す (未読キューへ復帰) |
| situation_evidence.added_at | 10 | 行ごと削除 (リプレイが作った割当) |
| situation_detection_log.run_at | 166 | 削除 |
| **situations.last_evidence_at** | **40** | **真値へ復元 + 不当休眠を active へ戻す** |

最後の 1 行が実害の本体: ``touch_situation`` が ``last_evidence_at`` を過去日付に書き戻したため、
休眠 sweep (cyber_incident 14 日 / 他 30 日) がそれを「古い」と判定した。実測で **14 件が
不当に休眠**していた (真の最終証拠は 8-10 日前 = 本来 active)。真値 = 汚染でない証拠の
``max(added_at)`` (無ければ ``opened_at``)。閾値は ``src.assessment.ledger`` から読む (SSoT)。

**3 つ目の経路 — ``situations.title``** (``--repair-titles``、2026-09-15 追加): 増分 ACH は
「評価済み claim が title と乖離したら title を進める」(同一性追従 P2) ため、リプレイの claim が
``update_title`` でタイトルに焼き付いていた。revision を消してもタイトルは残る。しかも
リプレイの 26B は日本語を壊すことがあり (「乱用調査委員会」→「事取査御会」)、
**タイトルだけが化けた situation** が残った。復元 = 退避テーブルの claim と一致する title を、
現存する最新 revision の claim へ戻す (コードが保つ不変量と同じ値)。

**復元しないもの** (正直に記録): ``assessed_at`` を NULL に戻した 72 行の ``polarity`` /
``attribution_basis`` / ``excerpt`` はリプレイの ACH が上書きした値のまま残る。次の増分 ACH が
評価し直すときに上書きされる (抜粋は本文照合済みなので捏造ではない)。

方針: 復元可能に消す。削除前に ``_backup_replay_purge_<種別>_<date>`` へ丸ごと退避し、既定は
dry-run (件数を出すだけ)。``--apply`` で実行。

使い方 (コンテナ内):
    docker exec kuebiko python scripts/purge_replay_revisions.py            # dry-run
    docker exec kuebiko python scripts/purge_replay_revisions.py --apply
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.assessment.ledger import _DORMANT_AFTER_DAYS, _DORMANT_DEFAULT_DAYS  # noqa: E402
from src.storage.run_history import RunHistoryRepository  # noqa: E402

# 署名: 'YYYY-MM-DDT00:00:00+00:00' (マイクロ秒なし)。LIKE で末尾を固定する。
_SIGNATURE = "%T00:00:00+00:00"

_COUNTS = {
    "revisions": "SELECT COUNT(*) AS n FROM situation_revisions WHERE created_at LIKE ?",
    "revisions_latest": """
        SELECT COUNT(*) AS n FROM situation_revisions r
         WHERE r.created_at LIKE ?
           AND r.rev = (SELECT MAX(rev) FROM situation_revisions x
                         WHERE x.situation_id = r.situation_id)
    """,
    "evidence_read": "SELECT COUNT(*) AS n FROM situation_evidence WHERE read_at LIKE ?",
    "evidence_assessed": "SELECT COUNT(*) AS n FROM situation_evidence WHERE assessed_at LIKE ?",
    "evidence_added": "SELECT COUNT(*) AS n FROM situation_evidence WHERE added_at LIKE ?",
    "detection_log": "SELECT COUNT(*) AS n FROM situation_detection_log WHERE run_at LIKE ?",
    "situations_last_ev": "SELECT COUNT(*) AS n FROM situations WHERE last_evidence_at LIKE ?",
}

# 汚染 situation と、その「真の最終証拠時刻」(汚染でない証拠の max、無ければ opened_at)。
_POLLUTED_SITUATIONS = """
    SELECT s.situation_id AS situation_id, s.status AS status, s.domain AS domain,
           s.opened_at AS opened_at, s.last_evidence_at AS bad_at,
           MAX(CASE WHEN e.added_at NOT LIKE ? THEN e.added_at END) AS true_ev
      FROM situations s
      LEFT JOIN situation_evidence e ON e.situation_id = s.situation_id
     WHERE s.last_evidence_at LIKE ?
     GROUP BY s.situation_id, s.status, s.domain, s.opened_at, s.last_evidence_at
"""


def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def situation_repairs(conn: Any, *, now: datetime) -> list[dict[str, Any]]:
    """汚染 situation ごとの復元内容 (真値と、休眠を解くべきか) を決める。"""
    rows = conn.execute(_POLLUTED_SITUATIONS, (_SIGNATURE, _SIGNATURE)).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        truth = str(r["true_ev"] or r["opened_at"])
        idle_days = (now - _parse(truth)).total_seconds() / 86400
        threshold = _DORMANT_AFTER_DAYS.get(str(r["domain"]), _DORMANT_DEFAULT_DAYS)
        out.append(
            {
                "situation_id": str(r["situation_id"]),
                "status": str(r["status"]),
                "bad_at": str(r["bad_at"]),
                "truth": truth,
                "idle_days": idle_days,
                # 真値なら休眠閾値に届かない = 汚染が原因の不当休眠 → active へ戻す
                "reactivate": str(r["status"]) == "dormant" and idle_days < threshold,
            }
        )
    return out


# タイトルがリプレイの claim (退避テーブル) と一致する situation と、現存する最新 claim。
_POLLUTED_TITLES = """
    SELECT s.situation_id AS situation_id, s.title AS title, r.claim AS latest_claim
      FROM situations s
      JOIN situation_revisions r ON r.situation_id = s.situation_id
       AND r.rev = (SELECT MAX(rev) FROM situation_revisions x
                     WHERE x.situation_id = s.situation_id)
     WHERE s.title <> r.claim
       AND EXISTS (SELECT 1 FROM {backup} b
                    WHERE b.situation_id = s.situation_id AND b.claim = s.title)
"""


def repair_titles(repo: RunHistoryRepository, *, tag: str, dry_run: bool) -> int:
    """リプレイの claim が焼き付いたタイトルを、現存する最新 claim へ戻す。"""
    sql = _POLLUTED_TITLES.format(backup=f"_backup_replay_purge_rev_{tag}")
    with repo._connect() as conn:  # noqa: SLF001
        rows = conn.execute(sql).fetchall()
        targets = [(str(r["situation_id"]), str(r["title"]), str(r["latest_claim"])) for r in rows]
        if dry_run:
            for sid, title, claim in targets[:5]:
                print(f"  {sid} 旧: {title[:38]}")
                print(f"  {' ' * len(sid)} 新: {claim[:38]}")
            return len(targets)
        if targets:
            conn.execute(
                f"CREATE TABLE _backup_replay_purge_title_{tag} AS "
                f"SELECT s.situation_id, s.title FROM situations s WHERE s.situation_id IN "
                f"(SELECT situation_id FROM ({sql}) t)"
            )
            for sid, _title, claim in targets:
                conn.execute("UPDATE situations SET title = ? WHERE situation_id = ?", (claim, sid))
            conn.commit()
    return len(targets)


def counts(repo: RunHistoryRepository) -> dict[str, int]:
    out: dict[str, int] = {}
    with repo._connect() as conn:  # noqa: SLF001 — 監査スクリプトの接続 seam 共有
        for name, sql in _COUNTS.items():
            row = conn.execute(sql, (_SIGNATURE,)).fetchone()
            out[name] = int(row["n"]) if row else 0
        out["wrongly_dormant"] = sum(
            1 for r in situation_repairs(conn, now=datetime.now(UTC)) if r["reactivate"]
        )
    return out


def apply(repo: RunHistoryRepository, *, tag: str) -> None:
    """退避 → 削除/リセット/復元。1 接続 = 1 トランザクション。"""
    with repo._connect() as conn:  # noqa: SLF001
        repairs = situation_repairs(conn, now=datetime.now(UTC))
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
        conn.execute(
            f"CREATE TABLE _backup_replay_purge_sit_{tag} AS "
            "SELECT * FROM situations WHERE last_evidence_at LIKE ?",
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
        # situations: 最終証拠時刻を真値へ、汚染が原因の休眠は active へ戻す。
        for r in repairs:
            conn.execute(
                "UPDATE situations SET last_evidence_at = ? WHERE situation_id = ?",
                (r["truth"], r["situation_id"]),
            )
            if r["reactivate"]:
                conn.execute(
                    "UPDATE situations SET status = 'active' WHERE situation_id = ?",
                    (r["situation_id"],),
                )
        conn.commit()


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--apply", action="store_true", help="実際に退避 + 除去 + 復元する")
    ap.add_argument(
        "--repair-titles",
        action="store_true",
        help="タイトルに焼き付いたリプレイ claim を最新 claim へ戻す (--apply と併用で実行)",
    )
    ap.add_argument("--tag", default="20260914", help="退避テーブルの日付タグ")
    args = ap.parse_args()
    repo = RunHistoryRepository()
    if args.repair_titles:
        n = repair_titles(repo, tag=args.tag, dry_run=not args.apply)
        verb = "復元した" if args.apply else "が対象 (dry-run — 変更なし)"
        print(f"タイトル汚染 {n} 件{verb}")
        return 0
    before = counts(repo)
    print("汚染行 (署名 = UTC 00:00:00 ちょうど):")
    for k, v in before.items():
        print(f"  {k:20s} {v}")
    if not args.apply:
        print("\n(dry-run — 変更なし。実行は --apply)")
        return 0
    tag = datetime.now(UTC).strftime("%Y%m%d")
    apply(repo, tag=tag)
    after = counts(repo)
    print(f"\n退避 _backup_replay_purge_{{rev,ev,det,sit}}_{tag} を作成し除去・復元した。残存:")
    for k, v in after.items():
        print(f"  {k:20s} {v}")
    return 0 if all(v == 0 for v in after.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
