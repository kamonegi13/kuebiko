#!/usr/bin/env python3
"""古典 ML 蒸留ヘッド v0 の学習 CLI (docs/self_evolving_tuning_design.md §14)。

DB (articles × dedup_seen_urls × article_embeddings) から学習データを取り、
時系列 split (古い 80% を train、新しい 20% を eval。シャッフル禁止 — ドリフト実態に
即した評価のため) で ``src.tuning.head_model.train_head`` を呼び、成果物を
``--out-dir`` (既定 ``data/models/head/current``) に保存する。

保存先は ``src/tuning/head_shadow.py`` の ``DEFAULT_ARTIFACT_DIR`` と一致させてある —
本 script の実行だけでシャドー推論が新しい成果物を拾う (プロセス再起動不要、
metadata.json の mtime で再読込)。

Usage::

    uv run python scripts/train_head.py
    uv run python scripts/train_head.py --embed-model snowflake-arctic-embed2 \\
        --out-dir data/models/head/current --rubric-version 2026-08-22

出力・ログに記事本文・タイトルは含めない (取得 SQL 自体が article_id / importance /
category / created_at / vector のみを選択する)。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO if (_REPO / "src").is_dir() else Path("/app")))

from src.tuning.head_model import HeadMetadata, save_artifact, train_head  # noqa: E402

_DEFAULT_EMBED_MODEL = "snowflake-arctic-embed2"
_DEFAULT_OUT_DIR = Path("data/models/head/current")
_PREVIOUS_DIRNAME = "previous"
# 時系列 split の train 比率 (古い側)。シャッフルはしない。
_TRAIN_SPLIT_RATIO = 0.8


@dataclass(frozen=True)
class _TrainingRow:
    """DB から取得した 1 記事分の学習用素材 (本文・タイトルは含めない)。"""

    article_id: str
    importance: str
    category: str | None
    created_at: str
    vector: NDArray[np.float32]


def _fetch_training_rows(embed_model: str) -> tuple[list[_TrainingRow], int]:
    """学習データを取得する。戻り値は (有効行, dim 不一致でスキップした件数)。

    ``a.created_at`` 昇順で取得するため、呼出側はこの順序をそのまま時系列 split に使える。
    """
    from src.storage.run_history import RunHistoryRepository

    repo = RunHistoryRepository()
    sql = """
        SELECT a.article_id, a.importance, a.category, a.created_at, e.vector, e.dim
        FROM articles a
        JOIN dedup_seen_urls d ON d.url = a.url
        JOIN article_embeddings e ON e.url_hash = d.url_hash
        WHERE a.importance IN ('high','medium','low') AND e.model = ?
        ORDER BY a.created_at
    """
    with repo._connect() as conn:  # noqa: SLF001 — 学習データの読み取り専用アクセス
        rows = conn.execute(sql, (embed_model,)).fetchall()

    parsed: list[_TrainingRow] = []
    expected_dim: int | None = None
    skipped = 0
    for row in rows:
        dim = int(row["dim"])
        if expected_dim is None:
            expected_dim = dim
        vector = np.frombuffer(row["vector"], dtype="<f4")
        if dim != expected_dim or vector.shape[0] != dim:
            skipped += 1
            continue
        category = row["category"]
        parsed.append(
            _TrainingRow(
                article_id=str(row["article_id"]),
                importance=str(row["importance"]),
                category=str(category) if category else None,
                created_at=str(row["created_at"]),
                vector=vector.astype(np.float32),
            )
        )
    return parsed, skipped


def _time_series_split(
    rows: list[_TrainingRow], train_ratio: float
) -> tuple[list[_TrainingRow], list[_TrainingRow]]:
    """``rows`` は既に created_at 昇順である前提 (シャッフルしない)。"""
    split_idx = int(len(rows) * train_ratio)
    return rows[:split_idx], rows[split_idx:]


def _rows_to_arrays(
    rows: list[_TrainingRow],
) -> tuple[NDArray[np.float32], list[str], list[str | None]]:
    vectors = np.vstack([r.vector for r in rows]).astype(np.float32)
    importances = [r.importance for r in rows]
    categories: list[str | None] = [r.category for r in rows]
    return vectors, importances, categories


def _rotate_existing(out_dir: Path) -> None:
    """既存の成果物を ``<out_dir の親>/previous`` へ退避してから上書きできるようにする。"""
    if not out_dir.exists() or not any(out_dir.iterdir()):
        return
    previous_dir = out_dir.parent / _PREVIOUS_DIRNAME
    if previous_dir.exists():
        shutil.rmtree(previous_dir)
    shutil.move(str(out_dir), str(previous_dir))


def _format_report(metadata: HeadMetadata, *, skipped_dim_mismatch: int, out_dir: Path) -> str:
    imp: dict[str, Any] = metadata.metrics["importance"]
    cat: dict[str, Any] = metadata.metrics["category"]
    lines = [
        "",
        "=== 古典 ML 蒸留ヘッド v0 学習結果 ===",
        f"embedding_model={metadata.embedding_model} dim={metadata.dim}",
        f"n_train={metadata.n_train} n_eval={metadata.n_eval} "
        f"dim不一致スキップ={skipped_dim_mismatch}",
        f"artifact_version={metadata.artifact_version} sklearn={metadata.sklearn_version}",
        f"rubric_version={metadata.rubric_version}",
        "",
        "[importance]",
        f"  accuracy={imp['accuracy']:.3f} macro_f1={imp['macro_f1']:.3f} ece={imp['ece']:.3f}",
        "  致命混同 (high→low): "
        f"{imp['critical_confusion']['high_to_low_count']} 件 "
        f"({imp['critical_confusion']['high_to_low_rate']:.1%})",
    ]
    for cls in metadata.importance_classes:
        pc = imp["per_class"].get(cls, {})
        lines.append(
            f"    {cls:<10} precision={pc.get('precision', 0.0):.3f} "
            f"recall={pc.get('recall', 0.0):.3f} support={pc.get('support', 0)}"
        )
    lines += [
        "",
        "[category]",
        f"  top1_accuracy={cat['top1_accuracy']:.3f} coverage={cat['coverage']:.3f} "
        f"trained_classes={cat['trained_classes']}",
        f"  学習対象クラス: {', '.join(metadata.category_classes) or '(なし)'}",
        "",
        f"出力先: {out_dir}",
    ]
    return "\n".join(lines)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--embed-model",
        default=os.environ.get("OLLAMA_EMBED_MODEL", _DEFAULT_EMBED_MODEL),
        help="学習に使う embedding モデル名 (article_embeddings.model と一致させる)",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=_DEFAULT_OUT_DIR,
        help="成果物の出力先ディレクトリ (既存は previous/ へ退避)",
    )
    parser.add_argument(
        "--rubric-version",
        default=None,
        help="メタデータに記録する rubric バージョン (任意)",
    )
    return parser


def main() -> int:
    args = _build_arg_parser().parse_args()

    rows, skipped = _fetch_training_rows(args.embed_model)
    print(f"取得 {len(rows) + skipped} 件 (dim 不一致でスキップ {skipped} 件、有効 {len(rows)} 件)")
    if not rows:
        print("学習データが 0 件のため中止", file=sys.stderr)
        return 1

    train_rows, eval_rows = _time_series_split(rows, _TRAIN_SPLIT_RATIO)
    if not train_rows or not eval_rows:
        print(
            f"時系列 split 後に train={len(train_rows)} / eval={len(eval_rows)} 件 — "
            "いずれかが空のため中止",
            file=sys.stderr,
        )
        return 1

    dim = rows[0].vector.shape[0]
    x_train, y_imp_train, y_cat_train = _rows_to_arrays(train_rows)
    x_eval, y_imp_eval, y_cat_eval = _rows_to_arrays(eval_rows)

    bundle, metadata = train_head(
        x_train,
        y_imp_train,
        y_cat_train,
        x_eval,
        y_imp_eval,
        y_cat_eval,
        embedding_model=args.embed_model,
        dim=dim,
        trained_at=datetime.now(UTC).isoformat(),
        rubric_version=args.rubric_version,
    )

    print(_format_report(metadata, skipped_dim_mismatch=skipped, out_dir=args.out_dir))

    _rotate_existing(args.out_dir)
    save_artifact(args.out_dir, bundle, metadata)
    print(f"成果物を保存: {args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
