"""S (fast/reasoning ティアのモデル) 切替時の ML 一括の学び直しの道具 (S-SWITCH-RETRAIN)。

手順書: data/eval/s_switch_retrain_procedure.md (運用者ローカル) と docs/s_switch_retrain.md。
**本番の表・config/models/*.json・割当には一切書かない** (出力は staging のみ)。

    uv run python scripts/s_switch_retrain.py plan [--s <新しい S>]
    uv run python scripts/s_switch_retrain.py relabel --s <新しい S> --dry-run     # 対象件数だけ
    uv run python scripts/s_switch_retrain.py relabel --s <新しい S> --what kind
    uv run python scripts/s_switch_retrain.py relabel --s <現行の S> --what kind  # detect の対照腕の種別
    uv run python scripts/s_switch_retrain.py relabel --s <新しい S> --what axes    # 軸 (本文入力、GPU)
    uv run python scripts/s_switch_retrain.py relabel --s <新しい S> --what probes  # 深刻さ C の 9 問 (GPU)
    # 試走: --limit 3 --max-seconds 50 --out-dir <scratch> (Ollama を長く占有しない)
    uv run python scripts/s_switch_retrain.py train --s <新しい S> [--control-s <現行の S>] [--dry-run]
    uv run --with scikit-learn python scripts/s_switch_retrain.py measure --s <新しい S> [--control-s <現行の S>]
    uv run python scripts/s_switch_retrain.py gate --metrics <json>
    uv run python scripts/s_switch_retrain.py swap | revert                         # 手順の表示のみ

読みは ``DATABASE_URL`` (読み取り) 経由。LLM を呼ぶ段 (relabel の kind・axes・probes・pair) は GPU/Ollama を使うので
本番コンテナの外・空き時間に流す (CLAUDE.md §7)。``LLM_USAGE_LOCAL=0`` を強制する。
"""

# ruff: noqa: E501
from __future__ import annotations

import argparse
import asyncio
import importlib
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
from src.tools.s_switch_measure import merge_metrics  # noqa: E402

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
#: 深刻さ C の学習集合 604 件 (10-28 に sevc_ci65_probe.py で当て直した集合)。
#: 評価用の 203 件 (data/eval/sevcmp/items.jsonl、train_severity_s3_model.py の既定) とは別
SEVC_ITEMS = ROOT / "data/eval/sevc_v5/items.jsonl"
SEVC_MAX_MISSING = 2  # sevc_v5_train.MAX_MISSING と同じ
MODEL_NAMES = ("detect", "severity_s3", "deep_dive", "relation", "pair")
PAIR_SAMPLES = tuple(
    ROOT / f"data/eval/{n}" for n in ("eval_sample.json", "eval_sample2.json", "eval_sample3.json")
)
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


def _ids(rows: list[dict[str, Any]]) -> set[str]:
    return {str(r["article_id"]) for r in rows}


def relabel_targets() -> dict[str, set[str] | list[tuple[str, str]]]:
    """付け直しの対象 (ファイルから。DB は要らない)。値は記事 id の集合、pair は対の一覧。

    detect の学習 = 審判 ``detect_goldset`` (学習) + held-out (閾値)。深刻さ C の学習集合 = 604 件
    (``data/eval/sevc_v5/items.jsonl``、10-28 に ``sevc_ci65_probe.py`` で当て直した集合)。
    評価用の 203 件 (``data/eval/sevcmp``) は付け直しの対象に含めない (別の用途)。
    relation の種別は DB の窓内の記事で決まり、ここでは数えない (DB が要る)。
    """
    gold = _ids(_jsonl(DETECT_GOLD))
    heldout = _ids(_jsonl(KIND_FILES[1]))
    sevc = _ids(_jsonl(SEVC_ITEMS))
    pairs = [(str(p["a"]), str(p["b"])) for f in PAIR_SAMPLES for p in json.loads(f.read_text())]
    return {
        "kind": gold | heldout | sevc | {x for ab in pairs for x in ab},
        "axes": gold | heldout,
        "probes": sevc,
        "pair_judge": pairs,
    }


def staging_paths(s: str, out_dir: Path | None = None) -> dict[str, Path]:
    """付け直しの出力先。``out_dir`` (試走用) を渡すと 4 本ともその下に置く。"""
    tag = s.replace(":", "_").replace("/", "_")
    base = out_dir or STAGING_DIR
    return {
        "kinds": base / f"kinds_{tag}.jsonl",
        "axes": base / f"axes_{tag}.jsonl",
        "probes": (out_dir or ROOT / "data/eval/sevc_v5") / f"probe_{tag}.jsonl",
        "pair_llm": base / f"pair_llm_{tag}.jsonl",
    }


