#!/usr/bin/env python3
"""文字化け本文 (U+FFFD 混入) を全文再取得キューへ戻す一回性の是正 (2026-09-06)。

背景 (2026-09-04 実測): @IT / ITmedia がヘッダ charset 無しの Shift_JIS 応答を返し、
httpx の UTF-8 既定で本文が U+FFFD へ**不可逆に**化けて保存されていた (230 記事)。
検出器修正 (``content_extractor._detect_charset``) は 2026-09-06 デプロイ済み。

化けた記事は本文が「ある」ため既存の再取得キュー (``list_articles_needing_refetch``)
の対象外。**破壊的な NULL 化はせず**、body_source を 'feed_summary' (切り株) に戻して
attempts をリセットする — 以後は既設の毎時 body-refetch ジョブが修正済み抽出器で
全文化 + 再エンリッチする (A4 の設計経路に乗せるだけ)。失敗しても現本文は残る。

使用例 (コンテナ内):
    docker exec kuebiko python scripts/requeue_mojibake_bodies.py           # dry-run
    docker exec kuebiko python scripts/requeue_mojibake_bodies.py --apply
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.storage.run_history import RunHistoryRepository  # noqa: E402

# U+FFFD (REPLACEMENT CHARACTER)。position() は 1-based で 0 = 不在。
# ⚠ 列は必ず別名を振る — PG backend は行を dict で返すため、同名列 (count が 2 本) は
# 1 キーに潰れる (2026-08-24 の「別名なし COALESCE が潰れる」と同型)。
_SELECT = (
    "SELECT count(DISTINCT article_id) AS n_articles, count(*) AS n_rows FROM articles "
    "WHERE position(chr(65533) in body) > 0"
)
_UPDATE = (
    "UPDATE articles SET body_source='feed_summary', "
    "extraction_failure_reason=NULL, refetch_attempts=0 "
    "WHERE position(chr(65533) in body) > 0"
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="実際に更新する (既定 dry-run)")
    args = ap.parse_args()

    repo = RunHistoryRepository()
    with repo._connect() as conn:  # noqa: SLF001 — 一回性の是正スクリプト
        row = conn.execute(_SELECT).fetchone()
        # PG backend は行を dict-like で返す (SQLite は tuple)。両対応。
        vals = list(row.values()) if hasattr(row, "values") else list(row)
        distinct, rows = vals[0], vals[1]
        print(f"対象: {distinct} 記事 ({rows} 行)")
        if not args.apply:
            print("dry-run (— --apply で切り株状態へ戻し毎時再取得ジョブに乗せる)")
            return 0
        conn.execute(_UPDATE)
        conn.commit()
    queued = repo.list_articles_needing_refetch(limit=500)
    print(f"更新完了。再取得キュー現在 {len(queued)} 件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
