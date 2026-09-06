#!/usr/bin/env python3
"""spotlight 教師対の識別子オフライン関門 (repo 環境で実行する前処理)。

収穫は SPOTLIGHT_IDENTIFIER_GATE=0 で行う (書き直し指示がプロンプト形を汚すため)。
その代わりここで教師出力 (headline+outlook) をプロンプトと対称照合し、出典不支持の
識別子を含む対を除外する。教師 (Opus) にも低頻度の転記破損はあり得る前提。

使用例: uv run python scripts/filter_spotlight_teacher.py \\
            --src data/mlx/teacher/spotlight.jsonl --out data/mlx/teacher/spotlight_ok.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.spotlight.identifier_check import find_unsupported_identifiers  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", type=Path, default=Path("data/mlx/teacher/spotlight.jsonl"))
    ap.add_argument("--out", type=Path, default=Path("data/mlx/teacher/spotlight_ok.jsonl"))
    args = ap.parse_args()

    kept = rejected = 0
    seen: set[str] = set()
    with args.out.open("w", encoding="utf-8") as fh:
        for line in args.src.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            d = json.loads(line)
            if d.get("skipped") or not d.get("prompt"):
                continue
            if d.get("key") in seen:  # 煙試験の二重合流などの重複 key を除去
                continue
            seen.add(d.get("key", ""))
            payload = json.loads(d["completion"])
            text = f"{payload.get('headline', '')}\n{payload.get('outlook', '')}"
            bad = find_unsupported_identifiers(text, d["prompt"])
            if bad:
                rejected += 1
                print(f"  除外 {d.get('key')}: {[i.raw for i in bad][:5]}")
                continue
            fh.write(line + "\n")
            kept += 1
    print(f"採用 {kept} / 識別子関門で除外 {rejected} → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