def _load_axes_items(ids: list[str]) -> list[Any]:
    """軸の付け直しの入力 (DB の読み取りのみ。本文・公開日は本番の読み手 fetch_bodies を流用)。"""
    from src.cti.severity_axes import fetch_bodies, fetch_published
    from src.storage.run_history import RunHistoryRepository
    from src.tools.s_switch_relabel import RelabelItem

    os.environ["AXES_FROM_BODY"] = "1"  # 本文入力 (D1 以降の本番と同じ prompt)。このプロセスだけ
    repo = RunHistoryRepository()
    arts = repo.get_articles_by_ids(ids)
    bodies, published = fetch_bodies(repo, ids), fetch_published(repo, ids)
    return [
        RelabelItem(
            a, arts[a].title or "", arts[a].summary or "", bodies.get(a, ""), published.get(a)
        )
        for a in ids
        if a in arts
    ]


async def _relabel_axes(model: str, ids: list[str], out: Path, limit: int, max_s: float) -> Any:
    from src.config_loader import load_app_config
    from src.cti.severity_axes import classify_axes
    from src.tools.model_tiers import Step, build_llm_for_ref
    from src.tools.s_switch_relabel import relabel_axes

    items = _load_axes_items(ids)
    llm = build_llm_for_ref(model, Step.SEVERITY_AXES, load_app_config())

    async def classify(title: str, summary: str, body: str, pub: str | None) -> Any:
        axes = await classify_axes(llm, title, summary, body, pub)
        return None if axes is None else axes.model_dump()

    return await relabel_axes(
        items, model=model, out=out, classify=classify, limit=limit, max_seconds=max_s
    )


async def _relabel_probes(model: str, out: Path, limit: int, max_s: float) -> Any:
    """深刻さ C の学習集合 604 件 (items.jsonl に本文込み、DB 不要) に新しい S で 9 問を当てる。"""
    import ollama

    from src.config_loader import load_app_config
    from src.cti.severity_c import QUESTION_IDS, probe_questions
    from src.tools.model_tiers import STEP_REGISTRY, Step
    from src.tools.s_switch_relabel import items_from_sevc_rows, relabel_probes

    config = load_app_config()
    host = os.environ.get("OLLAMA_BASE_URL") or config.ollama_base_url
    timeout = STEP_REGISTRY[Step.SEVERITY_AXES].timeout_seconds
    client = ollama.AsyncClient(host=host, timeout=timeout)

    async def probe(title: str, pub: str | None, body: str) -> Any:
        return await probe_questions(
            title=title, published=pub, body=body, config=config, client=client, model=model
        )

    items = items_from_sevc_rows(_jsonl(SEVC_ITEMS))
    return await relabel_probes(
        items,
        model=model,
        out=out,
        probe=probe,
        question_ids=QUESTION_IDS,
        limit=limit,
        max_seconds=max_s,
    )


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


async def _relabel_pairs(model: str, pairs: list[tuple[str, str]], out: Path) -> int:
    """正解対の同一判定を新しい判定器で当て直す (``{"a","b","same": bool|null,"model"}``)。再開可。"""
    from src.config_loader import load_app_config
    from src.eventnews.pair_judge import build_prompt, judge_pair
    from src.storage.run_history import RunHistoryRepository
    from src.tools.model_tiers import Step, build_llm_for_ref

    done = {(str(r["a"]), str(r["b"])) for r in _jsonl(out)}
    todo = [p for p in pairs if p not in done]
    arts = RunHistoryRepository().get_articles_by_ids(sorted({x for p in todo for x in p}))
    llm = build_llm_for_ref(model, Step.PAIR_JUDGE, load_app_config())
    out.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out.open("a", encoding="utf-8") as fh:
        for a_id, b_id in todo:
            a, b = arts.get(a_id), arts.get(b_id)
            if a is None or b is None:
                continue
            prompt = build_prompt(
                a.title,
                a.summary or "",
                a.feed_title or "",
                b.title,
                b.summary or "",
                b.feed_title or "",
            )
            same = await judge_pair(llm, prompt)
            row = {"a": a_id, "b": b_id, "same": same, "model": model}
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            n += 1
    return n


