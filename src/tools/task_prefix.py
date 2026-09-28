"""課題の接頭辞 — 多課題 SFT の負の転移を抑える (2026-09-22)。

⚠ **発端**: detect (96 対) を判定系 5 課題の混合へ足した s18 で、**event_kind が崩壊**した
(凍結 300 問で s17 278 → s18 13。出力が ``thought_token_token…`` のような語の反復に
なる。pair_judge は s17 と全問同一なので取込の失敗ではない)。

⚠⚠ 当初は「triage が退行した (移動 4 → 49 件)」と書いていたが **測定の取り違え**だった
(2026-09-23 訂正)。s17 の「4 件」は s16 との相互不一致、s18 の「49 件」は day-0 26B
からの移動で、基準が違う。s17 も 26B からは移動 49 件・降格 43 件で、triage は同水準。

教師のプロンプトは課題の多くが「あなたは日本の CTI アナリストです」で始まり、
**課題の違いは本文の途中に埋もれていた**。課題の境界が曖昧なまま混ぜたことが
崩壊の一因と見て、接頭辞で境界を明示する (効果は双子の対照 s19np で測る)。

先行研究: 入力へ固有の接頭辞を付けるとモデルの容量配分が課題ごとに調整され、
負の転移が減る (task-specific instruction prefixes / Task Compass, arXiv 2210.06277)。

⭐ **学習と本番の両方に入れる**。片方だけだと生徒が見たことのない形になる。
⭐ **系列の先頭に置く** (system があれば system の先頭)。system の後ろに置くと、課題を
  定義する長い文を読んだ後に印が来るため、切替の合図として働きにくい。
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

_EXTRA_MODELS_ENV = "SFT_TASK_PREFIX_MODELS"

#: 接頭辞つきで学習したモデル。**「印で学習したか」はモデル固有の性質**なので、ここに
#: 名前を足した時点で、UI でそのモデルを割り当てるだけで印が付く (環境変数の設定漏れで
#: 黙って外れる事故を構造で消す — s19 から印を外すと event_kind が 277 → 257 に落ちた)。
#: 評価中の新モデルは環境変数 ``SFT_TASK_PREFIX_MODELS`` (カンマ区切り) で一時的に足す。
PREFIX_TRAINED_MODELS: frozenset[str] = frozenset({"kuebiko-sft:s19", "kuebiko-sft:s21"})

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


def task_prefix_enabled(model: str) -> bool:
    """このモデルに接頭辞を付けるか (= 接頭辞つきで学習したモデルか)。

    ⚠⚠ 2026-09-24 まではプロセス全体の旗 ``SFT_TASK_PREFIX`` だった。立てると接頭辞なしで
    学習した常駐モデル (n17c / n17m30 等) にも印が付き、見たことのない形が届く。
    旧旗は読まない (一度も本番で立てていない)。
    """
    extra = {m.strip() for m in os.environ.get(_EXTRA_MODELS_ENV, "").split(",") if m.strip()}
    return model.strip() in PREFIX_TRAINED_MODELS | extra


def prefix_for(step: Step) -> str:
    """step の接頭辞 (学習していない step は空文字)。"""
    return TASK_MARKERS.get(step, "")


def with_task_prefix(prompt: str, step: Step, model: str) -> str:
    """プロンプトの先頭へ課題の印を付ける (二重付与はしない)。"""
    if not task_prefix_enabled(model):
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

    def _placed(self, prompt: str, system: str | None) -> tuple[str, str | None]:
        """印を **系列の先頭**へ置く (system があれば system 側)。"""
        model = self._inner.model
        if system:
            return prompt, with_task_prefix(system, self._step, model)
        return with_task_prefix(prompt, self._step, model), system

    async def generate(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        think: bool | None = None,
    ) -> LLMResponse:
        p, sys_ = self._placed(prompt, system)
        return await self._inner.generate(
            p, system=sys_, temperature=temperature, max_tokens=max_tokens, think=think
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
        p, sys_ = self._placed(prompt, system)
        return await self._inner.generate_structured(
            p,
            schema,
            system=sys_,
            temperature=temperature,
            max_tokens=max_tokens,
            think=think,
            max_attempts=max_attempts,
        )
