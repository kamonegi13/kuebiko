"""Spotlight の period 解決と PIR config の取得元を固定する。

2026-08-30 に踏んだ 2 つの静かな故障を再発させないための関門:

1. ``pipelines.yaml`` に書いた ``rolling7`` が固定タプルの許可リストに無く、
   **エラーを出さずに weekly へ落ちていた** (04:50 の実行が週次を作っていた)
2. 実行時 SSoT は DB なのに Spotlight の PIR 取得だけ **seed yaml を直読み**して
   おり、DB で 20 件へ広げた spotlight.enabled が届かず 5 件しか回らなかった
"""

from __future__ import annotations

from typing import get_args
from unittest.mock import patch

import pytest

from src.config_loader import PipelineConfig
from src.pipeline.runners import _resolve_spotlight_period
from src.spotlight.models import SpotlightPeriod


def _pipeline(periods: list[str]) -> PipelineConfig:
    return PipelineConfig.model_validate(
        {
            "name": "pir-spotlight",
            "source": {"type": "pir_spotlight", "synthesis_periods": periods},
            "processor": {"extract_method": "trafilatura", "target_language": "ja"},
        }
    )


@pytest.mark.parametrize("period", sorted(get_args(SpotlightPeriod)))
def test_every_spotlight_period_reaches_the_runner(period: str) -> None:
    """SpotlightPeriod に足した値は必ず pipelines.yaml から届く。

    許可リストを固定タプルで持つと、新しい period を足したときに黙って
    既定へ落ちる。SSoT (SpotlightPeriod) から導出していればここが失敗しない。
    """
    assert _resolve_spotlight_period(_pipeline([period])) == period


def test_unknown_period_falls_back_to_weekly() -> None:
    assert _resolve_spotlight_period(_pipeline(["fortnightly"])) == "weekly"


def test_empty_periods_fall_back_to_weekly() -> None:
    assert _resolve_spotlight_period(_pipeline([])) == "weekly"


def test_unknown_period_is_logged_not_silent() -> None:
    """宣言があるのに 1 つも通らないときは黙って落ちない (静かな故障の防止)。"""
    with patch("src.pipeline.runners._log") as log:
        _resolve_spotlight_period(_pipeline(["fortnightly"]))
    assert log.warning.called


def test_spotlight_runner_reads_runtime_config_not_seed_yaml() -> None:
    """PIR の取得は DB 正 (get_pir_config)。seed の yaml 直読みに戻さない。"""
    import inspect

    import src.spotlight.runner as runner

    src = inspect.getsource(runner)
    assert "get_pir_config" in src
    assert "load_pir_config" not in src


def test_spotlight_regenerate_api_reads_runtime_config() -> None:
    """同型の経路 (API の regenerate) も同じ取得元にする。片方だけ直さない。"""
    import inspect

    import src.ui.api.spotlight as api

    src = inspect.getsource(api)
    assert "get_pir_config" in src
    assert "load_pir_config" not in src


def test_bridge_reports_unreadable_env_file() -> None:
    """共有 .env が読めないとき、黙って env fallback へ落ちない。

    単一ファイルの bind mount はホスト側の置き換え (inode 交代) で参照先を失う。
    app だけを再作成する運用では sidecar のマウントだけが古いまま残り、
    **認証が静かに消える** (2026-08-30: 15 時間ローカルへ落ちていた)。
    """
    import importlib.util
    from pathlib import Path
    from unittest.mock import patch

    spec = importlib.util.spec_from_file_location(
        "claude_code_bridge", Path("scripts/claude_code_bridge.py")
    )
    assert spec is not None and spec.loader is not None
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)

    bridge._ENV_FILE_ERROR.clear()
    with patch.dict(
        "os.environ",
        {"BRIDGE_ENV_FILE": "/nonexistent/.env", "CLAUDE_CODE_OAUTH_TOKEN": ""},
        clear=False,
    ):
        assert bridge.resolve_oauth_token() == ""
    assert bridge._ENV_FILE_ERROR.get("reason"), "読取失敗を記録していない"
