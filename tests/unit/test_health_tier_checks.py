"""ティア割当モデルの提供元対応 死活チェック (2026-07-20) の unit test。

外部割当 (claudecode:/anthropic:) を Ollama の pull 済確認に流して ERROR 誤判定して
いた問題の回帰防止。外部到達不可はフォールバックで運用継続するため warning。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from src.tools.model_tiers import (
    MODEL_TIERS_CONFIG_KEY,
    Tier,
    invalidate_model_tiers_cache,
)
from src.ui.services.health import check_tier_model


def _cfg() -> Any:
    return SimpleNamespace(
        ollama_base_url="http://127.0.0.1:1",  # 到達不可 port
        # 未使用 port = 接続拒否 → bridge 停止扱い
        claude_code_bridge_url="http://127.0.0.1:1",
        anthropic_api_key="",
    )


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MODEL_TIERS_CONFIG_DB", raising=False)
    invalidate_model_tiers_cache()
    yield tmp_path / "health.db"
    invalidate_model_tiers_cache()


def _assign(db_path: Any, tier: Tier, model: str) -> None:
    from src.storage.config_store import save_config

    save_config(MODEL_TIERS_CONFIG_KEY, {tier.value: model}, db_path=db_path)
    invalidate_model_tiers_cache()


class TestTierHealthDispatch:
    @pytest.mark.asyncio
    async def test_claudecode_bridge_down_is_warning_not_error(
        self, _clean_cache: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.tools.model_tiers as mt

        _assign(_clean_cache, Tier.REASONING, "claudecode:sonnet")
        monkeypatch.setattr(mt, "resolve_tier_model", lambda tier, **kw: "claudecode:sonnet")
        hc = await check_tier_model(_cfg(), Tier.REASONING, "reasoning")
        assert hc.status == "warning"  # ERROR にしない (自動切替で継続するため)
        assert "自動切替" in hc.detail
        assert hc.name == "llm_reasoning"

    @pytest.mark.asyncio
    async def test_anthropic_without_key_is_warning(
        self, _clean_cache: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.tools.model_tiers as mt

        monkeypatch.setattr(
            mt, "resolve_tier_model", lambda tier, **kw: "anthropic:claude-sonnet-5"
        )
        hc = await check_tier_model(_cfg(), Tier.REASONING, "reasoning")
        assert hc.status == "warning"
        assert "ANTHROPIC_API_KEY 未設定" in hc.detail

    @pytest.mark.asyncio
    async def test_local_ref_still_uses_ollama_check(
        self, _clean_cache: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.tools.model_tiers as mt

        monkeypatch.setattr(mt, "resolve_tier_model", lambda tier, **kw: "gemma4:31b")
        hc = await check_tier_model(_cfg(), Tier.REASONING, "reasoning")
        # Ollama 到達不可 → 従来どおり error (ローカルはフォールバックが無い真の障害)
        assert hc.status == "error"
        assert hc.name == "ollama_reasoning"
