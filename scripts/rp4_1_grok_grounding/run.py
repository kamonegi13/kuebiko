"""RP-4-1: GROK_SUMMARY_GROUNDING の判定 (prereg docs/replay_prereg/grok_summary_grounding.md)。

    uv run python -m scripts.rp4_1_grok_grounding.run --phase sample --out <dir>   # LLM なし
    uv run python -m scripts.rp4_1_grok_grounding.run --phase judge  --out <dir>   # Opus (上限 39)

DB は docker exec postgres で read-only セッションの SELECT のみ。出力に本文・要約は書かない。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from scripts.replay._closure import R18_VERSION
from scripts.rp4_1_grok_grounding import logic

DATA_ROOT = Path("/Users/juzo1192/git/auto-post")
PREREG = "grok_summary_grounding"
ITEM = "RP-4-1"
ESTIMATE = 30
MAX_TOKENS = 1500
CLAIM_SENTINEL_LEN = 60
EXCL_FILE = "data/eval/grok_link_fetch_2026_10_24/sample.json"
SELECT_COLS = "id, article_id, url, title, body, summary, remediation, published_at, created_at"
RUBRIC = """あなたは CTI 部門の上級分析官です。X (旧 Twitter) の投稿の本文と、
それを要約した日本語の「要約」「対処」を示します。
要約・対処に書かれた事実のうち、本文 (題を含む) に根拠が無い、または本文と矛盾するものを探します。
数字・日付・版番号・CVE・金額・件数・攻撃者の帰属・組織名を特に確認してください。
言い換え・単位の換算 (万・億など)・要約として自然な省略は誤りとしません。
heavy = 本文に無い版・日付・CVE・金額・件数の補完、本文と矛盾する帰属、捏造。
minor = 言い回しの飛躍、本文から推測できる補足。
verdict: none = 根拠の無い主張なし / minor_only = minor のみ / heavy = heavy が 1 つ以上。"""
QUESTION = "この要約と対処の中に、本文に根拠の無い、または矛盾する主張はありますか。"


class UnsupportedClaim(BaseModel):
    model_config = ConfigDict(extra="ignore")
    claim: str
    kind: Literal["number", "date", "actor", "other"]
    severity: Literal["heavy", "minor"]


class GroundingVerdict(BaseModel):
    model_config = ConfigDict(extra="ignore")
    unsupported_claims: list[UnsupportedClaim] = Field(default_factory=list)
    verdict: Literal["none", "minor_only", "heavy"]
    confidence: Literal["high", "medium", "low"]
    reason: str


def psql_json(sql: str) -> list[dict[str, Any]]:
    """read-only セッションで SELECT を実行し、1 行 1 JSON で返す。"""
    from scripts.replay._sqlguard import assert_read_only_sql

    assert_read_only_sql(sql)
    wrapped = f"SELECT row_to_json(t) FROM ({sql}) t"
    out = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "docker", "exec", "-e", "PGOPTIONS=-c default_transaction_read_only=on",
            "postgres", "psql", "-U", "kuebiko", "-d", "kuebiko", "-At", "-c", wrapped,
        ],  # fmt: skip
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    return [json.loads(x) for x in out.splitlines() if x.strip()]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fetch_grok_rows() -> list[dict[str, Any]]:
    return psql_json(f"SELECT {SELECT_COLS} FROM articles WHERE body_source = 'grok'")


def _in_list(ids: list[str]) -> str:
    return ",".join("'" + i.replace("'", "''") + "'" for i in ids)


def r18_excluded(
    population: list[dict[str, Any]], all_rows: list[dict[str, Any]]
) -> tuple[set[str], dict[str, int]]:
    """gold:grok_link_44 と同じ閉包の成分に入る母集団の status_id (prereg §7)。"""
    import numpy as np

    from scripts.replay._closure import build_components, resolve_roots

    tainted_sids = {
        logic.status_id_of(r["url"]) for r in json.loads((DATA_ROOT / EXCL_FILE).read_text("utf-8"))
    } - {None}
    tainted_rows = [r for r in all_rows if logic.status_id_of(r["url"]) in tainted_sids]
    pop_ids = {r["article_id"]: r["status_id"] for r in population}
    tainted_ids = {r["article_id"] for r in tainted_rows}
    ids = sorted(set(pop_ids) | tainted_ids)
    if not tainted_ids:
        return set(), {"tainted_posts": len(tainted_sids), "tainted_rows": 0, "excluded": 0}
    body_of = {r["article_id"]: r["body"] for r in all_rows if r["article_id"] in set(ids)}
    cves = {a: logic.cves_of(body_of.get(a, "")) for a in ids}
    mem = psql_json(
        f"SELECT article_id, item_id FROM event_item_members WHERE article_id IN ({_in_list(ids)})"
    )
    merged = {
        r["id"]: r["merged_into"] for r in psql_json("SELECT id, merged_into FROM event_items")
    }
    roots = resolve_roots(merged)
    root_of = {m["article_id"]: roots.get(m["item_id"], m["item_id"]) for m in mem}
    emb_rows = psql_json(
        f"SELECT article_id, vector FROM summary_embeddings WHERE article_id IN ({_in_list(ids)})"
    )
    emb = {
        e["article_id"]: np.frombuffer(bytes.fromhex(e["vector"][2:]), dtype=np.float32).tolist()
        for e in emb_rows
    }
    comp = build_components(ids, root_of=root_of, cves=cves, victim_orgs={}, embeddings=emb)
    bad = {comp[a] for a in tainted_ids}
    drop = {sid for aid, sid in pop_ids.items() if comp[aid] in bad}
    return drop, {
        "tainted_posts": len(tainted_sids),
        "tainted_rows": len(tainted_rows),
        "excluded": len(drop),
        "with_embedding": len(emb),
        "with_event_root": len(root_of),
    }


def phase_sample(out: Path, gate: dict[str, str]) -> int:
    all_rows = fetch_grok_rows()
    pop, funnel = logic.fold_population(all_rows)
    drop, r18 = r18_excluded(pop, all_rows)
    pop = [r for r in pop if r["status_id"] not in drop]
    reasons = {r["status_id"]: logic.fire_reasons(r) for r in pop}
    fired = [s for s, v in reasons.items() if v]
    nofire = [s for s, v in reasons.items() if not v]
    fired_s, nofire_s = logic.draw_sample(fired, nofire)
    by_sid = {r["status_id"]: r for r in pop}
    kinds = Counter(k for s in fired for k in logic.reason_kinds(reasons[s]))
    per_day = Counter(str(logic.posted_at(s) + logic.JST)[:10] for s in by_sid)
    iv = logic.precision_interval(len(fired), len(pop))
    population = {
        **funnel.__dict__,
        "r18": r18,
        "r18_version": R18_VERSION,
        "r18_dropped": len(drop),
        "population_after_r18": len(pop),
        "fired": len(fired),
        "fire_rate": iv.point,
        "fire_rate_low": iv.low,
        "fire_rate_high": iv.high,
        "fired_by_kind": dict(kinds),
        "posts_per_day_jst": dict(sorted(per_day.items())),
    }
    out.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "status_id": s,
            "article_id": by_sid[s]["article_id"],
            "arm": "fired" if s in fired_s else "nofire",
            "reason_kinds": logic.reason_kinds(reasons[s]),
            "n_reasons": len(reasons[s]),
        }
        for s in sorted(fired_s + nofire_s, key=int)
    ]
    (out / "sample.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), "utf-8"
    )
    (out / "population.json").write_text(
        json.dumps({**population, "gate": gate}, ensure_ascii=False, indent=1) + "\n", "utf-8"
    )
    print(json.dumps(population, ensure_ascii=False, indent=1))
    return 0


def _inputs(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "題": row["title"] or "",
        "本文": row["body"],
        "要約": row["summary"],
        "対処": row["remediation"] or "",
    }


async def _run_judge(
    sample: list[dict[str, Any]], out: Path, budget: Any
) -> tuple[list[str], str | None]:
    from scripts.graphrag_eval.common import BudgetExceededError, make_judge

    mlx = str(DATA_ROOT / "data" / "mlx")
    if mlx not in sys.path:
        sys.path.insert(0, mlx)
    judge = make_judge(GroundingVerdict, RUBRIC, budget, max_tokens=MAX_TOKENS)
    all_rows = {logic.status_id_of(r["url"]): r for r in fetch_grok_rows()}
    path = out / "judgments.jsonl"
    done = (
        {json.loads(x)["status_id"] for x in path.read_text("utf-8").splitlines() if x.strip()}
        if path.exists()
        else set()
    )
    failed: list[str] = []
    stopped: str | None = None
    order = logic.presentation_order([s["status_id"] for s in sample])
    meta = {s["status_id"]: s for s in sample}

    async def one(sid: str) -> None:
        v = await judge(QUESTION, _inputs(all_rows[sid]))
        if not isinstance(v, GroundingVerdict):
            failed.append(sid)
            return
        rec = {
            "status_id": sid,
            "arm": meta[sid]["arm"],
            "reason_kinds": meta[sid]["reason_kinds"],
            "verdict": v.verdict,
            "confidence": v.confidence,
            "n_claims": len(v.unsupported_claims),
            "claim_kinds": sorted({c.kind for c in v.unsupported_claims}),
            "heavy_kinds": sorted({c.kind for c in v.unsupported_claims if c.severity == "heavy"}),
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    pending = [s for s in order if s not in done]
    fired_first = [s for s in pending if meta[s]["arm"] == "fired"]
    rest = [s for s in pending if meta[s]["arm"] != "fired"]
    try:
        await asyncio.gather(*[one(s) for s in fired_first])
        if budget.used <= logic.NOFIRE_CONTINUE_MAX_USED:
            await asyncio.gather(*[one(s) for s in rest])
    except BudgetExceededError as exc:
        stopped = str(exc)
    return failed, stopped


def summarize(path: Path, population: dict[str, Any], sample_n: int, failed: int) -> dict[str, Any]:
    rows = [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]
    fired = [r for r in rows if r["arm"] == "fired"]
    errors = sum(logic.is_error(r["verdict"]) for r in fired)
    heavy = sum(r["verdict"] == "heavy" for r in fired)
    decision, detail = logic.decide(
        population=population["population_after_r18"], fired=population["fired"],
        judged=len(fired), errors=errors, failed=failed, sample_n=sample_n,
    )  # fmt: skip
    hv = logic.precision_interval(heavy, len(fired))
    by_kind = {
        k: {
            "n": sum(k in r["reason_kinds"] for r in fired),
            "errors": sum(k in r["reason_kinds"] and logic.is_error(r["verdict"]) for r in fired),
        }
        for k in sorted({k for r in fired for k in r["reason_kinds"]})
    }
    nofire = [r for r in rows if r["arm"] == "nofire"]
    return {
        "decision": decision,
        **detail,
        "fired_judged": len(fired),
        "fired_errors": errors,
        "fired_heavy": heavy,
        "heavy_precision": hv.point,
        "heavy_low": hv.low,
        "heavy_high": hv.high,
        "by_reason_kind": by_kind,
        "nofire_judged": len(nofire),
        "nofire_heavy": sum(r["verdict"] == "heavy" for r in nofire),
        "nofire_any_error": sum(logic.is_error(r["verdict"]) for r in nofire),
        "verdict_dist_fired": dict(Counter(r["verdict"] for r in fired)),
    }


def phase_judge(out: Path, gate: dict[str, str]) -> int:
    from scripts.graphrag_eval.common import OpusBudget, now_iso, write_json

    sample = [
        json.loads(x) for x in (out / "sample.jsonl").read_text("utf-8").splitlines() if x.strip()
    ]
    population = json.loads((out / "population.json").read_text("utf-8"))
    budget = OpusBudget(ITEM, ESTIMATE)
    print(f"標本 {len(sample)} / 見積もり {budget.estimate} / 上限 {budget.limit}")
    failed, stopped = asyncio.run(_run_judge(sample, out, budget))
    jpath = out / "judgments.jsonl"
    if not jpath.exists():
        print("判定なし", stopped)
        return 2
    summary = summarize(jpath, population, len(sample), len(failed))
    write_json(out / "summary.json", summary)
    manifest = {
        **gate,
        "item": ITEM,
        "judge": "claude-opus-5-5",
        "sample": {"path": str(out / "sample.jsonl"), "sha256": sha256(out / "sample.jsonl")},
        "gold": {"path": str(jpath), "sha256": sha256(jpath)},
        "opus_calls_bridge": budget.used,
        "budget_limit": budget.limit,
        "failed": failed,
        "stopped": stopped,
        "finished_at": now_iso(),
    }
    write_json(out / "manifest.json", manifest)
    ledger = DATA_ROOT / "data/eval/adjudication_ledger.jsonl"
    row = {
        "time": now_iso(), "item": ITEM, "judge": "claude-opus-5-5",
        "rubric": "grok-grounding-v1", "opus_calls_charged": budget.used,
        "budget_limit": ESTIMATE, "call_cap": budget.limit,
        "n_done": len(summary_rows(jpath)), "n_failed": len(failed),
    }  # fmt: skip
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print("Opus", budget.used, "失敗", len(failed), "停止", stopped)
    return 0 if stopped is None else 2


def summary_rows(path: Path) -> list[str]:
    return [x for x in path.read_text("utf-8").splitlines() if x.strip()]


def main() -> int:
    from scripts.graphrag_eval.common import ROOT, require_prereg

    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=("sample", "judge"), required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    gate = require_prereg(PREREG, Path(__file__), args.out if args.out.exists() else ROOT)
    print(json.dumps(gate, ensure_ascii=False))
    if args.phase == "sample":
        return phase_sample(args.out, gate)
    return phase_judge(args.out, gate)


if __name__ == "__main__":
    sys.exit(main())
