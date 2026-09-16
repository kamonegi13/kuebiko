#!/usr/bin/env python3
"""収穫済みの教師対 (synthesis.jsonl / _oversize.jsonl) の prompt を現在の render 形式で再描画する。

render プロンプトの形式を変えたとき (2026-09-17: SIR ロールアップの番号参照化)、既に外部枠で
取った completion を捨てずに使うための道具。completion は estimate の**内容**にしか依存しない
ので、同じ estimate から現在の seam (``build_render_plan``) でプロンプトだけ作り直せば
学習時と本番の入力分布が揃う。再描画後に pair 見積りが予算内に戻った対は本体へ戻す。

⚠ コンテナ内で (DB 読み)。元ファイルは ``.bak`` に退避する。
    docker exec kuebiko python scripts/rerender_sft_teacher_prompts.py [--period daily] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.build_sft_teacher_synthesis import (  # noqa: E402
    DEFAULT_OUT,
    Window,
    _est_tokens,
    _oversize_path,
    select_windows,
)
from src.storage.run_history import RunHistoryRepository  # noqa: E402

_SCAN = 600
_MAX_PAIR_TOKENS = 13_000


def rerender_rows(
    rows: list[dict[str, str]], prompts: dict[str, str], *, max_pair_tokens: int
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[str]]:
    """行の prompt を差し替え、(本体行, 副ファイル行, 再描画できなかった key) を返す (純粋関数)。"""
    main: list[dict[str, str]] = []
    side: list[dict[str, str]] = []
    missing: list[str] = []
    for row in rows:
        key = row["key"]
        if key not in prompts:
            missing.append(key)
            continue
        new = {**row, "prompt": prompts[key]}
        pair = _est_tokens(new["prompt"]) + _est_tokens(new["completion"])
        if pair > max_pair_tokens:
            side.append({**new, "reason": "pair_too_long", "pair_tokens_est": str(pair)})
        else:
            main.append({k: v for k, v in new.items() if k not in ("reason", "pair_tokens_est")})
    return main, side, missing


def _read(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _write(path: Path, rows: list[dict[str, str]]) -> None:
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--period", default="daily")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    side_path = _oversize_path(args.out)
    rows = _read(args.out) + _read(side_path)
    if not rows:
        print("対が無い", file=sys.stderr)
        return 1
    repo = RunHistoryRepository()
    records = repo.list_synthesis(period_type=args.period, limit=_SCAN)
    # 予約 (審判 / 直近) は再描画の対象選定には関係ない — 既に取った対の key だけ引く
    windows: list[Window]
    windows, _ = select_windows(
        records,
        period_type=args.period,
        reserved=set(),
        reserve_before=datetime.now(UTC) + timedelta(days=1),
        cot=any("analysis_notes" in r["completion"] for r in rows),
    )
    prompts = {w.key: w.prompt for w in windows}
    main_rows, side_rows, missing = rerender_rows(rows, prompts, max_pair_tokens=_MAX_PAIR_TOKENS)
    before = sum(_est_tokens(r["prompt"]) for r in rows) / max(1, len(rows))
    after = sum(_est_tokens(r["prompt"]) for r in main_rows + side_rows) / max(
        1, len(main_rows) + len(side_rows)
    )
    print(
        f"再描画 {len(main_rows) + len(side_rows)} / 未再描画 {len(missing)} — "
        f"本体 {len(main_rows)} 副 {len(side_rows)} / prompt 平均 {before:.0f} → {after:.0f} tok"
    )
    for key in missing:
        print(f"  ⚠ {key}: 現在の窓に無い (estimate 消失?) — そのまま残す")
    if args.dry_run:
        return 0
    for p in (args.out, side_path):
        if p.exists():
            shutil.copy2(p, p.with_suffix(p.suffix + ".bak"))
    keep_missing = [r for r in rows if r["key"] in set(missing)]
    _write(args.out, main_rows + [r for r in keep_missing if "reason" not in r])
    _write(side_path, side_rows + [r for r in keep_missing if "reason" in r])
    print(f"書込: {args.out} / {side_path} (.bak 退避済)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
