"""要約の暴走の検出・修復・作り直し (2026-09-27、src/pipeline/summary_runaway.py)。"""

from __future__ import annotations

from typing import Any

import pytest

from src.pipeline.summary import SummaryOutput
from src.pipeline.summary_runaway import repair, runaway_reasons

_S = "英国の政府機関が相次いで侵害され、10 万人を超える職員の情報が流出しました。"


def _summary(**kw: Any) -> SummaryOutput:
    base: dict[str, Any] = {
        "title_ja": "見出し",
        "importance": "medium",
        "category": "breach",
        "summary": "正常な要約です。攻撃者は不明です。",
    }
    return SummaryOutput(**{**base, **kw})


def test_normal_summary_has_no_reasons() -> None:
    assert runaway_reasons(_summary(mitre_techniques=["T1190", "T1078"])) == []


def test_repeated_sentence_is_a_runaway() -> None:
    got = runaway_reasons(_summary(summary=_S * 5))

    assert got == ["summary:sentence_repeat"]


def test_repeated_list_items_are_a_runaway() -> None:
    got = runaway_reasons(_summary(mitre_techniques=["T1566.001", "T1059"] * 6))

    assert got == ["mitre_techniques:list_dup"]


def test_repair_collapses_repeats_without_mutating() -> None:
    broken = _summary(summary="冒頭の文はここにあります。" + _S * 4, mitre_techniques=["T1"] * 9)

    fixed = repair(broken)

    assert fixed.summary == "冒頭の文はここにあります。" + _S
    assert fixed.mitre_techniques == ["T1"]
    assert broken.mitre_techniques == ["T1"] * 9  # 元は変わらない
    assert runaway_reasons(fixed) == []


def test_list_caps_appear_only_in_the_schema() -> None:
    schema = SummaryOutput.model_json_schema()["properties"]

    assert schema["mitre_techniques"]["maxItems"] == 30
    assert schema["iocs"]["maxItems"] == 200
    # 検証では落とさない (JSON 修復・保存済みデータの超過で本番を止めない)
    assert len(_summary(iocs=["x"] * 300).iocs) == 300


class _FakeLLM:
    model = "fake"

    def __init__(self, outputs: list[SummaryOutput]) -> None:
        self._outputs = list(outputs)
        self.calls = 0

    async def generate_structured(self, prompt: str, *, schema: Any, think: bool) -> Any:
        self.calls += 1
        return self._outputs.pop(0)


@pytest.mark.asyncio
async def test_runaway_is_regenerated_once() -> None:
    from src.pipeline.briefing import _summarize_without_runaway

    llm = _FakeLLM([_summary(summary=_S * 5), _summary()])

    got = await _summarize_without_runaway(llm, "p", think=False, url="u")  # type: ignore[arg-type]

    assert llm.calls == 2
    assert runaway_reasons(got) == []


@pytest.mark.asyncio
async def test_second_runaway_is_repaired() -> None:
    from src.pipeline.briefing import _summarize_without_runaway

    llm = _FakeLLM([_summary(summary=_S * 5), _summary(summary=_S * 6)])

    got = await _summarize_without_runaway(llm, "p", think=False, url="u")  # type: ignore[arg-type]

    assert llm.calls == 2
    assert got.summary == _S


@pytest.mark.asyncio
async def test_normal_summary_is_not_regenerated() -> None:
    from src.pipeline.briefing import _summarize_without_runaway

    llm = _FakeLLM([_summary()])

    await _summarize_without_runaway(llm, "p", think=False, url="u")  # type: ignore[arg-type]

    assert llm.calls == 1
