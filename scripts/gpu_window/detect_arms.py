#!/usr/bin/env python3
"""GPU-B (DETECT-JUDGE) の 2 腕: 凍結した入力 (inputs.json) に現行腕・出荷腕を 3 回ずつ当てる。

仕様は docs/replay_prereg/detect_decompose.md (§4・§5.1・§9)。**本番の表には一切書かない**:
DB は read-only セッションの上、detect の部品が使う repo の読み口は ``FrozenRepo`` (凍結した値)
に差し替え、書き込み口 (``_record_detect_ml_shadow``) は無効化して回数だけ数える。

  現行腕 current : kuebiko-sft:s23i     DETECT_DECOMPOSE=shadow (開設は現行経路、分けた形は参考)
  出荷腕 ship    : kuebiko-sft:s24ci65  DETECT_DECOMPOSE=1      (開設・合流は分けた形)

    python scripts/gpu_window/detect_arms.py --dry-run
    docker compose run --rm --no-deps -T kuebiko python scripts/gpu_window/detect_arms.py --limit 10
    docker compose run --rm --no-deps -T kuebiko python scripts/gpu_window/detect_arms.py \\
        --deadline 18:00 --out-dir data/eval/detect_decompose
本番の毎時の LLM ジョブを止めてから (PSTOP-GPU-B)。評価を本番コンテナの中で回さない。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.request
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import detect_attest  # noqa: E402
import detect_common as dc  # noqa: E402
from gpu_window_common import JST, next_clock  # noqa: E402

RUN_TIMEOUT_S = 1500.0  # 1 (窓, run, 腕) の上限 (shadow の予算 120 秒を含む)
GATE_REASON_PREFIX = "claim の固有名詞が記事に無い"


# ---- 純粋部分: 凍結入力 → 本番の部品への入力 ----------------------------------------------


def situation_rows(window: Mapping[str, Any]) -> list[Any]:
    from src.assessment.situation_store import SituationRow

    rows = []
    for d in window["situations"]:
        d = {k: v for k, v in d.items() if k != "claim_type"}
        rows.append(
            SituationRow(
                **{**d, "anchors": frozenset(d["anchors"]), "pir_ids": tuple(d["pir_ids"])}
            )
        )
    return rows


def build_context(window: Mapping[str, Any], pir_context: list[dict[str, str]], llm: Any) -> Any:
    """凍結した窓から ``DetectContext`` を作る (repo は FrozenRepo、store は無し)。"""
    from src.assessment.assignment import build_article_keys, build_situation_keys
    from src.assessment.detect_step import DetectContext
    from src.assessment.standing import STANDING_KIND

    claim_type = {d["situation_id"]: d.get("claim_type", "") for d in window["situations"]}
    sit_keys = [
        build_situation_keys(r, claim_type=claim_type.get(r.situation_id, ""))
        for r in situation_rows(window)
        if r.kind != STANDING_KIND
    ]
    detect_input = list(window["detect_input"])
    entities = {a: set(v) for a, v in window["entities"].items()}
    art_keys = {
        str(a["article_id"]): build_article_keys(
            article_id=str(a["article_id"]),
            title=str(a["title"]),
            entity_keys=frozenset(entities.get(str(a["article_id"]), set())),
        )
        for a in detect_input
    }
    keep = set(window["llm_input_ids"])
    return DetectContext(
        llm_input=[a for a in detect_input if str(a["article_id"]) in keep],
        detect_input=detect_input,
        ml_scores=dict(window["ml_scores"]),
        ml_kinds=dict(window["ml_kinds"]),
        entities_by_id=entities,
        sit_keys=sit_keys,
        art_keys=art_keys,
        repo=dc.FrozenRepo(window),  # type: ignore[arg-type]  # 読み口 4 つだけの代役
        store=None,  # type: ignore[arg-type]  # 凍結入力では読ませない (触れれば失敗する)
        llm=llm,
        active_titles=list(window["active_titles"]),
        pir_context=pir_context,
        period_label=f"{window['start'][:10]} ({window['window_id']} GPU-B)",
        now_iso=window["end"],
    )


def grounding_of(window: Mapping[str, Any], cluster: Sequence[str], claim: str) -> dict[str, Any]:
    """開設した claim の固有名詞の裏付け (実行時の ``claim_grounding.check_grounding``)。"""
    from src.assessment.claim_grounding import check_grounding
    from src.assessment.detect_decompose import _grounding_sources

    arts = window["articles"]
    by_id = {str(a["article_id"]): a for a in window["detect_input"]}
    sources = _grounding_sources(
        by_id,
        {a: str(v.get("summary", "")) for a, v in arts.items()},
        {a: str(v.get("body_prefix", "")) for a, v in arts.items()},
        {a: str(v.get("feed_title", "")) for a, v in arts.items()},
        list(cluster),
    )
    rep = check_grounding(claim, sources)
    return {"violations": list(rep.violations), "attributions": list(rep.attributions)}


def summarize_decision(
    window: Mapping[str, Any], decision: Any, arm: dc.Arm, shadow_records: Sequence[Any]
) -> dict[str, Any]:
    opened = [
        {
            "claim": c.claim,
            "domain": c.domain,
            "article_ids": list(c.article_ids),
            **grounding_of(window, c.article_ids, c.claim),
        }
        for c in decision.detected.open
    ]
    records = list(decision.bundle_records)
    shadow_open = [r for r in shadow_records if r.outcome == "shadow_open"]
    return {
        "variant": decision.bundle_variant,
        "fallback": decision.bundle_variant != arm.expected_variant,
        "opened": opened,
        "n_opened": len(opened),
        "n_violating": sum(1 for o in opened if o["violations"]),
        "merges": {s: list(a) for s, a in decision.merges.items()},
        "n_merged_articles": sum(len(a) for a in decision.merges.values()),
        "n_rejected": len(decision.detected.rejected),
        "n_skipped_clusters": sum(1 for r in records if r.outcome == "skipped"),
        "n_gate_blocked": sum(
            1 for r in records if r.outcome == "skipped" and r.reason.startswith(GATE_REASON_PREFIX)
        ),
        "overflow": decision.detected.overflow,
        "shadow": {
            "n_open": len(shadow_open),
            "n_violating": sum(
                1 for r in shadow_open if "固有名詞の違反" in r.reason and "0 件" not in r.reason
            ),
            "n_records": len(shadow_records),
        },
    }


# ---- 実行 -------------------------------------------------------------------------------


@contextmanager
def frozen_patches(window: Mapping[str, Any], counters: dict[str, int]) -> Any:
    """合流の採点を凍結値に、ML shadow の書き込みを無効 (回数だけ計上) に差し替える。"""
    from src.assessment import detect_decompose as dd
    from src.assessment import stateful

    scores, sids = dc.merge_scores_from_frozen(window)

    async def frozen_score(**_kw: Any) -> Any:
        return scores, sids

    def no_write(**_kw: Any) -> None:
        counters["ml_shadow_write_blocked"] += 1

    with (
        mock.patch.object(dd, "score_merge_pairs", frozen_score),
        mock.patch.object(stateful, "_record_detect_ml_shadow", no_write),
    ):
        yield


async def run_once(
    arm: dc.Arm, window: Mapping[str, Any], run: int, llm: Any, pir: list[dict[str, str]]
) -> dict[str, Any]:
    from src.assessment.detect_step import decide_detect, shadow_records

    os.environ.update(dc.arm_env(arm))
    counters = {"ml_shadow_write_blocked": 0}
    ctx = build_context(window, pir, llm)
    started = time.monotonic()
    rec: dict[str, Any] = {
        "window_id": window["window_id"],
        "run": run,
        "arm": arm.name,
        "model": arm.model,
        "resolved_model": str(getattr(llm, "model", "")),
        "started_at": datetime.now(UTC).isoformat(),
        "n_candidates": len(ctx.llm_input),
        "n_active_titles": len(ctx.active_titles),
    }
    try:
        with frozen_patches(window, counters):
            decision = await asyncio.wait_for(decide_detect(ctx), timeout=RUN_TIMEOUT_S)
            detect_s = time.monotonic() - started
            shadow: list[Any] = []
            if arm.name == "current":
                shadow = await shadow_records(ctx)
        rec.update(summarize_decision(window, decision, arm, shadow))
        rec.update(ok=True, detect_s=round(detect_s, 2))
    except Exception as exc:  # noqa: BLE001 - 1 run の失敗で枠を落とさない (記録して続ける)
        rec.update(ok=False, error=type(exc).__name__)
    rec.update(elapsed_s=round(time.monotonic() - started, 2), **counters)
    return rec


def fetch_tags(base_url: str, timeout: float = 15.0) -> dict[str, str]:
    req = urllib.request.Request(base_url.rstrip("/") + "/api/tags")  # noqa: S310
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        data = json.loads(resp.read().decode("utf-8"))
    return {str(m["name"]): str(m.get("digest", "")) for m in data.get("models", [])}


def select_units(
    windows: Sequence[Mapping[str, Any]], runs: int, limit: int, only: Sequence[str]
) -> list[tuple[str, int, str]]:
    ids = [str(w["window_id"]) for w in windows if not only or w["window_id"] in only]
    units = dc.plan_units(ids, runs)
    return units[:limit] if limit else units


def run_all(args: argparse.Namespace, cert: Mapping[str, Any]) -> int:
    from src.config_loader import load_app_config
    from src.tools.model_tiers import Step, build_llm_for_ref

    dc.install_read_only_env()
    dc.install_read_only_connect()
    inputs_sha = dc.verify_inputs(Path(args.inputs_dir))
    payload = json.loads((Path(args.inputs_dir) / dc.INPUTS_NAME).read_text(encoding="utf-8"))
    windows = {str(w["window_id"]): w for w in payload["windows"]}
    units = select_units(payload["windows"], args.runs, args.limit, args.window_ids)
    cfg = load_app_config()
    llms = {
        a.name: build_llm_for_ref(a.model, Step.SYNTHESIS_DETECT, cfg, timeout_seconds=900.0)
        for a in dc.ARMS
    }
    arms = {a.name: a for a in dc.ARMS}
    tags_before = fetch_tags(cfg.ollama_base_url)
    deadline = next_clock(datetime.now(JST), args.deadline) if args.deadline else None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = Path(args.out_dir) / f"arms_{stamp}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(UTC).isoformat()
    n_ok, hit = 0, False
    with out.open("w", encoding="utf-8") as sink:
        for i, (wid, run, arm_name) in enumerate(units, 1):
            if deadline is not None and datetime.now(JST) >= deadline:
                hit = True
                print(f"[detect-arms] deadline {args.deadline} JST で停止 ({i - 1}/{len(units)})")
                break
            rec = asyncio.run(
                run_once(arms[arm_name], windows[wid], run, llms[arm_name], payload["pir_context"])
            )
            sink.write(json.dumps(rec, ensure_ascii=False) + "\n")
            sink.flush()
            n_ok += 1 if rec["ok"] else 0
            print(f"  {i}/{len(units)} {wid} r{run} {arm_name} ok={rec['ok']} {rec['elapsed_s']}s")
    tags_after = fetch_tags(cfg.ollama_base_url)
    write_manifest(
        out, cert, inputs_sha, tags_before, tags_after, started_at, len(units), n_ok, hit, args
    )
    return 0 if n_ok == len(units) and not hit else 1


def write_manifest(
    out: Path,
    cert: Mapping[str, Any],
    inputs_sha: str,
    before: Mapping[str, str],
    after: Mapping[str, str],
    started_at: str,
    n_units: int,
    n_ok: int,
    deadline_hit: bool,
    args: argparse.Namespace,
) -> None:
    from scripts.replay._manifest import assert_no_body
    from src.assessment.assign_gate import gate_mode

    manifest = {
        "prereg_path": cert["prereg_path"],
        "prereg_git_sha": cert["prereg_git_sha"],
        "prereg_blob_sha256": cert["prereg_blob_sha256"],
        "head_sha": cert["head_sha"],
        "inputs_sha256": inputs_sha,
        "detect_model_sha256": dc.verify_detect_model(),
        "tools_sha256": detect_attest.tool_shas(),
        "code_files_sha256": dc.file_shas(Path("."), dc.CODE_FILES),
        "arms": {a.name: {"model": a.model, "DETECT_DECOMPOSE": a.decompose_mode} for a in dc.ARMS},
        "digests_start": {a.model: before.get(a.model, "") for a in dc.ARMS},
        "digests_end": {a.model: after.get(a.model, "") for a in dc.ARMS},
        "flags": {
            "ASSIGN_EMBED_GATE": gate_mode(),
            "DETECT_ML_PREFILTER": os.environ.get("DETECT_ML_PREFILTER", ""),
            "LLM_USAGE_LOCAL": os.environ.get("LLM_USAGE_LOCAL", ""),
        },
        "seed": dc.SEED,
        "runs_per_arm": args.runs,
        "limit": args.limit,
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "n_units": n_units,
        "n_ok": n_ok,
        "deadline_hit": deadline_hit,
        "output": out.name,
    }
    assert_no_body(manifest)
    out.with_suffix(".manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )


def dry_run(args: argparse.Namespace) -> int:
    """計画の表示。inputs があれば sha 検証と read-only の確認もする (LLM には触れない)。"""
    print("[detect-arms] dry-run (LLM に触れない)")
    print(
        f"  prereg={dc.PREREG_NAME} 乱数種={dc.SEED} 窓<={dc.N_WINDOWS}"
        f" x 腕 {len(dc.ARMS)} x run {args.runs}"
    )
    for a in dc.ARMS:
        print(
            f"  腕 {a.name}: {a.model} DETECT_DECOMPOSE={a.decompose_mode}"
            f" (採用 variant={a.expected_variant})"
        )
    dc.verify_detect_model()
    dc.install_read_only_env()
    if os.environ.get("DATABASE_URL"):
        dc.install_read_only_connect()
        from src.storage import db_backend

        conn = db_backend.connect()
        try:
            dc.assert_read_only_setting(
                next(iter(conn.execute("SHOW default_transaction_read_only").fetchone().values()))
            )
            print("  DB: read-only セッション確認済み (SHOW default_transaction_read_only = on)")
        finally:
            conn.close()
    else:
        print("  DB: DATABASE_URL なし (凍結入力のみで走るので不要)")
    path = Path(args.inputs_dir) / dc.INPUTS_NAME
    if not path.is_file():
        print(f"  inputs: まだ無い ({path})。detect_snapshot.py を先に")
        return 0
    sha = dc.verify_inputs(Path(args.inputs_dir))
    payload = json.loads(path.read_text(encoding="utf-8"))
    units = select_units(payload["windows"], args.runs, args.limit, args.window_ids)
    print(f"  inputs sha256={sha[:12]} 窓 {len(payload['windows'])} / 実行単位 {len(units)}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--inputs-dir", default=str(dc.OUT_DIR))
    ap.add_argument("--out-dir", default=str(dc.OUT_DIR))
    ap.add_argument("--attestation", default=str(dc.OUT_DIR / detect_attest.ATTESTATION_NAME))
    ap.add_argument("--runs", type=int, default=dc.RUNS_PER_ARM)
    ap.add_argument("--limit", type=int, default=0, help="先頭 N 単位だけ (初回 10 件の実測用)")
    ap.add_argument("--window-ids", nargs="*", default=[], help="窓を限る (smoke 用)")
    ap.add_argument("--deadline", default="", help="JST の HH:MM。過ぎたら次の単位へ進まず停止")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    if args.dry_run:
        return dry_run(args)
    cert = detect_attest.check_attestation(Path(args.attestation))
    return run_all(args, cert)


if __name__ == "__main__":
    raise SystemExit(main())
