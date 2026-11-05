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

dc.install_read_only_env()  # 以降の import が接続しても read-only (PGOPTIONS)
from gpu_window_common import JST, next_clock  # noqa: E402

RUN_TIMEOUT_S = 1500.0  # 1 (窓, run, 腕) の上限 (shadow の予算 120 秒を含む)
GATE_REASON_PREFIX = "claim の固有名詞が記事に無い"
FIDELITY_REASON_PREFIX = "claim が根拠どおりでない"  # detect_decompose (DETECT_WRITER_GATES)
ONE_OFF_PREFIX = "単発の事案"


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
        # 書き手の直し (DETECT_WRITER_GATES=1) で止めた claim・塊 (rejudge P3 の参考)
        "n_fidelity_blocked": sum(
            1
            for r in records
            if r.outcome == "skipped" and r.reason.startswith(FIDELITY_REASON_PREFIX)
        ),
        "n_one_off_skipped": sum(
            1 for r in records if r.outcome == "skipped" and r.reason.startswith(ONE_OFF_PREFIX)
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


FAILURE_NAMES_MAX = 8


@contextmanager
def log_spy() -> Any:
    """detect の部品の握りつぶす except が出す warning を拾う (空の成功に見せないため)。"""
    from src.assessment import detect_decompose, detect_step, stateful
    from src.synthesis.grounded import incremental

    spy = dc.LogSpy()
    with (
        mock.patch.object(detect_step, "_log", spy),
        mock.patch.object(detect_decompose, "_log", spy),
        mock.patch.object(stateful, "_log", spy),
        mock.patch.object(incremental, "_log", spy),
    ):
        yield spy


def classify_failure(rec: Mapping[str, Any], failures: Sequence[str]) -> str:
    """握りつぶされた失敗があり、開設が 0 件なら「空の成功」ではなく失敗にする。"""
    if failures and not rec.get("n_opened") and not rec.get("n_merged_articles"):
        return "swallowed:" + ",".join(sorted(set(failures))[:FAILURE_NAMES_MAX])
    return ""


async def run_once(
    arm: dc.Arm,
    window: Mapping[str, Any],
    run: int,
    llm: Any,
    pir: list[dict[str, str]],
    timeout_s: float = RUN_TIMEOUT_S,
) -> dict[str, Any]:
    """1 (窓, run, 腕)。例外・タイムアウトは ok=False + 型名で記録し、空の成功にしない。"""
    from src.assessment.detect_step import decide_detect, shadow_records

    os.environ.update(dc.arm_env(arm))
    counters = {"ml_shadow_write_blocked": 0}
    started = time.monotonic()
    rec: dict[str, Any] = {
        "window_id": window["window_id"],
        "run": run,
        "arm": arm.name,
        "model": arm.model,
        "resolved_model": str(getattr(llm, "model", "")),
        "started_at": datetime.now(UTC).isoformat(),
    }
    failures: list[str] = []
    try:
        ctx = build_context(window, pir, llm)
        rec.update(n_candidates=len(ctx.llm_input), n_active_titles=len(ctx.active_titles))
        with frozen_patches(window, counters), log_spy() as spy:
            decision = await asyncio.wait_for(decide_detect(ctx), timeout=timeout_s)
            detect_s = time.monotonic() - started
            shadow: list[Any] = []
            if arm.name == "current":
                left = max(1.0, timeout_s - detect_s)
                shadow = await asyncio.wait_for(shadow_records(ctx), timeout=left)
            failures = spy.failures()
        rec.update(summarize_decision(window, decision, arm, shadow))
        rec.update(ok=True, detect_s=round(detect_s, 2))
        rec["written_claims"] = rec["n_opened"] + rec["n_gate_blocked"]
        bad = classify_failure(rec, failures)
        if bad:
            rec.update(ok=False, error=bad)
    except Exception as exc:  # noqa: BLE001 - 1 run の失敗で枠を落とさない (型名を記録して続ける)
        rec.update(ok=False, error=type(exc).__name__)
    rec.update(swallowed_failures=sorted(set(failures)), **counters)
    rec["elapsed_s"] = round(time.monotonic() - started, 2)
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


def preflight(cert: Mapping[str, Any]) -> dict[str, Any]:
    """DB が read-only であること、本番の旗と同じであること。違えば実行しない。"""
    import src.storage.db_backend as backend

    raw_connect = backend.connect  # install_read_only_connect の前の素の接続
    route = dc.prefer_read_only_role(os.environ)
    dc.install_read_only_connect()
    checks = dc.verify_read_only_runtime(raw_connect, backend.connect)
    flags = dc.runtime_flags(os.environ)
    prod = dict(cert.get("prod_flags") or {})
    diffs = dc.flag_mismatches(prod, flags) if prod else ["prod_flags が証明書に無い"]
    if diffs:
        raise SystemExit("本番コンテナの旗と食い違う (実行しない): " + "; ".join(diffs))
    return {"db_route": route, "read_only_checks": checks, "flags": flags, "prod_flags": prod}


def resolved_ship_model() -> str:
    """S = step の上書きを外した先 (synthesis_detect のティアの今の割当)。"""
    from src.tools.model_tiers import STEP_REGISTRY, Step, resolve_tier_model

    return resolve_tier_model(STEP_REGISTRY[Step.SYNTHESIS_DETECT].tier)


def check_ship_model(resolved: str, ship: dc.Arm, *, required: bool) -> None:
    """rejudge §5.1: S の解決先が出荷腕のモデルでなければ実行しない。"""
    if required and resolved != ship.model:
        raise SystemExit(f"S の解決先 {resolved!r} が出荷腕 {ship.model!r} と違う (実行しない)")


def model_tiers_version() -> int | None:
    import src.storage.db_backend as backend

    conn = backend.connect()
    try:
        row = conn.execute(
            "SELECT MAX(version) AS v FROM app_config_versions WHERE config_key = ?",
            ("model_tiers",),
        ).fetchone()
        v = next(iter(row.values())) if row is not None and hasattr(row, "values") else None
        return int(v) if v is not None else None
    finally:
        conn.close()


async def run_units(
    units: Sequence[tuple[str, int, str]],
    windows: Mapping[str, Mapping[str, Any]],
    llms: Mapping[str, Any],
    pir: list[dict[str, str]],
    sink: Any,
    deadline: datetime | None,
    on_record: Any,
) -> tuple[int, bool]:
    """全単位を 1 つのイベントループで回す (LLM client の再利用で loop が閉じる事故を避ける)。"""
    arms = {a.name: a for a in dc.ARMS}
    n_ok, hit = 0, False
    for i, (wid, run, arm_name) in enumerate(units, 1):
        left = None if deadline is None else (deadline - datetime.now(JST)).total_seconds()
        if left is not None and left <= 0:
            hit = True
            print(f"[detect-arms] deadline で停止 ({i - 1}/{len(units)})")
            break
        timeout_s = dc.unit_timeout(RUN_TIMEOUT_S, left)
        rec = await run_once(arms[arm_name], windows[wid], run, llms[arm_name], pir, timeout_s)
        sink.write(json.dumps(rec, ensure_ascii=False) + "\n")
        sink.flush()
        on_record(rec)
        n_ok += 1 if rec["ok"] else 0
        err = rec.get("error", "")
        print(
            f"  {i}/{len(units)} {wid} r{run} {arm_name} ok={rec['ok']} {rec['elapsed_s']}s {err}"
        )
    return n_ok, hit


def run_all(args: argparse.Namespace, cert: Mapping[str, Any]) -> int:
    from src.config_loader import load_app_config
    from src.tools.model_tiers import Step, build_llm_for_ref

    pre = preflight(cert)
    check_ship_model(resolved_ship_model(), dc.SHIP_ARM, required=dc.PROFILE.check_ship_resolution)
    inputs_sha = dc.verify_inputs(Path(args.inputs_dir))
    payload = json.loads((Path(args.inputs_dir) / dc.INPUTS_NAME).read_text(encoding="utf-8"))
    windows = {str(w["window_id"]): w for w in payload["windows"]}
    units = select_units(payload["windows"], args.runs, args.limit, args.window_ids)
    cfg = load_app_config()
    llms = {
        a.name: build_llm_for_ref(a.model, Step.SYNTHESIS_DETECT, cfg, timeout_seconds=900.0)
        for a in dc.ARMS
    }
    tags_before = fetch_tags(cfg.ollama_base_url)
    deadline = next_clock(datetime.now(JST), args.deadline) if args.deadline else None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = Path(args.out_dir) / f"arms_{stamp}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    base = base_manifest(cert, inputs_sha, payload, pre, tags_before, args, len(units))
    write_manifest(out, {**base, "status": "running"})  # 開始時にも書く (途中で死んでも残る)
    records: list[dict[str, Any]] = []
    with out.open("w", encoding="utf-8") as sink:
        n_ok, hit = asyncio.run(
            run_units(units, windows, llms, payload["pir_context"], sink, deadline, records.append)
        )
    tags_after = fetch_tags(cfg.ollama_base_url)
    final = {
        **base,
        "status": "deadline" if hit else "finished",
        "finished_at": datetime.now(UTC).isoformat(),
        "digests_end": {a.model: tags_after.get(a.model, "") for a in dc.ARMS},
        "n_ok": n_ok,
        "deadline_hit": hit,
        "p3": dc.p3_gate_share(records),
    }
    write_manifest(out, final)
    return 0 if n_ok == len(units) and not hit else 1


def base_manifest(
    cert: Mapping[str, Any],
    inputs_sha: str,
    payload: Mapping[str, Any],
    pre: Mapping[str, Any],
    tags_before: Mapping[str, str],
    args: argparse.Namespace,
    n_units: int,
) -> dict[str, Any]:
    from scripts.replay._closure import R18_VERSION

    meta = dict(payload.get("meta", {}))
    return {
        "prereg_path": cert["prereg_path"],
        "prereg_git_sha": cert["prereg_git_sha"],
        "prereg_blob_sha256": cert["prereg_blob_sha256"],
        "head_sha": cert["head_sha"],
        "inputs_sha256": inputs_sha,
        "inputs_meta": {k: meta.get(k) for k in ("seed", "frozen_at", "excluded", "deviations")},
        "r18": {"version": R18_VERSION, "occupancy": None, "note": "道具は閉包を計算しない (別段)"},
        "detect_model_sha256": dc.verify_detect_model(),
        "model_tiers_version": model_tiers_version(),
        "tools_sha256": detect_attest.tool_shas(),
        "code_files_sha256": dc.file_shas(Path("."), dc.CODE_FILES),
        "profile": dc.PROFILE.name,
        "arms": {a.name: {"model": a.model, **dc.arm_env(a)} for a in dc.ARMS},
        "ship_model_resolved": resolved_ship_model(),
        "digests_start": {a.model: tags_before.get(a.model, "") for a in dc.ARMS},
        "flags": pre["flags"],
        "prod_flags": pre["prod_flags"],
        "db_route": pre["db_route"],
        "read_only_checks": pre["read_only_checks"],
        "seed": dc.SEED,
        "runs_per_arm": args.runs,
        "limit": args.limit,
        "started_at": datetime.now(UTC).isoformat(),
        "n_units": n_units,
    }


def write_manifest(out: Path, manifest: Mapping[str, Any]) -> None:
    from scripts.replay._manifest import assert_no_body

    assert_no_body(manifest)
    path = out.with_suffix(".manifest.json")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def dry_run(args: argparse.Namespace) -> int:
    """計画の表示。DB の read-only、旗、inputs の sha を確かめる (LLM には触れない)。"""
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
    cert_path = Path(args.attestation)
    if cert_path.is_file() and os.environ.get("DATABASE_URL"):
        pre = preflight(detect_attest.check_attestation(cert_path))
        print(f"  DB: {pre['db_route']} read-only 確認済み {pre['read_only_checks']}")
        print("  旗: 本番コンテナと一致")
    else:
        print("  preflight: 証明書または DATABASE_URL が無いので省略")
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
    ap.add_argument("--deadline", default="", help="JST の HH:MM。単位の途中でも効く")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    if args.dry_run:
        return dry_run(args)
    cert = detect_attest.check_attestation(Path(args.attestation))
    return run_all(args, cert)


if __name__ == "__main__":
    raise SystemExit(main())
