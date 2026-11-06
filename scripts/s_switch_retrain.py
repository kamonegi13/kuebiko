"""S (fast/reasoning ティアのモデル) 切替時の ML 一括の学び直しの道具 (S-SWITCH-RETRAIN)。

手順書: data/eval/s_switch_retrain_procedure.md (運用者ローカル) と docs/s_switch_retrain.md。
**本番の表・config/models/*.json・割当には一切書かない** (出力は staging のみ)。

    uv run python scripts/s_switch_retrain.py plan [--s <新しい S>]
    uv run python scripts/s_switch_retrain.py relabel --s <新しい S> --dry-run     # 対象件数だけ
    uv run python scripts/s_switch_retrain.py relabel --s <新しい S> --what kind
    uv run python scripts/s_switch_retrain.py train --s <新しい S> [--dry-run]    # staging へ
    uv run python scripts/s_switch_retrain.py gate --metrics <json>
    uv run python scripts/s_switch_retrain.py swap | revert                         # 手順の表示のみ

読みは ``DATABASE_URL`` (読み取り) 経由。LLM を呼ぶ段 (relabel の kind・pair) は GPU/Ollama を使うので
本番コンテナの外・空き時間に流す (CLAUDE.md §7)。``LLM_USAGE_LOCAL=0`` を強制する。
"""

# ruff: noqa: E501
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

# 評価・教師収穫は計測を止める (CLAUDE.md §7 10-29)。import より前に立てる
os.environ["LLM_USAGE_LOCAL"] = "0"

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.tools.ml_s_dependency import (  # noqa: E402
    NOT_RETRAINED,
    RETRAIN_PLAN,
    derive_dependencies,
    find_misaligned,
)
from src.tools.s_switch_gate import (  # noqa: E402
    evaluate_gates,
    parse_deep_dive_stdout,
    parse_relation_stdout,
    parse_severity_stdout,
)

MODELS_DIR = ROOT / "config/models"
MLX = ROOT / "data/mlx"
STAGING_DIR = ROOT / "data/eval/s_switch_staging"
TIERS_URL = "http://127.0.0.1:8001/api/v1/model-tiers"
#: ML ごとの S 由来の特徴を付け直す step (表の SSoT は ml_s_dependency。ここは割当の確認用)
RELABEL_STEPS = (
    "event_kind",
    "severity_axes",
    "pair_judge",
    "article_summary",
    "judgment_classify",
)
KIND_FILES = (MLX / "detect_gold_kinds.jsonl", MLX / "detect_heldout_kinds.jsonl")
SEVC_ITEMS = ROOT / "data/eval/sevcmp/items.jsonl"
DETECT_GOLD = MLX / "detect_goldset.jsonl"
PROBE_TIMEOUT_S = 5


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def fetch_tiers() -> dict[str, Any] | None:
    """現行の割当 (読み取りのみ)。到達できなければ None。"""
    try:
        with urllib.request.urlopen(TIERS_URL, timeout=PROBE_TIMEOUT_S) as r:  # noqa: S310
            data: dict[str, Any] = json.loads(r.read())
            return data
    except Exception as e:  # noqa: BLE001 — 表示用。失敗は理由つきで続行
        print(f"  (割当 API に届かない: {type(e).__name__})", file=sys.stderr)
        return None


def cmd_plan(args: argparse.Namespace) -> int:
    report = derive_dependencies(MODELS_DIR)
    print("[ML × S 由来の特徴 × step]")
    for d in report.s_derived():
        print(
            f"  {d.file:26s} {d.step:18s} {d.strength:6s} n={len(d.features):2d} 学習時={d.trained_models or '(記録なし)'}"
        )
    tiers = fetch_tiers()
    steps = {s["step"]: s for s in (tiers or {}).get("steps", [])}
    print("\n[現行の割当]")
    if tiers:
        print("  tiers:", json.dumps(tiers.get("tiers"), ensure_ascii=False))
    for name in RELABEL_STEPS:
        s = steps.get(name)
        print(
            f"  step:{name:18s} -> {s['model'] if s else '(不明)'}"
            + (" [上書き]" if s and s["source"] == "override" else "")
        )
    print("\n[学習スクリプト]")
    for p in RETRAIN_PLAN:
        print(f"  {p.file:26s} {p.trainer or '(repo に無い)'}  付け直し={','.join(p.reattach)}")
    print("\n[対象外]")
    for f, why in NOT_RETRAINED.items():
        print(f"  {f}: {why}")
    s_new = args.s or (tiers or {}).get("tiers", {}).get("fast", "")
    for m in find_misaligned(report, s_new) if s_new else ():
        print(f"  {m.reason}: {m.file} {m.step} 学習時={m.trained_models}")
    return 1 if report.unmapped or report.unknown_files else 0


