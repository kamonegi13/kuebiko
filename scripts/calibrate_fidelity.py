#!/usr/bin/env python3
"""固有情報の網羅率 (src/eventnews/fidelity.py) を要点照合で較正する (2026-09-26)。

問い: LLM を使わない簡易な指標が、要点照合 (Opus のシート × Sonnet の判定) の被覆率と連動するか。
見るのは 3 つ:
1. 全体の順位相関 (出力 × 窓)
2. **同じ窓で 2 つの出力を比べたとき、差の向きが一致する割合** (本命 — 実用はモデル・版の比較)
3. 出力ごとの平均の並び

    uv run python scripts/calibrate_fidelity.py
"""

from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path
from statistics import mean
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from scripts.keyfact_lib import render_draft, source_block  # noqa: E402
from src.eventnews.fidelity import entity_coverage  # noqa: E402

_ARMS = {
    "M30": ("data/mlx/eval_ollama_n17m30_v2.json", "ollama_generated"),
    "n18": ("data/mlx/eval_ollama_n18.json", "ollama_generated"),
    "K": ("data/mlx/eval_ollama_n17m30_K.json", "ollama_generated"),
    "Opus教師": ("data/mlx/eval_sft.json", "reference"),
    "M30旧": ("data/mlx/eval_ollama_n17m30.json", "ollama_generated"),
    "n17c": ("data/mlx/eval_ollama_n17c.json", "ollama_generated"),
}


def _rank(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2
        i = j + 1
    return ranks


def spearman(a: list[float], b: list[float]) -> float:
    ra, rb = _rank(a), _rank(b)
    ma, mb = mean(ra), mean(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb, strict=True))
    va = sum((x - ma) ** 2 for x in ra) ** 0.5
    vb = sum((y - mb) ** 2 for y in rb) ** 0.5
    return cov / (va * vb) if va and vb else 0.0


def main() -> int:
    cov = json.loads(Path("data/mlx/keyfact_coverage_v1.json").read_text(encoding="utf-8"))
    prompts = json.loads(Path("data/mlx/eval_sft.json").read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    for arm, (path, field) in _ARMS.items():
        if arm not in cov:
            continue
        outputs = json.loads(Path(path).read_text(encoding="utf-8"))
        by_window: dict[int, list[bool]] = {}
        for r in cov[arm]:
            by_window.setdefault(int(r["window"]), []).append(bool(r["present"]))
        for w, pres in by_window.items():
            summary = render_draft(outputs[w].get(field))
            fid = entity_coverage(source_block(prompts[w]["prompt"]), summary)
            if fid.ratio is None:
                continue
            rows.append({"arm": arm, "w": w, "fid": fid.ratio, "kf": sum(pres) / len(pres)})

    print(f"標本 (出力 × 窓): {len(rows)}")
    print(f"1. 全体の順位相関: {spearman([r['fid'] for r in rows], [r['kf'] for r in rows]):.3f}")
    agree = total = 0
    for w in {r["w"] for r in rows}:
        same = [r for r in rows if r["w"] == w]
        for a, b in itertools.combinations(same, 2):
            dk, df = a["kf"] - b["kf"], a["fid"] - b["fid"]
            if dk == 0:
                continue  # 要点照合で差が無い組は判定しない
            total += 1
            agree += (dk > 0) == (df > 0) and df != 0
    print(
        f"2. 同じ窓の 2 出力で差の向きが一致: {agree}/{total} = {agree / max(1, total):.1%}"
        " (偶然なら約 50%、指標が同点のときは不一致に数える)"
    )
    # 同点を除き、指標に差が出た組だけで向きの正しさを見る (差が大きいほど当たるか)
    for thr in (0.0, 0.05, 0.10):
        ok = n = 0
        for w in {r["w"] for r in rows}:
            for a, b in itertools.combinations([r for r in rows if r["w"] == w], 2):
                dk, df = a["kf"] - b["kf"], a["fid"] - b["fid"]
                if dk == 0 or abs(df) <= thr:
                    continue
                n += 1
                ok += (dk > 0) == (df > 0)
        print(f"   指標の差が {thr:.0%} 超の組 {n} 件: 向き一致 {ok / max(1, n):.1%}")
    print("3. 出力ごとの平均 (要点照合 / 固有情報):")
    for arm in _ARMS:
        sel = [r for r in rows if r["arm"] == arm]
        if sel:
            kf, fid = mean(r["kf"] for r in sel), mean(r["fid"] for r in sel)
            print(f"   {arm:8s} {kf:.1%} / {fid:.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
