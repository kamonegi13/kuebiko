"""台帳の割当判定モデル (学習済みランダムフォレストの numpy 推論、2026-09-24)。

⭐ sklearn を本番に持ち込まない — 学習はオフラインで行い、木を JSON に書き出してここで推論する
(群化の ``eventnews.pair_model`` と同じ方式)。モデルが無い・壊れている・特徴量の並びが違う
ときは None を返し、呼び手は埋込の関門 (``assign_gate``) へ退避する。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from src.logging_config import get_logger
from src.tools.model_tiers import Step, resolve_step_model
from src.tools.tree_forest import forest_probability

_log = get_logger(__name__)

DEFAULT_MODEL_PATH = Path("config/models/assign_model.json")
#: probe 特徴量ありのモデルの既定パス (F2b、2026-10-11)。base モデルとは別ファイル —
#: 無ければ呼び出し側は base モデル (``load_assign_model``) にそのまま退避する。
PROBE_MODEL_PATH = Path("config/models/assign_model_probe.json")
_ENABLE_ENV = "ASSIGN_ML"
#: probe モデル JSON の ``probe_model`` (特徴量を作った判定器) が無いときの扱い。
#: 段 1 (False) = 照合せず従来どおり使う + 警告。JSON にキーを足した次のデプロイで True に
#: 締める (キー無し = None = base へ退避)。コードを先に出して JSON を後にするための 2 段。
REQUIRE_PROBE_MODEL_KEY = False


def assign_ml_enabled() -> bool:
    """割当の判定に ML を使うか (``ASSIGN_ML=1``)。モデルが無ければ使えない。"""
    return os.environ.get(_ENABLE_ENV, "0").strip() == "1"


@dataclass(frozen=True)
class AssignModel:
    feature_names: tuple[str, ...]
    threshold: float
    _trees: tuple[dict[str, list[float]], ...]
    #: この木が学習された時の特徴量 (probe の logit) を出した判定器。無ければ None (不明)。
    probe_model: str | None = None

    def probability(self, features: list[float]) -> float:
        """同じ情勢である確率 (木ごとの葉の比率の平均)。"""
        if len(features) != len(self.feature_names):
            raise ValueError(
                f"特徴量の数が合いません: {len(features)} != {len(self.feature_names)}"
            )
        return forest_probability(self._trees, features)

    def assigns(self, features: list[float]) -> bool:
        return self.probability(features) >= self.threshold


def _load_model(
    target: Path, expected_names: tuple[str, ...], *, event_prefix: str
) -> AssignModel | None:
    """モデル JSON を読んで検証する (``load_assign_model`` / ``load_assign_model_probe`` 共通本体)。

    無い・壊れている・特徴量の並びが違う → None (呼び出し側はより単純な判定へ退避する)。
    """
    if not target.exists():
        return None
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        got = tuple(raw["feature_names"])
        if got != expected_names:
            _log.warning(
                f"{event_prefix}_feature_mismatch", expected=len(expected_names), got=len(got)
            )
            return None
        declared = raw.get("probe_model")
        return AssignModel(
            feature_names=got,
            threshold=float(raw["threshold"]),
            _trees=tuple(raw["trees"]),
            probe_model=declared.strip()
            if isinstance(declared, str) and declared.strip()
            else None,
        )
    except Exception as e:  # noqa: BLE001 — 壊れたモデルで台帳を止めない
        _log.warning(f"{event_prefix}_load_failed", path=str(target), error=str(e)[:200])
        return None


@lru_cache(maxsize=2)
def load_assign_model(path: str | None = None) -> AssignModel | None:
    """モデルを読む。無い・壊れている・並びが違う → None (埋込の関門へ退避)。"""
    from src.assessment.assign_features import FEATURE_NAMES

    target = Path(path) if path else DEFAULT_MODEL_PATH
    return _load_model(target, FEATURE_NAMES, event_prefix="assign_model")


@lru_cache(maxsize=2)
def _load_probe_file(path: str | None) -> AssignModel | None:
    """probe モデル JSON の読み込み (ファイル単位のキャッシュ。判定器の照合は呼び出しごと)。"""
    from src.assessment.assign_features import FEATURE_NAMES_WITH_PROBE

    target = Path(path) if path else PROBE_MODEL_PATH
    return _load_model(target, FEATURE_NAMES_WITH_PROBE, event_prefix="assign_model_probe")


def _probe_model_matches(model: AssignModel) -> bool:
    """特徴量を作った判定器が、いま割り当たっている判定器と同じか。

    ``probe_model`` 未記載は段 1 では通す (警告のみ)。実モデルを引けないときも従来どおり通す
    (照合できないことで台帳の割当を止めない)。
    """
    if model.probe_model is None:
        _log.warning("assign_model_probe_model_undeclared", required=REQUIRE_PROBE_MODEL_KEY)
        return not REQUIRE_PROBE_MODEL_KEY
    try:
        actual = resolve_step_model(Step.ASSIGN_EVIDENCE_JUDGE).strip()
    except Exception as e:  # noqa: BLE001 — 照合の失敗で割当を止めない
        _log.warning("assign_model_probe_resolve_failed", error=str(e)[:200])
        return True
    if actual == model.probe_model:
        return True
    _log.warning("assign_model_probe_model_mismatch", trained=model.probe_model, actual=actual)
    return False


def load_assign_model_probe(path: str | None = None) -> AssignModel | None:
    """probe 特徴量 (gemma4:26b の P(yes)) ありのモデルを読む (F2b、2026-10-11)。

    無い・壊れている・並びが違う・特徴量を作った判定器が割当と違う → None — 呼び出し側は
    base モデル (``load_assign_model``) にそのまま退避する (`ASSIGN_PROBE_FEATURE=1` でも
    base の挙動を壊さない)。判定器の照合は呼び出しごとに行う (割当の変更に追随する)。
    """
    model = _load_probe_file(path)
    if model is None or not _probe_model_matches(model):
        return None
    return model
