"""spotlight 尾部最小件数関門のテスト (2026-09-11)。

3 段で固定する (識別子関門と同じ構図):
1. 判定器 (最小件数を下回る欄を返す / 充足なら空)
2. 配線 — generate_spotlight が不足時に同一プロンプトで再サンプルし、最良候補を採る
3. 上限後も不足なら保存を止めず最良候補を採用する / 旗 SPOTLIGHT_TAIL_GATE=0 で無効
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from src.spotlight.tail_gate import MAX_RESAMPLES, tail_deficit, tail_score


@dataclass
class _Out:
    caveats: list[str]
    unknowns: list[str]


class TestTailDeficit:
    def test_both_empty_reports_both(self) -> None:
        assert tail_deficit(_Out([], [])) == ("caveats", "unknowns")

    def test_single_item_is_deficit(self) -> None:
        assert tail_deficit(_Out(["a"], ["b", "c"])) == ("caveats",)

    def test_two_each_is_sufficient(self) -> None:
        assert tail_deficit(_Out(["a", "b"], ["c", "d"])) == ()

    def test_score_is_total_count(self) -> None:
        assert tail_score(_Out(["a"], ["b", "c"])) == 3


# ---------- 配線 ----------


@dataclass
class _FakeMatch:
    article_id: str
    title: str = "t"
    url: str = "https://example.com/a"
    feed_title: str = "feed"
    importance: str = "high"
    created_at: str = "2026-09-01T00:00:00+00:00"
    summary: str = "要約。"
    feed_url: str = ""


class _FakeLLM:
    """呼出ごとに指定された尾部 (caveats, unknowns) を返す fake。"""

    model = "fake"

    def __init__(self, tails: list[tuple[list[str], list[str]]]) -> None:
        self.tails = tails
        self.prompts: list[str] = []

    async def generate_structured(self, prompt: str, *, schema: Any, **_kw: Any) -> Any:
        self.prompts.append(prompt)
        caveats, unknowns = self.tails[min(len(self.prompts) - 1, len(self.tails) - 1)]
        return schema(
            headline="見出し",
            key_events=[{"index": i + 1} for i in range(5)],
            outlook="展望。",
            caveats=caveats,
            unknowns=unknowns,
        )


@dataclass
class _FakePir:
    id: str = "pir_test"
    title: str = "test"
    enabled: bool = True
    spotlight: Any = field(default_factory=lambda: type("S", (), {"enabled": True})())


@pytest.fixture()
def _patched(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.spotlight.generator as gen

    matches = [_FakeMatch(article_id=f"a{i}") for i in range(6)]
    monkeypatch.setattr(gen, "evaluate_pir_matches", lambda *a, **k: matches)
    monkeypatch.setattr(gen, "_build_prompt", lambda *a, **k: "PROMPT")
    monkeypatch.setattr(gen, "_load_previous_spotlight", lambda **k: None)
    monkeypatch.setattr(gen, "_build_ledger_context", lambda *a, **k: [])

    class _FakeRepo:
        def entities_for_articles(self, *a: Any, **k: Any) -> dict[str, list[str]]:
            return {}

    monkeypatch.setattr(gen, "RunHistoryRepository", _FakeRepo)
    monkeypatch.setenv("SPOTLIGHT_IDENTIFIER_GATE", "0")  # 本テストの関心外
    monkeypatch.delenv("SPOTLIGHT_TAIL_GATE", raising=False)


async def _run(llm: _FakeLLM) -> Any:
    from src.spotlight.generator import generate_spotlight

    return await generate_spotlight(
        _FakePir(),  # type: ignore[arg-type]  # Pir 相当の最小 stub
        llm=llm,  # type: ignore[arg-type]
        assessment=object(),  # type: ignore[arg-type]  # _build_prompt patch 済で未使用
        now=datetime(2026, 9, 11, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_sufficient_tail_is_not_resampled(_patched: None) -> None:
    llm = _FakeLLM([(["c1", "c2"], ["u1", "u2"])])
    record = await _run(llm)
    assert record is not None
    assert len(llm.prompts) == 1


@pytest.mark.asyncio
async def test_empty_tail_is_resampled_until_sufficient(_patched: None) -> None:
    llm = _FakeLLM([([], []), (["c1", "c2"], ["u1", "u2", "u3"])])
    record = await _run(llm)
    assert record is not None
    assert len(llm.prompts) == 2
    assert llm.prompts[1] == llm.prompts[0]  # 指示を足さず同一プロンプトで再サンプル
    assert record.caveats == ["c1", "c2"]
    assert record.unknowns == ["u1", "u2", "u3"]


@pytest.mark.asyncio
async def test_best_candidate_kept_when_limit_reached(_patched: None) -> None:
    # 全候補が不足 → 上限で打ち切り、最も充足した候補 (2 回目) を採用して保存は止めない
    llm = _FakeLLM([([], []), (["c1"], ["u1"]), ([], ["u1"])])
    record = await _run(llm)
    assert record is not None
    assert len(llm.prompts) == 1 + MAX_RESAMPLES
    assert record.caveats == ["c1"]
    assert record.unknowns == ["u1"]


@pytest.mark.asyncio
async def test_gate_disabled_by_flag(_patched: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPOTLIGHT_TAIL_GATE", "0")
    llm = _FakeLLM([([], [])])
    record = await _run(llm)
    assert record is not None
    assert len(llm.prompts) == 1  # rollback: 従来挙動
    assert record.caveats == []
