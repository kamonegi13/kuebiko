"""key_events の上限は黙って効かせない (2026-09-19、SYNTHESIS §53 訂正)。

09-19 に凍結評価で 1 窓 126 件の暴走を見つけたが、**本番の log には何も残っていなかった** —
`_KEY_EVENTS_MAX = 10` が黙って切っていたため。切り捨ては読者を守るが、切ったことが
記録されないとモデルの暴走を検知できない。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest


@dataclass
class _FakeMatch:
    """`test_spotlight_identifier_gate.py` と同じ形 (生成器が読む属性に合わせる)。"""

    article_id: str
    title: str = "t"
    url: str = "https://kuebiko.example/a"
    feed_title: str = "feed"
    importance: str = "high"
    created_at: str = "2026-09-01T00:00:00+00:00"
    summary: str = "悪用が観測された。"
    feed_url: str = ""


class _FloodLLM:
    """上限を超える key_events を返す fake。"""

    model = "fake"

    def __init__(self, n: int) -> None:
        self.n = n

    async def generate_structured(self, prompt: str, *, schema: Any, **_kw: Any) -> Any:
        return schema(
            headline="見出し",
            key_events=[{"index": (i % 6) + 1} for i in range(self.n)],
            outlook="見通し",
            caveats=["留保"],
            unknowns=["未解明"],
        )


@dataclass
class _FakePir:
    id: str = "pir_test"
    title: str = "test"
    enabled: bool = True
    spotlight: Any = field(default_factory=lambda: type("S", (), {"enabled": True})())


def _patch(monkeypatch: pytest.MonkeyPatch) -> None:
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
    monkeypatch.setenv("SPOTLIGHT_TAIL_GATE", "0")
    monkeypatch.setenv("SPOTLIGHT_IDENTIFIER_GATE", "0")


@pytest.mark.asyncio
async def test_truncation_is_logged_with_the_produced_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.spotlight.generator as gen

    _patch(monkeypatch)
    seen: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(gen._log, "warning", lambda event, **kw: seen.append((event, kw)))

    record = await gen.generate_spotlight(
        _FakePir(),  # type: ignore[arg-type]
        llm=_FloodLLM(126),  # type: ignore[arg-type]
        assessment=object(),  # type: ignore[arg-type]
        now=datetime(2026, 9, 6, tzinfo=UTC),
    )

    assert record is not None
    assert len(record.key_events) <= gen._KEY_EVENTS_MAX  # 読者には届かない
    truncated = [kw for ev, kw in seen if ev == "spotlight_key_events_truncated"]
    assert truncated, "切ったことが記録されていない"
    assert truncated[0]["produced"] == 126  # 何件出たかが分かる
    assert truncated[0]["cap"] == gen._KEY_EVENTS_MAX


@pytest.mark.asyncio
async def test_no_log_when_within_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.spotlight.generator as gen

    _patch(monkeypatch)
    seen: list[str] = []
    monkeypatch.setattr(gen._log, "warning", lambda event, **kw: seen.append(event))

    await gen.generate_spotlight(
        _FakePir(),  # type: ignore[arg-type]
        llm=_FloodLLM(5),  # type: ignore[arg-type]
        assessment=object(),  # type: ignore[arg-type]
        now=datetime(2026, 9, 6, tzinfo=UTC),
    )

    assert "spotlight_key_events_truncated" not in seen
