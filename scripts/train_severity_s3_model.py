#!/usr/bin/env python3
# ruff: noqa: E501  (日本語の文は幅 2 で数えられ長く見える)
"""深刻さ C (9 問の logprob + 付帯情報 → RF) の S3 モデルを学習して書き出す (2026-10-22)。

比較 (data/eval/severity_method_compare_2026_10_21.md) の最良の設定: RF 200 本・葉 3・均衡、
特徴量 = 問い 9 本の logit + 付帯情報 (カテゴリ・記事の型・分類の種類・KEV・CVSS・被害額・CVE 数)。
閾値は層化 5 分割 × 5 反復の分割外の確率から「S3 の回収 90%」を満たす最大の値で決める。

⚠ **問いを当てるモデルごとの確率で学習する**。s24b の確率で学習した RF を s23i に当てると較正が
合わない。``--model`` には本番の fast ティアの割当を指定し、JSON の ``probe_model`` に残す
(本番は割当と違うモデルで当てたとき記録に不一致の印を付ける)。

まとめ記事 (分類が roundup・正解集でまとめ記事が S3 の 4 件) は深刻さを付けない型として外す
(``src/eval/severity_gold_marks.py``)。正解集のファイルは書き換えない。

    uv run python scripts/train_severity_s3_model.py --model kuebiko-sft:s23i [--dry-run]

入力: data/eval/sevcmp/items.jsonl と probe_<モデル>.jsonl (data/mlx/sevcmp_export.py / sevcmp_probe.py)。
出力: config/models/severity_s3_model.json (sklearn 非依存の木 JSON、export は train_relation_model と共通)。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "scripts"):
    sys.path.insert(0, str(p))

import numpy as np  # noqa: E402
from sklearn.ensemble import RandomForestClassifier  # type: ignore[import-untyped]  # noqa: E402
from sklearn.model_selection import StratifiedKFold  # type: ignore[import-untyped]  # noqa: E402
from train_relation_model import export  # noqa: E402

from src.cti import severity_c as sc  # noqa: E402
from src.eval.severity_gold_marks import gold_exclusion  # noqa: E402

ITEMS = ROOT / "data/eval/sevcmp/items.jsonl"
SEVDIR = ROOT / "data/eval/sevcmp"
OUT = ROOT / "config/models/severity_s3_model.json"
MODEL_VERSION = "severity_s3.2026-10-22"
N_TREES = 200
MIN_LEAF = 3
FOLDS = 5
REPEATS = 5
TARGET_RECALL = 0.90


def load_probes(model: str) -> dict[str, dict[str, float | None]]:
    path = SEVDIR / f"probe_{model.replace(':', '_').replace('/', '_')}.jsonl"
    rows = [json.loads(x) for x in path.open(encoding="utf-8") if x.strip()]
    return {r["article_id"]: r["p_yes"] for r in rows}


def feature_names(items: list[dict[str, Any]]) -> list[str]:
    def vocab(key: str, prefix: str) -> list[str]:
        return [f"{prefix}{v}" for v in sorted({str(r[key] or "") for r in items})]

    return [
        *[f"{sc.PROBE_PREFIX}{q}" for q in sc.QUESTION_IDS],
        *vocab("category", sc.CATEGORY_PREFIX),
        *vocab("article_type", sc.TYPE_PREFIX),
        *vocab("event_kind", sc.KIND_PREFIX),
        *sc.NUMERIC_FEATURES,
    ]


def meta_of(r: dict[str, Any]) -> sc.ArticleMeta:
    return sc.ArticleMeta(
        category=str(r["category"] or ""),
        article_type=str(r["article_type"] or ""),
        event_kind=str(r["event_kind"] or ""),
        on_kev=bool(r["on_kev"]),
        max_cvss=float(r["max_cvss"]),
        loss_usd=float(r["loss_usd"]),
        n_cves=int(r["n_cves"]),
    )


def choose_threshold(p: Any, y: Any, *, recall: float) -> tuple[float, float, float, int]:
    """回収が ``recall`` 以上になる最大の閾値と、そのときの (回収, 適合率, 付けた数)。"""
    best = (0.0, 1.0, 0.0, int(len(y)))
    for t in np.arange(0.05, 0.96, 0.01):
        sel = p >= t
        if not sel.any():
            continue
        tp = float((sel & (y == 1)).sum())
        rec = tp / max(1, int(y.sum()))
        if rec >= recall:
            best = (float(t), rec, tp / float(sel.sum()), int(sel.sum()))
    return best


def mk(seed: int = 0) -> Any:
    return RandomForestClassifier(
        N_TREES, min_samples_leaf=MIN_LEAF, class_weight="balanced", random_state=seed, n_jobs=1
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", required=True, help="問いを当てたモデル (本番の fast ティアの割当)")
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    probes = load_probes(args.model)
    all_items = [json.loads(x) for x in ITEMS.open(encoding="utf-8") if x.strip()]
    items = [
        r
        for r in all_items
        if r["article_id"] in probes
        and gold_exclusion(r["article_id"], str(r["event_kind"])) is None
    ]
    names = feature_names(items)
    x = np.array(
        [sc.feature_vector(names, probes[r["article_id"]], meta_of(r)) for r in items],
        dtype=np.float32,
    )
    y = np.array([int(r["gold_severity"] == "S3") for r in items])
    print(f"正解 {len(all_items)} 件 → まとめ記事等を外して {len(items)} 件 / S3 {int(y.sum())} 件")

    p = np.zeros(len(y))
    for rep in range(REPEATS):
        for tr, te in StratifiedKFold(FOLDS, shuffle=True, random_state=rep).split(x, y):
            p[te] += mk(rep).fit(x[tr], y[tr]).predict_proba(x[te])[:, 1] / REPEATS
    t, rec, prec, n_sel = choose_threshold(p, y, recall=TARGET_RECALL)
    print(
        f"閾値 {t:.2f} (分割外: 回収 {100 * rec:.0f}% 適合率 {100 * prec:.0f}% S3 を付けた数 {n_sel})"
    )

    model = mk().fit(x, y)
    out = export(model, t, len(y))
    out.update(
        {
            "feature_names": names,
            "model_version": MODEL_VERSION,
            "probe_model": args.model,
            "question_ids": list(sc.QUESTION_IDS),
            "target_recall": TARGET_RECALL,
            "n_positive": int(y.sum()),
            "excluded": "まとめ記事 (event_kind=roundup + 正解集で S3 の週報)",
        }
    )
    for key in ("positive", "window_days"):  # 関係モデル用の欄 (この JSON では意味を持たない)
        out.pop(key, None)
    diff = max(
        abs(sc.forest_probability(out["trees"], row) - q)
        for row, q in zip(x, model.predict_proba(x)[:, 1], strict=True)
    )
    print(f"numpy 推論と sklearn の最大差 {diff:.6f}")
    if diff > 1e-4:
        print("⚠ 一致しない — 書き出さない", file=sys.stderr)
        return 1
    if args.dry_run:
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    print(f"書込: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
