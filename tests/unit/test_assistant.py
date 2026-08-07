"""分析チャット (src/assistant/) の unit test。

orchestrator は fake LLM で plan → ツール実行 → 接地回答の配線を、tools は引数 clamp と
read-only handler の防御 (未知ツール / 実行エラーを turn 全体に波及させない) を検証する。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src.assistant.orchestrator import AssistantPlan, ToolCallPlan, chat_turn
from src.assistant.tools import ToolContext, _clamp_int, execute_tool
from src.storage.run_history import RunHistoryRepository


@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    db = tmp_path / "assistant.db"
    repo = RunHistoryRepository(db_path=db)
    return ToolContext(repo=repo, embedder=None, db_path=db)


class _FakeLLM:
    """plan (structured) と answer (text) を差し替え可能な fake。"""

    def __init__(self, *, plan: AssistantPlan | None = None, answer: str = "回答です") -> None:
        self._plan = plan
        self._answer = answer
        self.prompts: list[str] = []

    @property
    def model(self) -> str:
        return "fake-model"

    async def generate_structured(self, prompt: str, *, schema: Any, think: bool) -> Any:
        self.prompts.append(prompt)
        if self._plan is None:
            raise RuntimeError("plan failed")
        return self._plan

    async def generate(self, prompt: str, *, think: bool) -> Any:
        self.prompts.append(prompt)
        return SimpleNamespace(text=self._answer)


class TestChatTurn:
    @pytest.mark.asyncio
    async def test_plan_tools_executed_and_results_injected(self, ctx: ToolContext) -> None:
        call = ToolCallPlan(tool="article_stats", args={"days": 7, "group_by": "category"})
        plan = AssistantPlan(tool_calls=[call])
        llm = _FakeLLM(plan=plan, answer="7 日間の集計は 0 件です。")
        result = await chat_turn(llm=llm, ctx=ctx, message="集計して")  # type: ignore[arg-type]

        assert result.answer == "7 日間の集計は 0 件です。"
        assert len(result.tools) == 1
        assert "件数集計" in result.tools[0]["summary"]
        # answer プロンプトにツール結果 JSON が注入されている
        assert '"tool": "article_stats"' in llm.prompts[-1]
        assert result.model == "fake-model"

    @pytest.mark.asyncio
    async def test_unknown_tool_is_skipped_not_crash(self, ctx: ToolContext) -> None:
        plan = AssistantPlan(tool_calls=[ToolCallPlan(tool="drop_tables", args={})])
        llm = _FakeLLM(plan=plan)
        result = await chat_turn(llm=llm, ctx=ctx, message="x")  # type: ignore[arg-type]
        assert "未知のツール" in result.tools[0]["summary"]
        assert result.answer  # 回答は生成される

    @pytest.mark.asyncio
    async def test_tool_calls_capped_at_three(self, ctx: ToolContext) -> None:
        calls = [
            ToolCallPlan(tool="article_stats", args={"group_by": "category"}) for _ in range(5)
        ]
        llm = _FakeLLM(plan=AssistantPlan(tool_calls=calls))
        result = await chat_turn(llm=llm, ctx=ctx, message="x")  # type: ignore[arg-type]
        assert len(result.tools) == 3

    @pytest.mark.asyncio
    async def test_plan_failure_degrades_to_no_tools(self, ctx: ToolContext) -> None:
        llm = _FakeLLM(plan=None, answer="こんにちは")
        result = await chat_turn(llm=llm, ctx=ctx, message="こんにちは")  # type: ignore[arg-type]
        assert result.tools == []
        assert result.answer == "こんにちは"
        assert "(ツール実行なし)" in llm.prompts[-1]


class TestTools:
    def test_clamp_int_bounds_and_garbage(self) -> None:
        assert _clamp_int("500", lo=1, hi=365, default=30) == 365
        assert _clamp_int(-3, lo=1, hi=365, default=30) == 1
        assert _clamp_int("abc", lo=1, hi=365, default=30) == 30
        assert _clamp_int(None, lo=1, hi=365, default=30) == 30

    @pytest.mark.asyncio
    async def test_stats_rejects_unknown_group_by(self, ctx: ToolContext) -> None:
        payload, summary = await execute_tool("article_stats", {"group_by": "week; DROP"}, ctx)
        assert "error" in payload
        assert "skip" in summary

    @pytest.mark.asyncio
    async def test_search_requires_query(self, ctx: ToolContext) -> None:
        payload, summary = await execute_tool("search_articles", {}, ctx)
        assert "error" in payload
        assert "skip" in summary

    @pytest.mark.asyncio
    async def test_search_on_empty_db_returns_no_articles(self, ctx: ToolContext) -> None:
        payload, _ = await execute_tool(
            "search_articles", {"query": "Volt Typhoon", "days": 7}, ctx
        )
        assert payload["articles"] == []
        assert payload["window_days"] == 7

    @pytest.mark.asyncio
    async def test_list_situations_on_empty_db(self, ctx: ToolContext) -> None:
        payload, _ = await execute_tool("list_situations", {"query": "", "limit": 5}, ctx)
        assert payload["situations"] == []

    @pytest.mark.asyncio
    async def test_tool_error_is_contained(self, ctx: ToolContext) -> None:
        broken = ToolContext(repo=ctx.repo, embedder=None, db_path=Path("/nonexistent/x.db"))
        payload, summary = await execute_tool("article_stats", {"group_by": "category"}, broken)
        assert "error" in payload
        assert "エラー" in summary


class TestActorProfile:
    """actor_profile: 別名→正規名の辞書解決 (実 config/actor_aliases.yaml に依存)。"""

    @pytest.mark.asyncio
    async def test_alias_resolves_to_canonical(self, ctx: ToolContext) -> None:
        payload, summary = await execute_tool("actor_profile", {"name": "Cicada"}, ctx)
        assert payload["found"] is True
        assert payload["knowledge"]["canonical"] == "APT10"
        assert "Cicada" in payload["knowledge"]["aliases"]
        assert "APT10" in summary
        # 空 DB でも活動 0 件 (dormant) として knowledge は返る
        assert payload["activity"]["articles"] == 0

    @pytest.mark.asyncio
    async def test_unknown_name_returns_not_found(self, ctx: ToolContext) -> None:
        payload, summary = await execute_tool(
            "actor_profile", {"name": "Nonexistent Actor XYZ"}, ctx
        )
        assert payload["found"] is False
        assert "未収載" in summary

    @pytest.mark.asyncio
    async def test_requires_name(self, ctx: ToolContext) -> None:
        payload, summary = await execute_tool("actor_profile", {}, ctx)
        assert "error" in payload
        assert "skip" in summary

    @pytest.mark.asyncio
    async def test_search_articles_annotates_alias_resolution(self, ctx: ToolContext) -> None:
        payload, _ = await execute_tool(
            "search_articles", {"query": "Cicada の最近の動向", "days": 30}, ctx
        )
        res = payload.get("actor_name_resolution")
        assert res and res[0]["canonical"] == "APT10"
