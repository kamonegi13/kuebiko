"""LLM 接続先レジストリ + OpenAI 互換クライアント + 消費記録のテスト (2026-07-24)。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from src.storage.run_history import RunHistoryRepository
from src.tools import llm_endpoints as ep_mod
from src.tools.llm_client import LLMForbiddenModelError
from src.tools.llm_endpoints import (
    LlmEndpoint,
    endpoint_api_key,
    endpoint_key_env,
    validate_llm_endpoints,
)
from src.tools.openai_compat_client import OpenAICompatClient


def _register(monkeypatch: pytest.MonkeyPatch, *endpoints: LlmEndpoint) -> None:
    """テスト用: レジストリの process キャッシュへ直接注入 (実 DB 非依存)。"""
    monkeypatch.setattr(ep_mod, "_CACHE", {"None": list(endpoints)})


class TestValidate:
    def test_valid(self) -> None:
        doc = {"endpoints": [{"name": "lmstudio", "base_url": "http://192.168.1.10:1234/v1"}]}
        assert validate_llm_endpoints(doc) == []

    def test_bad_name_reserved_dup_url(self) -> None:
        doc = {
            "endpoints": [
                {"name": "OpenAI", "base_url": "https://api.openai.com/v1"},  # 大文字
                {"name": "anthropic", "base_url": "https://x.example/v1"},  # 予約名
                {"name": "ep1", "base_url": "https://a.example/v1"},
                {"name": "ep1", "base_url": "https://b.example/v1"},  # 重複
                {"name": "ep2", "base_url": "ftp://bad"},  # URL 不正
            ]
        }
        errs = validate_llm_endpoints(doc)
        assert len(errs) == 4

    def test_key_env_naming(self) -> None:
        assert endpoint_key_env("ollama-remote") == "LLM_ENDPOINT_KEY_OLLAMA_REMOTE"


class TestKeyResolution:
    def test_env_var_wins(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("LLM_ENDPOINT_KEY_EP1", "from-env")
        assert endpoint_api_key("ep1", env_path=tmp_path / "none.env") == "from-env"

    def test_dotenv_fallback(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv("LLM_ENDPOINT_KEY_EP1", raising=False)
        envf = tmp_path / ".env"
        envf.write_text("LLM_ENDPOINT_KEY_EP1=from-file\n", encoding="utf-8")
        assert endpoint_api_key("ep1", env_path=envf) == "from-file"

    def test_missing_is_empty(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv("LLM_ENDPOINT_KEY_NOPE", raising=False)
        assert endpoint_api_key("nope", env_path=tmp_path / "no.env") == ""


class TestDispatch:
    def test_endpoint_ref_builds_compat_client_with_fallback(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from src.storage.config_store import save_config
        from src.tools.llm_fallback import FallbackLLMClient
        from src.tools.model_tiers import (
            MODEL_TIERS_CONFIG_KEY,
            Step,
            build_llm_for,
            invalidate_model_tiers_cache,
            is_external_model,
        )

        _register(monkeypatch, LlmEndpoint(name="lmstudio", base_url="http://h:1234/v1"))
        db = tmp_path / "d.db"
        monkeypatch.delenv("DATABASE_URL", raising=False)
        save_config(MODEL_TIERS_CONFIG_KEY, {"dialog": "lmstudio:gpt-oss-20b"}, db_path=db)
        invalidate_model_tiers_cache()

        from typing import cast

        from src.config_loader import AppConfig

        class _Cfg:
            ollama_base_url = "http://localhost:11434"
            claude_code_bridge_url = "http://h:8010"
            anthropic_api_key = ""

        client = build_llm_for(Step.ASSISTANT_CHAT, cast(AppConfig, _Cfg()), db_path=db)
        assert isinstance(client, FallbackLLMClient)
        assert client.model == "lmstudio:gpt-oss-20b"
        assert is_external_model("lmstudio:gpt-oss-20b") is True
        assert is_external_model("gemma4:26b") is False

    def test_forbidden_model_rejected_at_construction(self) -> None:
        with pytest.raises(LLMForbiddenModelError):
            OpenAICompatClient("ep1", "qwen-max", base_url="http://h/v1")


def _mock_transport(payload: dict[str, Any]) -> httpx.MockTransport:
    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(_handler)


class TestCompatClient:
    @pytest.mark.asyncio
    async def test_generate_parses_text_and_records_usage(self) -> None:
        recorded: list[tuple[int, int, int]] = []
        client = OpenAICompatClient(
            "lmstudio",
            "gpt-oss-20b",
            base_url="http://h/v1",
            transport=_mock_transport(
                {
                    "choices": [{"message": {"content": "こんにちは"}}],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 34},
                }
            ),
            usage_recorder=lambda i, o, d, c=0, cost=0.0: recorded.append((i, o, d)),
        )
        resp = await client.generate("p")
        assert resp.text == "こんにちは"
        assert client.model == "lmstudio:gpt-oss-20b"
        assert recorded and recorded[0][:2] == (12, 34)

    @pytest.mark.asyncio
    async def test_generate_structured_validates_schema(self) -> None:
        from pydantic import BaseModel

        class _Out(BaseModel):
            label: str

        client = OpenAICompatClient(
            "ep1",
            "m1",
            base_url="http://h/v1",
            transport=_mock_transport(
                {
                    "choices": [{"message": {"content": json.dumps({"label": "ok"})}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 2},
                }
            ),
        )
        out = await client.generate_structured("p", schema=_Out)
        assert out.label == "ok"


class TestUsageStore:
    def test_record_summary_and_purge(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        repo = RunHistoryRepository(db_path=tmp_path / "u.db")
        repo.record_llm_usage(
            provider="lmstudio", model="m1", input_tokens=10, output_tokens=20, duration_ms=500
        )
        repo.record_llm_usage(
            provider="anthropic",
            model="claude-sonnet-5",
            input_tokens=5,
            output_tokens=7,
            duration_ms=100,
        )
        summary = repo.llm_usage_summary()
        assert summary["lmstudio"]["window_5h"]["output_tokens"] == 20
        assert summary["anthropic"]["days7"]["calls"] == 1
        assert repo.purge_old_llm_usage(retention_days=0) == 2

    def test_claudecode_cache_and_cost_recorded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 消費台帳の一本化 (2026-07-26): claudecode は cache 読取と $ 換算も記録し、
        # 集計は旧 bridge payload と同形 (cost_usd_equivalent / last_call_at) で返す
        monkeypatch.delenv("DATABASE_URL", raising=False)
        repo = RunHistoryRepository(db_path=tmp_path / "u2.db")
        repo.record_llm_usage(
            provider="claudecode",
            model="haiku",
            input_tokens=3,
            output_tokens=100,
            duration_ms=900,
            cache_read_tokens=5000,
            cost_usd=0.12,
        )
        repo.record_llm_usage(
            provider="claudecode",
            model="sonnet",
            input_tokens=8,
            output_tokens=200,
            duration_ms=1500,
            cache_read_tokens=10000,
            cost_usd=0.5,
        )
        summary = repo.llm_usage_summary()
        w = summary["claudecode"]["window_5h"]
        assert w["calls"] == 2
        assert w["cache_read_tokens"] == 15000
        assert w["cost_usd_equivalent"] == 0.62
        assert summary["claudecode"]["last_call_at"] is not None
