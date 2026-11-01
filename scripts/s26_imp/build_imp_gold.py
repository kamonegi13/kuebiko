#!/usr/bin/env python3
"""S26-IMP-GOLD (1/2): gold:imp_precheck_s26 の標本を選ぶ (prereg §2.2)。Opus は呼ばない。DB は読み取りのみ。

  python scripts/s26_imp/build_imp_gold.py [--dry-run]

--dry-run は標本・manifest を書かず、R18 の後の層ごとの母集団・標本の件数だけを出す。
実行は使い捨てコンテナで (data/eval/autorun_plan_2026_10_24.md の triage_fresh_s26 の実行と同じ型)。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.replay._common import assert_no_body, model_tiers_version  # noqa: E402
from scripts.s26_imp import closure  # noqa: E402
from scripts.s26_imp.common import (  # noqa: E402
    DATA_ROOT,
    PREREG,
    SEED_GOLD,
    event_ts_expr,
    git_head,
    open_db,
    run_gate,
    sha256_bytes,
    write_jsonl,
)

OUT = DATA_ROOT / "data/eval/imp_precheck_s26"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    started = datetime.now(UTC)
    rec = run_gate(Path(__file__), dry_run=args.dry_run, out_dir=None if args.dry_run else OUT)

    rp9, s24mod = closure.load_modules()
    ev = event_ts_expr()
    c = open_db()
    pool, pool_counts = closure.fetch_pool(c, ev)
    res = closure.compute_eligible(c, rp9, s24mod, pool, ev)
    imp = {a: pool[a]["importance"] for a in pool}
    picked, strata = closure.stratified_sample(res.eligible, imp, seed=SEED_GOLD)

    summary = {
        "population": pool_counts,
        "excluded_by_source_overlapping": res.excluded_by_source,
        "excluded_union": res.excluded_union,
        "eligible": len(res.eligible),
        "strata_after_r18": strata,
        "sampled": len(picked),
        "r18": res.diagnostics,
    }
    if args.dry_run:
        print(json.dumps(summary, ensure_ascii=False, indent=1))
        print(f"[dry-run] 書かない。Opus は呼ばない (この段は Opus なし)。標本 {len(picked)} 件")
        return 0

    assert rec is not None
    sample = [
        {
            "article_id": a,
            "body_sha256": sha256_bytes((pool[a]["body"] or "").encode()),
            "stored_importance": pool[a]["importance"],
        }
        for a in picked
    ]
    sample_sha = write_jsonl(OUT / "sample.jsonl", sample)
    manifest = {
        "name": "imp_precheck_s26",
        "item": "S26-IMP-GOLD",
        "built_at": datetime.now(UTC).isoformat(),
        "run_started_at": started.isoformat(),
        "prereg": PREREG,
        "prereg_path": rec.path,
        "prereg_git_sha": rec.git_sha,
        "prereg_blob_sha256": rec.blob_sha256,
        "head_sha": rec.head_sha,
        "git_head": git_head(),
        "model_tiers_version": model_tiers_version(c),
        "seed": SEED_GOLD,
        "window_event_ts_jst": list(closure.WINDOW),
        "universe_from": closure.UNIVERSE_FROM,
        "body_min_chars": closure.BODY_MIN_CHARS,
        **summary,
        "files": {"data/eval/imp_precheck_s26/sample.jsonl": sample_sha},
    }
    assert_no_body(manifest)
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True) + "\n", "utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