def cmd_relabel(args: argparse.Namespace) -> int:
    targets = relabel_targets()
    paths = staging_paths(args.s, Path(args.out_dir) if args.out_dir else None)
    lim, max_s = args.limit, args.max_seconds
    print(f"対象件数 (S={args.s}):")
    print(
        f"  kind (detect 学習+held-out・深刻さ C 604・pair の対の記事の和集合): {len(targets['kind'])}"
    )
    print(f"  axes (detect の学習集合 + held-out、本文入力の GPU 段): {len(targets['axes'])}")
    print(f"  probes (深刻さ C の学習集合 604 件 × 問い 9 = {len(targets['probes']) * 9} 回)")
    print(f"  pair_judge (正解対): {len(targets['pair_judge'])}")
    print("  (relation の種別は DB の窓内の記事で決まる。deep_dive は本番が種別を使わない)")
    print("出力先: " + ", ".join(f"{k}={v}" for k, v in paths.items()))
    if args.dry_run:
        print("--dry-run: LLM は呼ばない")
        return 0
    # probes は items.jsonl に本文込みで DB を読まない。他の段は記事を DB から読む
    if args.what != "probes" and not os.environ.get("DATABASE_URL"):
        print(
            "⚠ DATABASE_URL が無い (空 SQLite を読むことになる)。読み取りの接続を設定する",
            file=sys.stderr,
        )
        return 1
    if args.what in ("kind", "all"):
        kind_ids = sorted(str(x) for x in targets["kind"])
        n = asyncio.run(_relabel_kinds(args.s, kind_ids[:lim] if lim else kind_ids, paths["kinds"]))
        print(f"kind: {n} 件を書いた")
    if args.what in ("pair", "all"):
        model = args.pair_model or args.s
        pairs = list(targets["pair_judge"])
        n = asyncio.run(_relabel_pairs(model, pairs[:lim] if lim else pairs, paths["pair_llm"]))  # type: ignore[arg-type]
        print(f"pair_judge ({model}): {n} 件を書いた")
    if args.what in ("axes", "all"):
        r = asyncio.run(_relabel_axes(args.s, sorted(targets["axes"]), paths["axes"], lim, max_s))  # type: ignore[arg-type]
        print(f"axes (本文入力): {r} -> {paths['axes']}")
    if args.what in ("probes", "all"):
        r = asyncio.run(_relabel_probes(args.s, paths["probes"], lim, max_s))
        print(f"probes: {r} -> {paths['probes']}")
    return 0


def build_train_jobs(
    s: str, paths: dict[str, Path], *, pair_model: str | None
) -> list[tuple[str, list[str], Path, Any]]:
    """各 trainer の (名前, argv, staging の出力先, 標準出力の解析)。入力は staging の付け直し。"""
    out = {n: MODELS_DIR / f"{n}_model.json.staging" for n in MODEL_NAMES}
    pair_args = ["--kinds", str(paths["kinds"]), "--llm-scores", str(paths["pair_llm"])]
    if pair_model:
        pair_args += ["--llm-model", pair_model]
    return [
        (
            "detect",
            [
                "scripts/train_detect_ml.py",
                "--kinds",
                str(paths["kinds"]),
                "--axes",
                str(paths["axes"]),
                "--axes-model",
                s,
            ],
            out["detect"],
            None,
        ),
        (
            "severity_s3",
            [
                "scripts/train_severity_s3_model.py",
                "--model",
                s,
                "--items",
                str(SEVC_ITEMS),
                "--probe-file",
                str(paths["probes"]),
                "--max-missing",
                str(SEVC_MAX_MISSING),
                "--kinds",
                str(paths["kinds"]),
            ],
            out["severity_s3"],
            parse_severity_stdout,
        ),
        # deep_dive: 本番の選抜は種別を渡さない (全て other) ので付け直しの入力は無い。DB から再学習
        ("deep_dive", ["scripts/train_deep_dive_ml.py"], out["deep_dive"], parse_deep_dive_stdout),
        (
            "relation",
            ["scripts/train_relation_model.py", "--kinds", str(paths["kinds"])],
            out["relation"],
            parse_relation_stdout,
        ),
        ("pair", ["scripts/train_pair_model.py", *pair_args], out["pair"], None),
    ]


def build_control_job(
    control_s: str, control_kinds: Path, control_axes: Path | None = None
) -> tuple[str, list[str], Path, Any]:
    """detect の対照腕 (現行の S で付け直した種別 + 軸)。

    新 vs 対照が S の効果、対照 vs 現行が付け直しの効果 (欠けの解消・揺れ、参考)。
    ``control_axes`` (``relabel --s <現行の S> --what axes`` の出力) が無ければ軸は DB のまま
    (この場合の新 vs 対照には、軸の付け直しの揺れが S の効果に混ざる)。
    """
    argv = ["scripts/train_detect_ml.py", "--kinds", str(control_kinds), "--kinds-model", control_s]
    if control_axes is not None:
        argv += ["--axes", str(control_axes), "--axes-model", control_s]
    return ("detect_control", argv, MODELS_DIR / "detect_model.json.staging_control", None)


