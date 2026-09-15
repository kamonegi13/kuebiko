#!/usr/bin/env python3
"""変質した常設情報要求の**問い**を seed へ復元する (2026-09-15)。

背景: 増分 ACH の同一性追従 (`stateful.update_title`) が常設情報要求にも適用されており、
LLM の claim (= **答え**) が title (= **問い**) を上書きしていた。実測では seed 4 問のうち
3 問が別の問いへ変質していた — 北朝鮮の問いは「日本の重要インフラ」という主題を丸ごと落として
韓国金融の話になっていた。posture カードは見出しが国名・本文が仮説ラベルのため、
UI では劣化が見えなかった。

恒久対処は `stateful.title_follows_claim` (常設は追従させない)。本スクリプトは既に変質した
問いを `STANDING_SEEDS` の文へ戻す一回性の是正。

**問い以外は触らない** — revision (答えの履歴) はそのまま残す。問いがずれた期間の答えが
どうずれたかは、次の再評価が正しい問いで上書きしていく。

使い方 (コンテナ内):
    docker exec kuebiko python scripts/restore_standing_questions.py            # dry-run
    docker exec kuebiko python scripts/restore_standing_questions.py --apply
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.assessment.situation_store import SituationStore  # noqa: E402
from src.assessment.standing import STANDING_SEEDS  # noqa: E402


def drifted() -> list[tuple[str, str, str]]:
    """(situation_id, いまの title, seed の問い) — 変質しているものだけ。"""
    store = SituationStore()
    out: list[tuple[str, str, str]] = []
    for seed in STANDING_SEEDS:
        row = store.get_situation(seed.situation_id)
        if row is not None and row.title != seed.title:
            out.append((seed.situation_id, row.title, seed.title))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--apply", action="store_true", help="実際に復元する (既定は dry-run)")
    args = ap.parse_args()

    targets = drifted()
    if not targets:
        print("変質した問いはありません。")
        return 0

    print(f"変質した問い {len(targets)} 件:")
    for sid, current, seed_title in targets:
        print(f"\n  {sid}")
        print(f"    いま: {current[:76]}")
        print(f"    seed: {seed_title[:76]}")

    if not args.apply:
        print("\n(dry-run — 変更なし。実行は --apply)")
        return 0

    store = SituationStore()
    tag = datetime.now(UTC).strftime("%Y%m%d")
    with store._repo._connect() as conn:  # noqa: SLF001 — 是正スクリプトの接続 seam 共有
        conn.execute(
            f"CREATE TABLE _backup_standing_title_{tag} AS "
            "SELECT situation_id, title FROM situations WHERE kind='standing'"
        )
        conn.commit()
    for sid, _current, seed_title in targets:
        store.update_title(sid, seed_title)
    print(f"\n復元しました ({len(targets)} 件)。退避 = _backup_standing_title_{tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
