#!/usr/bin/env python3
"""全教師データセットの分布監査 (2026-09-07)。

背景: pair_judge で教師の False 偏重 (収穫期間の base rate) がそのまま学生の分割癖に
転化した (held-out で base より悪化)。**教師の分布バイアスはモデルの判定方向になる**
ため、全データセットについて (a) ラベル/クラス分布、(b) 本番参照分布との乖離、
(c) 構成の偏り (題材の型) を機械的に洗う。

コンテナ内実行 (DB 参照あり):
    docker exec -e PYTHONPATH=/app -w /app kuebiko python scripts/audit_teacher_datasets.py
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.storage.run_history import RunHistoryRepository  # noqa: E402

_T = Path("data/mlx/teacher")


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            d = json.loads(line)
            if not d.get("skipped"):
                out.append(d)
    return out


def _dist(counter: Counter, total: int) -> str:
    return " / ".join(
        f"{k}:{v} ({100 * v / max(total, 1):.0f}%)" for k, v in counter.most_common(8)
    )


def _db(sql: str) -> list[list]:
    repo = RunHistoryRepository()
    with repo._connect() as conn:  # noqa: SLF001 — 読み取り専用監査
        return [
            list(r.values()) if hasattr(r, "values") else list(r)
            for r in conn.execute(sql).fetchall()
        ]


def audit_triage() -> None:
    rows = _rows(_T / "triage.jsonl")
    c = Counter(json.loads(r["completion"])["importance"] for r in rows)
    print(f"\n== triage ({len(rows)}) ==")
    print(f"  教師分布: {_dist(c, len(rows))}")
    prod = _db(
        "SELECT importance, count(DISTINCT article_id) FROM articles "
        "WHERE created_at > NOW() - INTERVAL '120 days' AND importance IS NOT NULL "
        "GROUP BY importance"
    )
    total = sum(int(r[1]) for r in prod)
    print(f"  本番分布 (120d): {_dist(Counter({r[0]: int(r[1]) for r in prod}), total)}")


def audit_summary() -> None:
    rows = _rows(_T / "article_summary.jsonl")
    imp = Counter(json.loads(r["completion"]).get("importance") for r in rows)
    cat = Counter(json.loads(r["completion"]).get("category") for r in rows)
    print(f"\n== article_summary ({len(rows)}) ==")
    print(f"  importance: {_dist(imp, len(rows))}")
    print(f"  category:   {_dist(cat, len(rows))}")


def audit_pair() -> None:
    rows = _rows(_T / "pair_judge.jsonl")
    lab = Counter(json.loads(r["completion"])["same_event"] for r in rows)
    print(f"\n== pair_judge ({len(rows)}) ==")
    print(f"  ラベル: {_dist(Counter({str(k): v for k, v in lab.items()}), len(rows))}")
    # 構成の偏り: 題材の型ごとの True 率 (脆弱性報道 / ランサム被害列挙 / その他)
    vuln_re = re.compile(r"CVE-|脆弱性|ゼロデイ|0day|KEV|パッチ|vulnerab", re.IGNORECASE)
    ransom_re = re.compile(
        r"ランサム|ransom|被害|victim|: .+\((US|UK|NL|DE|FR|JP|CA|AU)\)", re.IGNORECASE
    )
    strata: dict[str, list[bool]] = {"vuln": [], "ransom": [], "other": []}
    for r in rows:
        p = r["prompt"]
        label = bool(json.loads(r["completion"])["same_event"])
        if vuln_re.search(p):
            strata["vuln"].append(label)
        elif ransom_re.search(p):
            strata["ransom"].append(label)
        else:
            strata["other"].append(label)
    for k, v in strata.items():
        if v:
            print(f"  {k:8s}: {len(v)} 対 / True 率 {100 * sum(v) / len(v):.0f}%")


def audit_event_kind() -> None:
    rows = _rows(_T / "event_kind.jsonl")
    c = Counter(json.loads(r["completion"])["kind"] for r in rows)
    print(f"\n== event_kind ({len(rows)}) ==")
    print(f"  教師分布: {_dist(c, len(rows))}")
    prod = _db("SELECT kind, count(*) FROM article_kinds GROUP BY kind")
    total = sum(int(r[1]) for r in prod)
    print(f"  本番分布 (全期間): {_dist(Counter({r[0]: int(r[1]) for r in prod}), total)}")


def audit_pir_judge() -> None:
    rows = _rows(_T / "pir_judge.jsonl")
    lab = Counter(str(json.loads(r["completion"]).get("matched")) for r in rows)
    pir = Counter(r["key"].split(":")[1] for r in rows)
    print(f"\n== pir_judge ({len(rows)}) ==")
    print(f"  matched: {_dist(lab, len(rows))}")
    print(f"  PIR 上位: {_dist(pir, len(rows))}")


def audit_event() -> None:
    rows = []
    for name in ("train.jsonl", "valid.jsonl"):
        p = Path("data/mlx/dataset") / name
        if p.exists():
            rows += [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
    fills: dict[str, Counter] = {f: Counter() for f in ("caveats", "discrepancies", "unknowns")}
    for r in rows:
        c = json.loads(r["completion"])
        for f in fills:
            fills[f][min(len(c.get(f) or []), 4)] += 1
    print(f"\n== event_news 教師 ({len(rows)}) ==")
    for f, cnt in fills.items():
        zero = cnt.get(0, 0)
        dist = dict(sorted(cnt.items()))
        print(f"  {f:14s}: 0 件率 {100 * zero / max(len(rows), 1):.0f}% / 件数分布 {dist}")


def audit_spotlight2() -> None:
    rows = _rows(_T / "spotlight2.jsonl")
    if not rows:
        print("\n== spotlight2 (収穫中/なし) ==")
        return
    cav = Counter(min(len(json.loads(r["completion"]).get("caveats") or []), 4) for r in rows)
    unk = Counter(min(len(json.loads(r["completion"]).get("unknowns") or []), 4) for r in rows)
    print(f"\n== spotlight2 ({len(rows)}, 収穫途中) ==")
    zero_rate = 100 * cav.get(0, 0) / len(rows)
    print(f"  caveats 件数分布: {dict(sorted(cav.items()))} (0 件率 {zero_rate:.0f}%)")
    print(f"  unknowns 件数分布: {dict(sorted(unk.items()))}")


def main() -> int:
    audit_triage()
    audit_summary()
    audit_pair()
    audit_event_kind()
    audit_pir_judge()
    audit_event()
    audit_spotlight2()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
