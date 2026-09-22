"""課題の接頭辞 — 多課題 SFT の負の転移を抑える (2026-09-22)。

⚠ **発端**: detect (96 対) を判定系 5 課題の混合へ足したら **triage が退行**した:

| 指標 | s17 | s18 (detect 込み) |
|---|---|---|
| 26B からの移動 | 4 件 | **49 件 (32.7%)** |
| 降格 (見逃し方向) | — | **43 件 (28.7%)** |
| high → low の反転 | **0 件** | **2 件** |

教師のプロンプトは 5 課題すべてが「あなたは日本の CTI アナリストです」で始まり、
**課題の違いは本文の途中に埋もれていた**。detect は「候補 46 件から 5 件を選ぶ」
絞り込みなので、その挙動が triage へ漏れたと読める。

先行研究: 入力へ固有の接頭辞を付けるとモデルの容量配分が課題ごとに調整され、
負の転移が減る (task-specific instruction prefixes / Task Compass, arXiv 2210.06277)。

⭐ **学習と本番の両方に入れる**。片方だけだと生徒が見たことのない形になる。
⭐ 印を付けるのは **SFT で学習している step だけ**。学習していない step に付けると、
  本番のプロンプトだけが変わって挙動が読めなくなる。
"""

from __future__ import annotations

import os
from typing import TypeVar

from pydantic import BaseModel

from src.tools.llm_client import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_TEMPERATURE,
    MAX_STRUCTURED_ATTEMPTS,
    LLMClient,
    LLMResponse,
)
from src.tools.model_tiers import Step

_T = TypeVar("_T", bound=BaseModel)

_FLAG = "SFT_TASK_PREFIX"

#: step → 接頭辞。**SFT の教師データを持つ step だけ**に付ける。
#: 値は短く、記事本文に現れない形にする (衝突すると本文が課題指示に見える)。
TASK_MARKERS: dict[Step, str] = {
    Step.TRIAGE: "[task: triage]\n",
    Step.ARTICLE_SUMMARY: "[task: summary]\n",
    Step.PAIR_JUDGE: "[task: pair]\n",
    Step.EVENT_KIND: "[task: kind]\n",
    Step.PIR_LLM_JUDGE: "[task: pir]\n",
    Step.SYNTHESIS_DETECT: "[task: detect]\n",
    Step.SYNTHESIS_ANALYSIS: "[task: ach]\n",
    Step.EVENT_NEWS: "[task: event_news]\n",
    Step.PIR_SPOTLIGHT: "[task: spotlight]\n",
    Step.SYNTHESIS_NARRATIVE: "[task: synthesis]\n",
    Step.DIGEST_DEEP_DIVE_SELECT: "[task: deep_dive_select]\n",
    Step.DIGEST_DEEP_DIVE: "[task: deep_dive]\n",
}


def task_prefix_enabled() -> bool:
    """接頭辞を付けるか。**既定 OFF** (``SFT_TASK_PREFIX=1`` で有効化)。

    ⚠⚠ 既定を ON にしてはいけない。常駐モデル (s17 / n17m30 / n17c) は **接頭辞なしで
    学習されている**ため、コードを入れた瞬間に「見たことのない形」のプロンプトが届く。
    接頭辞つきで学習したモデルを配備したときに、同じ版で旗を立てる。
    """
    return os.environ.get(_FLAG, "0").strip() in ("1", "true", "True")


def prefix_for(step: Step) -> str:
    """step の接頭辞 (学習していない step は空文字)。"""
    return TASK_MARKERS.get(step, "")


def with_task_prefix(prompt: str, step: Step) -> str:
    """プロンプトの先頭へ課題の印を付ける (二重付与はしない)。"""
    if not task_prefix_enabled():
        return prompt
    marker = prefix_for(step)
    if not marker or prompt.startswith(marker):
        return prompt
    return marker + prompt


class TaskPrefixClient(LLMClient):
    """プロンプトの先頭へ課題の印を付けて内側 client へ渡す wrapper。

    ⭐ **本番の全経路に一度で効かせるため factory (``model_tiers.build_llm_for``) で包む**。
    call site を 1 つずつ直すと必ず漏れる (漏れた経路だけ学習時と形が違う)。
    """

    def __init__(self, inner: LLMClient, step: Step) -> None:
        self._inner = inner
        self._step = step

    @property
    def model(self) -> str:
        return self._inner.model

    async def generate(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        think: bool | None = None,
    ) -> LLMResponse:
        return await self._inner.generate(
            with_task_prefix(prompt, self._step),
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            think=think,
        )

    async def generate_structured(
        self,
        prompt: str,
        schema: type[_T],
        system: str | None = None,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        think: bool | None = None,
        max_attempts: int = MAX_STRUCTURED_ATTEMPTS,
    ) -> _T:
        return await self._inner.generate_structured(
            with_task_prefix(prompt, self._step),
            schema,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            think=think,
            max_attempts=max_attempts,
        )
