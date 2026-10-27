#!/usr/bin/env python3
"""付け直した深刻度の軸 (jsonl) を本番の ``article_severity_axes`` へ取り込む (D1 手順書 §5.2)。

入力は ``data/mlx/axes_body_backfill.py`` の出力 (1 行 = ``{"article_id", "model", "axes"}``)。
``--model`` に一致する行だけを使い、軸は ``SeverityAxes`` で検証してから書く (語彙外は飛ばす)。
該当記事の ``article_importance_v2`` は消え、毎時の段が新しい軸で導出し直す。

既定は dry-run (件数だけ)。書くのは ``--apply`` のとき。戻し方は手順書 §6 (事前に
``article_severity_axes_bak_d1`` / ``article_importance_v2_bak_d1`` を作っておく)。

Usage (コンテナ内):
    docker compose run --rm --no-deps -T -v $PWD/scripts:/app/scripts:ro -v $PWD/data:/app/data \\
        kuebiko python scripts/import_severity_axes_jsonl.py data/eval/runs/d1_axes_body.jsonl \\
        --model kuebiko-sft:s24ci65 [--apply]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO if (_REPO / "src").is_dir() else Path("/app")))

from pydantic import ValidationError  # noqa: E402

from src.cti.severity_axes import SeverityAxes  # noqa: E402


def load_rows(path: Path, model: str) -> tuple[list[tuple[str, dict[str, Any]]], int, int]:
    """jsonl → (取り込む行, 別モデルの行数, 検証に落ちた行数)。同じ記事は後の行を採る。"""
    rows: dict[str, dict[str, Any]] = {}
    other = invalid = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("model") != model:
            other += 1
            continue
        try:
            axes = SeverityAxes.model_validate(rec["axes"]).model_dump()
        except (ValidationError, KeyError, TypeError):
            invalid += 1
            continue
        rows[str(rec["article_id"])] = axes
    return list(rows.items()), other, invalid


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("jsonl", type=Path)
    ap.add_argument("--model", required=True, help="取り込む行のモデル名 (本番に記録される名前)")
    ap.add_argument("--apply", action="store_true", help="書き込む (無ければ dry-run)")
    a = ap.parse_args()
    rows, other, invalid = load_rows(a.jsonl, a.model)
    print(f"取り込み対象 {len(rows)} / 別モデル {other} / 検証に落ちた {invalid}")
    if not a.apply:
        print("dry-run — 書くには --apply")
        return
    from src.storage.run_history import RunHistoryRepository

    n = RunHistoryRepository().replace_severity_axes(rows, a.model)
    print(f"書込 {n} 件 (importance_v2 は毎時の段が付け直す)")


if __name__ == "__main__":
    main()