def _control_axes(control_s: str) -> Path | None:
    """対照の軸 (現行の S の付け直し) があればそのパス。無ければ None と警告。"""
    path = staging_paths(control_s)["axes"]
    if path.exists():
        return path
    print(f"⚠ 対照の軸が無い ({path}): 対照は DB の軸。軸の付け直しの揺れが新 vs 対照に混ざる")
    return None


def current_severity_probes() -> tuple[str | None, Path | None]:
    """現行の深刻さ C が学んだ確率のファイル (``severity_s3_model.json`` の ``probe_model`` から)。

    関門の基準を「同じ手順で測った現行」にするため。無ければ (None, None) = 定数の基準。
    """
    model = json.loads((MODELS_DIR / "severity_s3_model.json").read_text(encoding="utf-8")).get(
        "probe_model"
    )
    if not model:
        return None, None
    path = staging_paths(str(model))["probes"]
    return (str(model), path) if path.exists() else (None, None)


def _write_metrics(computed: dict[str, Any]) -> None:
    """train_metrics.json へ重ねる。手で足した値 (開設の件数など) は計算値が None なら守る。"""
    path = STAGING_DIR / "train_metrics.json"
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    path.write_text(
        json.dumps(merge_metrics(existing, computed), ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )


def cmd_train(args: argparse.Namespace) -> int:
    """5 本を staging へ (付け直した入力を引数で渡す)。**本番の config は書かない**。"""
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    paths = staging_paths(args.s)
    if args.axes_file:
        paths = {**paths, "axes": Path(args.axes_file)}
    missing = [k for k, p in paths.items() if not p.exists()]
    if missing:
        print(f"付け直しの入力が未作成: {', '.join(f'{k}={paths[k]}' for k in missing)}")
        if not args.dry_run:
            print("先に relabel (と GPU の段) を済ませる", file=sys.stderr)
            return 2
    jobs = build_train_jobs(args.s, paths, pair_model=args.pair_model)
    if args.control_s:
        control_kinds = staging_paths(args.control_s)["kinds"]
        if not control_kinds.exists():
            print(f"対照の種別が未作成: {control_kinds}")
            print(f"  → relabel --s {args.control_s} --what kind (現行の S で付け直す)")
            if not args.dry_run:
                return 2
            missing = [*missing, "control_kinds"]
        jobs.append(build_control_job(args.control_s, control_kinds, _control_axes(args.control_s)))
    else:
        print("⚠ 対照腕なし (--control-s 無し): detect の新 vs 現行は交絡する。gate が警告する")
    metrics: dict[str, dict[str, float]] = {}
    for name, argv, out, parse in jobs:
        dry = ["--dry-run"] if args.dry_run and name != "deep_dive" else []
        cmd = [sys.executable, *argv, "--out", str(out), *dry]
        print("$", " ".join(cmd))
        if args.dry_run and (missing or name == "deep_dive"):
            print("  (dry-run: 入力が未作成 / --dry-run が無いので実行しない)")
            continue
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False)  # noqa: S603
        print(proc.stdout[-1500:])
        if proc.returncode != 0:
            print(proc.stderr[-800:], file=sys.stderr)
            return proc.returncode
        if parse:
            parsed = parse(proc.stdout)
            if name == "severity_s3":
                # trainer の値は「回収 90%」の閾値での値。関門は固定の 0.50 (measure が測る) なので別の名前で残す
                parsed = {f"{k}_at_train_threshold": v for k, v in parsed.items()}
            metrics[name] = parsed
    _write_metrics(metrics)
    return 0


MEASURE_SECTIONS = ("severity_s3", "relation", "pair", "detect")