def relabel_targets() -> dict[str, int]:
    """付け直しの対象件数 (ファイルから。DB の読みは --dry-run でも要らない)。"""
    kind_ids = {str(r["article_id"]) for f in KIND_FILES for r in _jsonl(f)}
    sevc = _jsonl(SEVC_ITEMS)
    gold = _jsonl(DETECT_GOLD)
    return {
        "kind (detect の学習・held-out の記事)": len(kind_ids),
        "kind (深刻さ C の正解集の記事)": len(sevc),
        "axes (detect の学習集合)": len(gold),
        "probes (深刻さ C の記事 × 問い 9)": len(sevc),
    }


async def _relabel_kinds(model: str, ids: list[str], out: Path) -> int:
    """記事の種別を新しい S で分類し直して ``out`` (jsonl) へ。再開可 (済みは飛ばす)。"""
    from src.config_loader import load_app_config
    from src.eventnews.event_kind import classify
    from src.storage.run_history import RunHistoryRepository
    from src.tools.model_tiers import Step, build_llm_for_ref

    done = {str(r["article_id"]) for r in _jsonl(out)}
    todo = [i for i in ids if i not in done]
    repo = RunHistoryRepository()
    arts = repo.get_articles_by_ids(todo)
    llm = build_llm_for_ref(model, Step.EVENT_KIND, load_app_config())
    out.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out.open("a", encoding="utf-8") as fh:
        for aid in todo:
            a = arts.get(aid)
            if a is None:
                continue
            kind = await classify(llm, a.title, a.summary or "")
            fh.write(
                json.dumps({"article_id": aid, "kind": kind, "model": model}, ensure_ascii=False)
                + "\n"
            )
            n += 1
    return n


def cmd_relabel(args: argparse.Namespace) -> int:
    targets = relabel_targets()
    print(f"対象件数 (S={args.s}, 出力先 {STAGING_DIR}):")
    for k, v in targets.items():
        print(f"  {k}: {v}")
    if args.dry_run:
        print("--dry-run: LLM は呼ばない")
        return 0
    if not os.environ.get("DATABASE_URL"):
        print(
            "⚠ DATABASE_URL が無い (空 SQLite を読むことになる)。読み取りの接続を設定する",
            file=sys.stderr,
        )
        return 1
    tag = args.s.replace(":", "_")
    if args.what in ("kind", "all"):
        ids = sorted(
            {str(r["article_id"]) for f in KIND_FILES for r in _jsonl(f)}
            | {str(r["article_id"]) for r in _jsonl(SEVC_ITEMS)}
        )
        n = asyncio.run(_relabel_kinds(args.s, ids, STAGING_DIR / f"kinds_{tag}.jsonl"))
        print(f"kind: {n} 件を書いた")
    if args.what in ("axes", "all"):
        print(
            "axes: GPU の段。D1 の型で  uv run python data/mlx/axes_body_backfill.py (出力は jsonl、本番の列は書かない)"
        )
    if args.what in ("probes", "all"):
        print(
            f"probes: GPU の段。  uv run python data/mlx/sevcmp_probe.py {args.s}  -> data/eval/sevcmp/probe_{tag}.jsonl"
        )
    return 0


