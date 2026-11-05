"""T3/T4 の集計を書く (Opus は呼ばない)。

    uv run python -m scripts.n20_audit.report

出力: data/eval/n20_t3/summary.json・data/eval/n20_t4/summary.json と、各 manifest.json
(標本の seed・sha・Opus の回数)。
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.n20_audit import aggregate as ag
from scripts.n20_audit.common import (
    SEED_T3,
    T3_DIR,
    T4_DIR,
    TASKS_T3,
    file_sha,
    read_jsonl,
    write_json,
)


def _by_id(path: Path) -> dict[str, dict[str, Any]]:
    """item_id → 行 (同じ id が複数あれば最後の行)。"""
    return {r["item_id"]: r for r in read_jsonl(path)}


def _opus_used() -> dict[str, Any]:
    """台帳 (opus_ledger.jsonl) の段ごとの課金回数の合計。"""
    total: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in read_jsonl(T3_DIR / "opus_ledger.jsonl"):
        for k in ("opus_calls_charged", "cache_hits", "refused", "failed"):
            total[r["stage"]][k] += int(r.get(k, 0))
    out = {s: dict(v) for s, v in total.items()}
    t3 = sum(v["opus_calls_charged"] for s, v in out.items() if s.startswith("T3"))
    t4 = out.get("T4", {}).get("opus_calls_charged", 0)
    return {"by_stage": out, "T3_total": t3, "T4_total": t4, "all": t3 + t4}


def t3_summary() -> dict[str, Any]:
    p1 = _by_id(T3_DIR / "audit_pass1.jsonl")
    p2 = _by_id(T3_DIR / "audit_pass2.jsonl")
    conf = _by_id(T3_DIR / "confirm.jsonl")
    tasks: dict[str, Any] = {}
    for task in TASKS_T3:
        items = _by_id(T3_DIR / f"items_{task}.jsonl")
        if not items:
            tasks[task] = {"n_items": 0, "blocked": True}
            continue
        mine = set(items)
        tasks[task] = {
            "n_items": len(items),
            "self_agreement": ag.self_agreement(
                items,
                {k: v for k, v in p1.items() if k in mine},
                {k: v for k, v in p2.items() if k in mine},
            ),
            "baseline": ag.baseline(
                items,
                {k: v for k, v in p1.items() if k in mine},
                {k: v for k, v in conf.items() if k in mine},
                seed=SEED_T3,
            ),
        }
    tr1 = _by_id(T4_DIR / "audit_pass1.jsonl")
    tr2 = _by_id(T4_DIR / "audit_pass2.jsonl")
    tasks["translation"] = {"self_agreement": ag.translation_agreement(tr1, tr2)}
    return {"created_at": datetime.now(UTC).isoformat(), "tasks": tasks, "opus": _opus_used()}


def t4_summary() -> dict[str, Any]:
    manifest = json.loads((T4_DIR / "manifest_items.json").read_text(encoding="utf-8"))
    rows = read_jsonl(T4_DIR / "audit_pass1.jsonl")
    return {
        "created_at": datetime.now(UTC).isoformat(),
        "decision": ag.t4_decision(rows, manifest["population_sizes"]),
        "heavy_items": [
            {k: r[k] for k in ("item_id", "kind", "boundary", "reason")}
            for r in rows
            if r["verdict"] == ag.HEAVY_VERDICT
        ],
    }


def _manifest(dir_: Path, extra: dict[str, Any]) -> dict[str, Any]:
    files = sorted(p for p in dir_.glob("*.jsonl"))
    return {
        "created_at": datetime.now(UTC).isoformat(),
        "files": {p.name: {"sha256": file_sha(p), "rows": len(read_jsonl(p))} for p in files},
        **extra,
    }


def main() -> int:
    t3 = t3_summary()
    t4 = t4_summary()
    write_json(T3_DIR / "summary.json", t3)
    write_json(T4_DIR / "summary.json", t4)
    opus = t3["opus"]
    write_json(T3_DIR / "manifest.json", _manifest(T3_DIR, {"seed": SEED_T3, "opus": opus}))
    write_json(T4_DIR / "manifest.json", _manifest(T4_DIR, {"opus": opus}))
    print(json.dumps({"t3": t3, "t4": t4}, ensure_ascii=False, indent=1)[:6000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
