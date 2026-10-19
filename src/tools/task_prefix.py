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
from collections.abc import Callable
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
PREFIX_TRAINED_MODELS: frozenset[str] = frozenset(
    {"kuebiko-sft:s19", "kuebiko-sft:s21", "kuebiko-sft:n19", "kuebiko-sft:i58", "kuebiko-sft:s23i"}
)

#: 名前の**前方一致**で接頭辞つきと見なすモデル系統。s24 (と s24b) とその補間の一時タグ
#: (``kuebiko-sft:s24i25`` / ``kuebiko-sft:s24bi50`` 等、``import_interp_s24.sh`` が付ける名前)
#: は、本番へ上げる補間の重みが決まる前でも印つきで学習した系統として扱う。s23i 以前は完全一致のまま。  # noqa: E501
PREFIX_TRAINED_FAMILIES: tuple[str, ...] = ("kuebiko-sft:s24",)

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
    # 借用を解消した step (2026-10-16)。**印は借り元と同じまま** — 外すかは s24 の評価で決める。
    # 学習した課題は summary / detect / ach の 3 つで、これらの呼び出しは未学習の別課題
    # (IOC 検証・claim の書き直し・開設の採点/反証/書き直し)。
    Step.IOC_LLM_VERIFY: "[task: summary]\n",
    # 統合判断分類器 (2026-10-18)。**既定の印は借り元 (要約) と同じ** — s23i 以前は今まで通り。
    # s24b 系だけ MODEL_STEP_MARKERS で [task: judgment] に差し替える
    Step.JUDGMENT_CLASSIFY: "[task: summary]\n",
    Step.DETECT_CLAIM_WRITE: "[task: detect]\n",
    Step.LEDGER_ACH_AUX: "[task: ach]\n",
    Step.DIGEST_DEEP_DIVE_SELECT: "[task: deep_dive_select]\n",
    Step.DIGEST_DEEP_DIVE: "[task: deep_dive]\n",
    # s22 から学習 (2026-09-29)。s21 / n19 は学習していない (UNTRAINED_STEPS)
    Step.PIR_DAILY_FOCUS: "[task: pir_focus]\n",
    Step.SEVERITY_AXES: "[task: axes]\n",
}

#: モデル名の接頭辞 → {step: 印}。**そのモデルだけ** TASK_MARKERS の既定と別の印を付ける。
#: s24b は判断の分類器を専用の課題 ``judgment`` として学習する (それまでは要約の印を借りていた)。
#: 前方一致なので s24b の一時タグ (``kuebiko-sft:s24b…``) にも効く。s23i 以前は既定のまま
#: (印は ``[task: summary]`` で本番と不変)。
MODEL_STEP_MARKERS: dict[str, dict[Step, str]] = {
    "kuebiko-sft:s24b": {
        Step.JUDGMENT_CLASSIFY: "[task: judgment]\n",
        # 補助課題 3 本 (2026-10-18、W4): s24b だけ専用の印で学習する。s23i 以前・s24 は今のまま
        Step.IOC_LLM_VERIFY: "[task: ioc_verify]\n",
        Step.LEDGER_ACH_AUX: "[task: ach_aux]\n",
        Step.DEDUP_CLUSTER_JUDGE: "[task: dedup_judge]\n",
    },
}

#: 接頭辞つきで学習したモデルのうち、**その step は学習していない**もの。印を付けると、
#: 学習していない step に見たことのない印が届く
#: (n19 は深掘りの教師データが無いまま投入 — 2026-09-29)。
_S24_FAMILY = "kuebiko-sft:s24"
_S24B_FAMILY = "kuebiko-sft:s24b"
UNTRAINED_STEPS: dict[str, frozenset[Step]] = {
    "kuebiko-sft:n19": frozenset(
        {
            Step.DIGEST_DEEP_DIVE,
            Step.DIGEST_DEEP_DIVE_SELECT,
            Step.PIR_DAILY_FOCUS,
            Step.SEVERITY_AXES,
        }
    ),
    "kuebiko-sft:s21": frozenset({Step.PIR_DAILY_FOCUS, Step.SEVERITY_AXES}),
    # s24 系 (前方一致、``_S24_FAMILY``): 学習した課題は triage / summary / pir / kind / pair / ach /  # noqa: E501
    # axes / pir_focus (s24b は judgment も)。下は**学習していない**のに印が付く step
    # (detect 系・ach/summary の借用・narrative 系・深掘り)。深掘りは n20 の担当
    _S24_FAMILY: frozenset(
        {
            Step.SYNTHESIS_DETECT,
            Step.DETECT_CLAIM_WRITE,
            Step.LEDGER_ACH_AUX,
            Step.IOC_LLM_VERIFY,
            Step.EVENT_NEWS,
            Step.PIR_SPOTLIGHT,
            Step.SYNTHESIS_NARRATIVE,
            Step.DIGEST_DEEP_DIVE,
            Step.DIGEST_DEEP_DIVE_SELECT,
        }
    ),
    "kuebiko-sft:s19": frozenset({Step.PIR_DAILY_FOCUS, Step.SEVERITY_AXES}),
}
#: s24b 系は IOC 検証・ACH 補助・重複判定を学習する (専用の印)。s24 は未学習のまま
UNTRAINED_STEPS[_S24B_FAMILY] = UNTRAINED_STEPS[_S24_FAMILY] - {
    Step.LEDGER_ACH_AUX,
    Step.IOC_LLM_VERIFY,
    Step.DEDUP_CLUSTER_JUDGE,
}


