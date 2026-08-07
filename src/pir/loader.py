"""config/pir.yaml の読み書き。

書き込みは atomic rename (temp file → rename) で半端な状態を作らない。
yaml は YAML 1.1 互換 (PyYAML safe_load)。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import yaml

from src.pir.models import PirConfig

DEFAULT_PIR_YAML_PATH = Path("config/pir.yaml")

# 旧 schema の残存キー (Pir は extra="forbid" のため validate 前に除去が必要)。
# target_channel: R0 配信 override (2026-06-13 撤去、全 PIR が auto のまま未使用だった)
# valid_from〜exclude_signals: 観察モード項目 (2026-07-23 撤去、~2ヶ月で利用 0 件。
# 条件ツリーの keyword AND 枝 / not 節が同じ意図をより正確に表現する)
_LEGACY_PIR_PRIORITY_KEYS = (
    "target_channel",
    "valid_from",
    "valid_until",
    "tags",
    "weak_signals",
    "exclude_signals",
)


# 監査 backlog 2026-07-05: これらの PIR の countries は「アクター国籍」の意図で
# 書かれていたが、evaluator は victim_country 照合のため意味反転していた
# (中国=被害者の記事が中国 APT 動向に計上)。読み込み時に countries → actor_nations
# へ冪等移行する (保存時に現行 schema で書き直されるので自然に定着する)。
_ACTOR_NATION_PIR_IDS = frozenset(
    {"pir_china_apt", "pir_dprk_apt", "pir_russia_apt", "pir_disinfo"}
)


def _migrate_actor_nation_countries(p: dict[str, object]) -> dict[str, object]:
    """既知 APT 系 PIR の countries を actor_nations に移す (冪等)。"""
    if p.get("id") not in _ACTOR_NATION_PIR_IDS:
        return p
    ss = p.get("strong_signals")
    if not isinstance(ss, dict):
        return p
    countries = ss.get("countries")
    if not countries or ss.get("actor_nations"):
        return p
    return {**p, "strong_signals": {**ss, "countries": [], "actor_nations": countries}}


def strip_legacy_pir_keys(raw: dict[str, object]) -> dict[str, object]:
    """過去 schema の残存キー除去 + 既知の意味反転データの正規化を行った新 dict を返す。

    DB (config_store) / yaml seed に残る旧データを現行 schema で読めるようにする
    (保存時に現行 schema で書き直されるので、時間とともに自然消滅する)。
    """
    priorities = raw.get("priorities")
    if not isinstance(priorities, list):
        return raw
    cleaned = [
        _migrate_actor_nation_countries(
            {k: v for k, v in p.items() if k not in _LEGACY_PIR_PRIORITY_KEYS}
        )
        if isinstance(p, dict)
        else p
        for p in priorities
    ]
    return {**raw, "priorities": cleaned}


def load_pir_config(path: Path | None = None) -> PirConfig:
    """config/pir.yaml を読み込んで PirConfig に validate。

    ファイル不在時は空 PirConfig を返す (default behavior 互換性)。
    """
    path = path or DEFAULT_PIR_YAML_PATH
    if not path.exists():
        return PirConfig()

    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    if not isinstance(raw, dict):
        raise ValueError(f"PIR yaml root must be a mapping, got {type(raw).__name__}")

    return PirConfig.model_validate(strip_legacy_pir_keys(raw))


def save_pir_config(config: PirConfig, path: Path | None = None) -> None:
    """PirConfig を yaml に書き出す (atomic rename)。"""
    path = path or DEFAULT_PIR_YAML_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    # mode="json" で datetime / date を ISO 文字列に正規化
    payload = config.model_dump(mode="json", exclude_none=False)

    fd, tmp_path_str = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                payload,
                f,
                allow_unicode=True,
                default_flow_style=False,
                sort_keys=False,
                width=120,
            )
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise
