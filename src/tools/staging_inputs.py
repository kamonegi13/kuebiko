"""学習スクリプトに「付け直した特徴 (staging の jsonl)」を読ませる入力層 (S-SWITCH-RETRAIN)。

S の切替で付け直した種別 (``{"article_id", "kind"}``) と深刻度の軸 (``{"article_id", "model",
"axes"}``、``data/mlx/axes_body_backfill.py`` の出力と同形) を、DB の代わりに学習へ渡す。
**本番の表は読み替えるだけで書かない**。引数を省けば (``None``) 従来の DB 経路のまま。

``StagingOverlayRepo`` は repo をそのまま委譲し、``get_article_kinds`` / ``get_severity_axes`` だけ
staging で置き換える。staging に無い記事は**返さない** (DB の旧 S の値と混ぜない)。
欠けた件数は ``misses`` に数え、呼び手が被覆を表示できる。
"""

# ruff: noqa: E501

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from src.cti.severity_axes import BOOL_EXTRA_FIELDS, EXTRA_FIELDS, STORED_FIELDS


def _rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def load_kinds_jsonl(path: Path) -> dict[str, str]:
    """``{"article_id", "kind"}`` の jsonl → 記事 id → 種別 (同じ記事は後の行を採る)。"""
    return {str(r["article_id"]): str(r["kind"]) for r in _rows(path)}


def infer_kinds_model(path: Path) -> str | None:
    """種別 jsonl の行が持つ ``model`` が 1 種類だけならその名前。無い・混在・空なら None。

    ``s_switch_retrain.py relabel`` は各行に分類した S を ``model`` で残す。複数の S が混ざる
    (再開で S を替えた等) ファイルは「どの S の種別か」を言えないので None (= 記録しない)。
    """
    models = {str(r.get("model") or "") for r in _rows(path)}
    # 空 (model 欄なし) が混じる = どの S か言えない
    return next(iter(models)) if len(models) == 1 and "" not in models else None


def resolve_kinds_model(explicit: str | None, kinds_path: Path | None) -> str | None:
    """学習 JSON の ``kinds_model``。明示 (``--kinds-model``) を優先、無ければ種別 jsonl から推定。

    種別を DB / 既存ファイルから読む (``kinds_path`` が None) ときは推定しない = None (混在・過去値)。
    """
    if explicit:
        return explicit
    return infer_kinds_model(kinds_path) if kinds_path is not None else None


def axes_to_db_shape(axes: Mapping[str, Any], model: str) -> dict[str, str]:
    """軸 (jsonl の dict) → ``get_severity_axes`` と同じ形 (str、真偽は true/false、欠測は省く)。"""
    out = {k: str(axes[k]) for k in STORED_FIELDS if axes.get(k) is not None}
    for k in EXTRA_FIELDS:
        v = axes.get(k)
        if v is None:
            continue
        out[k] = ("true" if v else "false") if k in BOOL_EXTRA_FIELDS else str(v)
    out["model"] = model
    return out


def load_axes_jsonl(path: Path, model: str | None = None) -> dict[str, dict[str, str]]:
    """軸の jsonl → 記事 id → DB と同じ形。``model`` 指定時はその行だけ (別モデルを除く)。"""
    out: dict[str, dict[str, str]] = {}
    for r in _rows(path):
        if model is not None and r.get("model") != model:
            continue
        out[str(r["article_id"])] = axes_to_db_shape(r["axes"], str(r.get("model", "")))
    return out


class StagingOverlayRepo:
    """repo の読みの一部を staging の辞書で置き換える (書きは委譲しない運用)。"""

    def __init__(
        self,
        repo: Any,
        *,
        kinds: Mapping[str, str] | None = None,
        axes: Mapping[str, Mapping[str, str]] | None = None,
    ) -> None:
        self._repo = repo
        self._kinds = kinds
        self._axes = axes
        self.misses: dict[str, int] = {"kinds": 0, "axes": 0}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._repo, name)

    def get_article_kinds(self, article_ids: Sequence[str]) -> dict[str, str]:
        if self._kinds is None:
            result: dict[str, str] = self._repo.get_article_kinds(article_ids)
            return result
        found = {a: self._kinds[a] for a in article_ids if a in self._kinds}
        self.misses["kinds"] += len(set(article_ids)) - len(found)
        return found

    def get_severity_axes(self, article_ids: Sequence[str]) -> dict[str, dict[str, str]]:
        if self._axes is None:
            result: dict[str, dict[str, str]] = self._repo.get_severity_axes(article_ids)
            return result
        found = {a: dict(self._axes[a]) for a in dict.fromkeys(article_ids) if a in self._axes}
        self.misses["axes"] += len(set(article_ids)) - len(found)
        return found


def overlay_if_given(
    repo: Any, *, kinds_path: Path | None, axes_path: Path | None, axes_model: str | None = None
) -> tuple[Any, dict[str, str] | None]:
    """引数が両方 None なら ``(repo, None)`` (従来どおり)。それ以外は overlay と読んだ種別。"""
    if kinds_path is None and axes_path is None:
        return repo, None
    kinds = load_kinds_jsonl(kinds_path) if kinds_path else None
    axes = load_axes_jsonl(axes_path, axes_model) if axes_path else None
    return StagingOverlayRepo(repo, kinds=kinds, axes=axes), kinds
