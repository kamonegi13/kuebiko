"""PIR (Priority Intelligence Requirements) management.

CTI doctrine の中心概念 PIR を first-class entity として扱う。
user 編集可能な PIR list を DB (config_store) に保持し、triage (importance 評価) /
synthesis / spotlight に注入することで、tool 挙動を PIR-driven にする。
配信チャンネルの決定は routing が専属で担う (R0 override は 2026-06-13 撤去)。

主要モジュール:
    models    Pydantic schema (Pir, StrongSignals 等)
    loader    config/delivery/pir.yaml の読み書き
    evaluator article x PIR の match 判定 (preview / KPI で共有)
    compiler  LLM 経由で description → structured fields 生成
    integration  triage / synthesis prompt への注入
"""

from src.pir.loader import load_pir_config, save_pir_config
from src.pir.models import (
    Pir,
    PirConfig,
    PirMetadata,
    RoutingImportance,
    SpotlightConfig,
    SpotlightWindow,
    StrongSignals,
)

__all__ = [
    "Pir",
    "PirConfig",
    "PirMetadata",
    "RoutingImportance",
    "SpotlightConfig",
    "SpotlightWindow",
    "StrongSignals",
    "load_pir_config",
    "save_pir_config",
]