def cmd_measure(args: argparse.Namespace) -> int:
    """staging のモデルと既存の評価データから関門の指標を計算し train_metrics.json へ重ねる。

    GPU・Ollama・DB への書き込みは無い (detect・relation は DB の読み取り)。手で足した値は守る。
    開設の件数 (審判が要る) は None のまま = gate が未測定として止め、測り方を表示する。
    """
    # sklearn が要るので遅延 import (plan/gate/relabel は不要)
    sm: Any = importlib.import_module("scripts.s_switch_measure")

    paths = staging_paths(args.s)
    if args.axes_file:
        paths = {**paths, "axes": Path(args.axes_file)}
    control = staging_paths(args.control_s)["kinds"] if args.control_s else None
    wanted = args.only or list(MEASURE_SECTIONS)
    computed: dict[str, Any] = {}
    failed: list[str] = []
    for name in wanted:
        try:
            if name == "severity_s3":
                cur_model, cur_probes = current_severity_probes()
                computed[name] = sm.measure_severity(
                    items_path=SEVC_ITEMS,
                    probe_file=paths["probes"],
                    probe_model=args.s,
                    kinds_path=paths["kinds"],
                    max_missing=SEVC_MAX_MISSING,
                    current_probe_file=cur_probes,
                    current_probe_model=cur_model,
                )
            elif name == "relation":
                computed[name] = sm.measure_relation(
                    gold=MLX / "event_relations_goldset_v3.jsonl", kinds_path=paths["kinds"]
                )
            elif name == "pair":
                computed[name] = sm.measure_pair(
                    current_model=MODELS_DIR / "pair_model.json",
                    staging_model=MODELS_DIR / "pair_model.json.staging",
                    labels=ROOT / "data/eval/labels_verified.json",
                    samples=list(PAIR_SAMPLES),
                    features=ROOT / "data/eval/features_v3.json",
                    current_kinds=ROOT / "data/eval/event_kind.json",
                    new_kinds=paths["kinds"],
                    new_llm_scores=paths["pair_llm"] if paths["pair_llm"].exists() else None,
                )
            else:
                computed[name] = sm.measure_detect(
                    gold_path=DETECT_GOLD,
                    current_kind_files=KIND_FILES,
                    new_kinds=paths["kinds"],
                    new_axes=paths["axes"],
                    axes_model=args.s,
                    control_kinds=control if control is not None and control.exists() else None,
                    control_axes=_control_axes(args.control_s) if args.control_s else None,
                    control_axes_model=args.control_s or None,
                )
                if control is None or not control.exists():
                    print(
                        "⚠ 対照腕なし: detect の新 vs 現行は交絡する (relabel --s <現行の S> で対照を作る)"
                    )
        except (FileNotFoundError, SystemExit) as e:
            print(f"  {name}: 測れない ({type(e).__name__}: {e})", file=sys.stderr)
            failed.append(name)
            continue
        print(f"  {name}: {json.dumps(computed[name], ensure_ascii=False)}")
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    _write_metrics(computed)
    print(f"-> {STAGING_DIR / 'train_metrics.json'} (測れなかった: {failed or 'なし'})")
    print("手で測る: detect の new_only_opens / control_only_opens (D1 の digest の審判)")
    return 1 if failed else 0


def cmd_gate(args: argparse.Namespace) -> int:
    metrics = json.loads(Path(args.metrics).read_text(encoding="utf-8"))
    cur = json.loads((MODELS_DIR / "deep_dive_model.json").read_text(encoding="utf-8")).get(
        "holdout_corr"
    )
    ok, results = evaluate_gates(metrics, current_deep_dive_corr=cur)
    for r in results:
        print(f"  {r.model:12s} {r.status:10s} {r.detail}")
        if r.warning:
            print(f"  {'':12s} ⚠ {r.warning}")
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
    p.add_argument("--what", choices=("kind", "axes", "probes", "pair", "all"), default="all")
    p.add_argument("--pair-model", default="", help="pair の判定器 (既定 = --s。現行は s23i 固定)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--limit", type=int, default=0, help="段ごとに当てる件数の上限 (試走用、0 = 全部)"
    )
    p.add_argument(
        "--max-seconds", type=float, default=0.0, help="軸・確率の段の時間の上限 (試走用、0 = なし)"
    )
    p.add_argument("--out-dir", default="", help="出力先 (試走用。既定は staging)")
    p = sub.add_parser("train")
    p.add_argument("--s", required=True)
    p.add_argument("--pair-model", default="", help="pair の判定器 (trained_llm_models に残す)")
    p.add_argument(
        "--axes-file", default="", help="付け直した軸の jsonl (既定は staging の axes_<S>)"
    )
    p.add_argument(
        "--control-s", default="", help="detect の対照腕 (現行の S。kinds_<S>.jsonl を先に relabel)"
    )
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("measure")
    p.add_argument("--s", required=True)
    p.add_argument("--control-s", default="", help="detect の対照腕 (現行の S)")
    p.add_argument("--axes-file", default="", help="付け直した軸の jsonl")
    p.add_argument("--only", nargs="+", choices=MEASURE_SECTIONS, help="測る節 (既定は全部)")
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
    handlers = {
        "plan": cmd_plan,
        "relabel": cmd_relabel,
        "train": cmd_train,
        "measure": cmd_measure,
        "gate": cmd_gate,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