def task_prefix_enabled(model: str) -> bool:
    """このモデルに接頭辞を付けるか (= 接頭辞つきで学習したモデルか)。

    ⚠⚠ 2026-09-24 まではプロセス全体の旗 ``SFT_TASK_PREFIX`` だった。立てると接頭辞なしで
    学習した常駐モデル (n17c / n17m30 等) にも印が付き、見たことのない形が届く。
    旧旗は読まない (一度も本番で立てていない)。
    """
    extra = {m.strip() for m in os.environ.get(_EXTRA_MODELS_ENV, "").split(",") if m.strip()}
    name = str(model.strip())
    return name in PREFIX_TRAINED_MODELS | extra or name.startswith(PREFIX_TRAINED_FAMILIES)


def untrained_steps_for(model: str) -> frozenset[Step]:
    """このモデルで学習していない step (完全一致 → s24 系は前方一致)。"""
    name = str(model.strip())
    if name in UNTRAINED_STEPS:
        return UNTRAINED_STEPS[name]
    if name.startswith(_S24B_FAMILY):
        return UNTRAINED_STEPS[_S24B_FAMILY]
    if name.startswith(_S24_FAMILY):
        return UNTRAINED_STEPS[_S24_FAMILY]
    return frozenset()


def prefix_for(step: Step, model: str | None = None) -> str:
    """step の接頭辞 (学習していない step は空文字)。

    ``model`` を渡すと ``MODEL_STEP_MARKERS`` の差し替えを反映する (省略時は既定の印)。
    """
    if model:
        name = str(model.strip())
        for model_prefix, markers in MODEL_STEP_MARKERS.items():
            if name.startswith(model_prefix) and step in markers:
                return markers[step]
    return TASK_MARKERS.get(step, "")


def with_task_prefix(prompt: str, step: Step, model: str) -> str:
    """プロンプトの先頭へ課題の印を付ける (二重付与はしない)。"""
    if not task_prefix_enabled(model) or step in untrained_steps_for(model):
        return prompt
    marker = prefix_for(step, model)
    if not marker or prompt.startswith(marker):
        return prompt
    return marker + prompt


class TaskPrefixClient(LLMClient):
    """プロンプトの先頭へ課題の印を付けて内側 client へ渡す wrapper。

    ⭐ **本番の全経路に一度で効かせるため factory (``model_tiers.build_llm_for``) で包む**。
    call site を 1 つずつ直すと必ず漏れる (漏れた経路だけ学習時と形が違う)。
    """

    def __init__(
        self,
        inner: LLMClient,
        step: Step,
        *,
        rebuild: Callable[[Step], LLMClient] | None = None,
    ) -> None:
        self._inner = inner
        self._step = step
        self._rebuild = rebuild
        self._by_step: dict[Step, LLMClient] = {}

    def for_step(self, step: Step) -> LLMClient:
        """同じ呼び出し元が別の step として呼ぶ client (借用の解消用、結果は run 中キャッシュ)。

        ``build_llm_for`` 由来なら step を解決し直す (その step の上書きが効く)。明示 ref の
        client (評価・教師収穫) は内側のモデルを保ったまま印だけ付け替える。
        """
        if step == self._step:
            return self
        cached = self._by_step.get(step)
        if cached is None:
            cached = (
                self._rebuild(step)
                if self._rebuild is not None
                else TaskPrefixClient(self._inner, step)
            )
            self._by_step[step] = cached
        return cached

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


def client_for_step(llm: LLMClient, step: Step) -> LLMClient:
    """``llm`` を別 step の client にする。印つき client 以外 (テストの代役等) はそのまま返す。"""
    return llm.for_step(step) if isinstance(llm, TaskPrefixClient) else llm
