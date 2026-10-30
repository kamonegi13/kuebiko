#!/usr/bin/env python3
"""薄い本文の関門の誤落ち (extract_failed / thin_body) を保存済み本文で回収する (2026-10-30)。

使い方 (⚠ 本番コンテナ kuebiko の中では実行しない — GPU を取り合い段が timeout する。
本番が空いている時間か本番を止めて、使い捨てコンテナで。compose の環境変数・config・
prompts のマウントを本番と揃えるため、イメージだけ差し替えて compose で走らせる):
    docker build -t kuebiko-recover:tmp <このブランチの worktree>
    # override.yml: services.kuebiko.image: kuebiko-recover:tmp / build: !reset null
    docker compose -f docker-compose.yml -f override.yml run --rm --no-deps --no-build -T \
      kuebiko python scripts/recover_thin_body_articles.py --since <前回の --until> \
      [--apply] [--limit N]
選定の範囲: --since / --until は created_at の下限 / 上限 (until は排他)。--min-age-hours N は
「now - N 時間より新しい行を除く」で、--until と併用すれば早い方が効く。既定 0 = 直近も含める
(2026-10-30 以降は articles.analysis_only 列が配信・dedup・公開・PIR の朝の要点から除くので、
通常の取り込みと競合しても配信へは漏れない。旧版は 48h 空けていた)。
本文 600 字未満は既定で対象外 (--include-short で含める)。どちらも今の関門が最終判定する。

既定は dry-run。--apply は LLM を呼び DB を更新する。冪等 (status='extract_failed' の
行だけが対象で、回収後は status='posted' / analysis_only='thin_body_recovery')。
Discord へは何も流さない。
ログは件数と article_id のみ (本文・要約は出さない)。詳細は src/pipeline/thin_body_recovery.py。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config_loader import load_app_config, load_llm_enrichment  # noqa: E402
from src.logging_config import configure_logging, get_logger  # noqa: E402
from src.pipeline.dispatch import _load_template  # noqa: E402
from src.pipeline.thin_body_recovery import (  # noqa: E402
    MIN_BODY_CHARS,
    effective_until,
    recover_one,
    select_candidates,
)
from src.storage.run_history import RunHistoryRepository  # noqa: E402
from src.tools.model_tiers import Step, build_llm_for  # noqa: E402

_log = get_logger(__name__)


def _parse_since(v: str | None) -> datetime | None:
    if not v:
        return None
    parsed = datetime.fromisoformat(v)  # 日付 / 日時のどちらも可 (tz 無しは UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def _run(
    apply: bool,
    limit: int | None,
    since: datetime | None,
    until: datetime | None,
    min_body_chars: int,
) -> int:
    repo = RunHistoryRepository()
    rows = repo.list_thin_body_extract_failed(since=since, until=until)
    candidates = select_candidates(rows, min_body_chars=min_body_chars)
    print(
        f"extract_failed(thin_body): {len(rows)} 記事 / 今の関門を通る: "
        f"{len(candidates)} / 今回の上限: {limit or '無し'}"
    )
    if not apply:
        print("dry-run (--apply で再要約して回収する)")
        return 0
    by_id = {str(r["article_id"]): r for r in rows}
    cfg = load_app_config()
    llm = build_llm_for(Step.ARTICLE_SUMMARY, cfg)
    enrichment = load_llm_enrichment()
    template = _load_template()
    ok = failed = 0
    started = time.monotonic()
    # --limit は「成功した件数」の上限。失敗した記事はこの実行では飛ばして次へ進む
    # (固定のリストを順に走るので、恒常的に失敗する記事が先頭を塞がない)。
    for i, cand in enumerate(candidates, 1):
        if limit is not None and ok >= limit:
            break
        try:
            done = await recover_one(
                repo, by_id[cand.article_id], llm=llm, template=template, enrichment=enrichment
            )
        except Exception as e:  # noqa: BLE001 — 1 件の失敗で全体を止めない (再実行で拾い直す)
            failed += 1
            _log.warning(
                "thin_body_recover_failed", article_id=cand.article_id, error=type(e).__name__
            )
            continue
        ok += int(done)
        failed += int(not done)
        if not done:
            _log.warning("thin_body_recover_returned_false", article_id=cand.article_id)
        if i % 10 == 0:
            print(
                f"progress {i}/{len(candidates)} ok={ok} failed={failed} "
                f"elapsed={int(time.monotonic() - started)}s"
            )
    print(f"done ok={ok} failed={failed} elapsed={int(time.monotonic() - started)}s")
    return 0 if failed == 0 else 1


def main() -> int:
    configure_logging()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="実際に再要約して更新する (既定 dry-run)")
    ap.add_argument("--limit", type=int, default=None, help="1 回の処理件数の上限 (新しい順)")
    ap.add_argument("--until", default=None, help="この日付/日時より前の created_at のみ")
    ap.add_argument(
        "--include-short",
        action="store_true",
        help="本文 600 字未満も対象にする (既定は 600 字以上のみ)",
    )
    ap.add_argument(
        "--min-age-hours",
        type=float,
        default=0.0,
        help="この時間より新しい created_at を除く (既定 0 = 直近も含める)",
    )
    ap.add_argument("--since", default=None, help="この日付 (YYYY-MM-DD) 以降の created_at のみ")
    args = ap.parse_args()
    return asyncio.run(
        _run(
            args.apply,
            args.limit,
            _parse_since(args.since),
            effective_until(_parse_since(args.until), args.min_age_hours),
            0 if args.include_short else MIN_BODY_CHARS,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