def cmd_train(args: argparse.Namespace) -> int:
    """5 本 (pair 除く) を staging へ。pair は repo に学習スクリプトが無いので手順書の宿題。"""
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    metrics: dict[str, dict[str, float]] = {}
    jobs = (
        ("detect", ["scripts/train_detect_ml.py"], MODELS_DIR / "detect_model.json.staging", None),
        (
            "severity_s3",
            ["scripts/train_severity_s3_model.py", "--model", args.s],
            MODELS_DIR / "severity_s3_model.json.staging",
            parse_severity_stdout,
        ),
        (
            "deep_dive",
            ["scripts/train_deep_dive_ml.py"],
            MODELS_DIR / "deep_dive_model.json.staging",
            parse_deep_dive_stdout,
        ),
        (
            "relation",
            ["scripts/train_relation_model.py"],
            MODELS_DIR / "relation_model.json.staging",
            parse_relation_stdout,
        ),
    )
    for name, argv, out, parse in jobs:
        cmd = [
            sys.executable,
            *argv,
            "--out",
            str(out),
            *(["--dry-run"] if args.dry_run and name != "deep_dive" else []),
        ]
        print("$", " ".join(cmd))
        if args.dry_run and name == "deep_dive":
            print("  (deep_dive は --dry-run が無いので実行しない)")
            continue
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False)  # noqa: S603
        print(proc.stdout[-1500:])
        if proc.returncode != 0:
            print(proc.stderr[-800:], file=sys.stderr)
            return proc.returncode
        if parse:
            metrics[name] = parse(proc.stdout)
    print("pair: 学習スクリプトが repo に無い (手順書 §3 の宿題)。staging は作らない")
    (STAGING_DIR / "train_metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    return 0


def cmd_gate(args: argparse.Namespace) -> int:
    metrics = json.loads(Path(args.metrics).read_text(encoding="utf-8"))
    cur = json.loads((MODELS_DIR / "deep_dive_model.json").read_text(encoding="utf-8")).get(
        "holdout_corr"
    )
    ok, results = evaluate_gates(metrics, current_deep_dive_corr=cur)
    for r in results:
        print(f"  {r.model:12s} {r.status:10s} {r.detail}")
    print("全体:", "合格 (組で切替可)" if ok else "不合格・未測定あり — 5 本とも出さない")
    return 0 if ok else 1


SWAP_STEPS = """組での切替 (本番の操作は利用者の許可のうえ、ローカルで。表示のみ):
 1. 退避: 5 本を data/model_backups/<名前>.json.bak_<旧S>_<日付> へ cp。article_severity_axes / article_importance_v2 を _bak_ へ CREATE TABLE AS
 2. 軸の取り込み (D1 §5-2) と旗 (AXES_FROM_BODY 等)
 3. 同じ時刻に: 5 本の *.json.staging を config/models/<名前>.json へ mv + 割当の版を POST /api/v1/model-tiers
    (基本の 5 ティア fast/narrative/reasoning/dialog/embedding を毎回すべて送る。step:/fallback: の族は含めなければ現値を持ち越す)
 4. 確認: check_s_switch_ml.py の mismatch 0・detect_ml_axes_model_mismatch が消える・S3 の日次件数"""
REVERT_STEPS = """組での戻し (片方だけ戻さない):
 1. 割当: POST /api/v1/config-history/model_tiers/revert {"version": <切替前の版>}
 2. ML: 退避の 5 本を config/models/ へ cp 戻す (JSON と割当をセットで)
 3. 軸: _bak_ から復元し、取り込んだ id の article_importance_v2 を DELETE (毎時で再導出)
 4. 旗を戻し docker compose up -d kuebiko readonly"""


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--s", default="")
    p = sub.add_parser("relabel")
    p.add_argument("--s", required=True)
    p.add_argument("--what", choices=("kind", "axes", "probes", "all"), default="all")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("train")
    p.add_argument("--s", required=True)
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("gate")
    p.add_argument("--metrics", required=True)
    sub.add_parser("swap")
    sub.add_parser("revert")
    args = ap.parse_args()
    if args.cmd == "swap":
        print(SWAP_STEPS)
        return 0
    if args.cmd == "revert":
        print(REVERT_STEPS)
        return 0
    return {"plan": cmd_plan, "relabel": cmd_relabel, "train": cmd_train, "gate": cmd_gate}[
        args.cmd
    ](args)


if __name__ == "__main__":
    sys.exit(main())
