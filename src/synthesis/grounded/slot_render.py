"""状況総括の節を「LLM の文の部品 + コードの定型文」で組む (n20 T5、旗 ``SLOT_RENDER``)。

利用者判断 U-B (2026-11-02): 確度語・検証の反証・対立仮説はコードが描き、LLM には書かせない。
LLM が書くのは判定ごとの ``world_clause`` (世界側で何が起きたか) と ``implication_clause`` (含意)、
見出しの 1 文 ``headline_clause``、注視点の ``watch_clause`` だけ (``WireJudgmentClauses``)。
節の並び・確度語・反証・対立仮説・連鎖・SIR 対応はコードが決定論で組む。設計 D §2.2「状況総括」。

関門 (決定論): 判定 id の所属と網羅 (書かれなかった判定は claim で埋めて記録) / 確度語・反証・
対立仮説の語を LLM の文に書いていないか (書いていればその文を台帳の値で置き換える) /
delta_note に無い因果の接続語 (「を受けて」等) を書いていないか。
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema

from src.logging_config import get_logger
from src.synthesis.grounded.estimate import Estimate, KeyJudgment
from src.synthesis.grounded.render import (
    _DELTA_JA,
    _MAX_TOKENS,
    _MAX_TOKENS_BY_PERIOD,
    _TEMPERATURE,
    RenderPlan,
    _counter,
    _hyp_label,
    _pir_rollup,
    _WireSections,
    build_render_plan,
)
from src.tools.llm_client import LLMClient
from src.tools.llm_schema import require_all_properties

_log = get_logger(__name__)

TEMPLATE_V9 = "synthesis/render_v9.j2"
#: weekly の A 層 40 + 継続 12 に余裕を足した上限 (文法に閉じを強制させる)
ITEMS_MAX = 80

#: 確度はコードが描く — LLM の文に入っていたら、その文を台帳の値で置き換える。
#: 学習の関門 (scripts/sft_gate_n20_schemas.py の CERTAINTY_WORDS + 反証・対立仮説・確度) と揃える。
BANNED_IN_CLAUSE: tuple[str, ...] = (
    "確実に",
    "間違いなく",
    "疑いなく",
    "疑いの余地",
    "断言",
    "必ず",
    "明らかに",
    "反証",
    "対立仮説",
    "確度",
    "可能性が高い",
    "可能性がある",
    "否定できない",
    "とみられる",
    "確証",
)
#: 材料 (delta_note) に無ければ因果の創作とみなす接続語
CAUSAL_CONNECTORS: tuple[str, ...] = ("を受けて", "これに対し", "その結果", "これを受け")

#: 確度の定型文 (render.j2 の確度語の対応表と同じ: 高=可能性が高い / 中=可能性がある /
#: 低=確証はなく〜の可能性も否定できない)。{label} は見立て (先頭仮説) の表示名。
CERTAINTY_SENTENCE: Mapping[str, str] = {
    "high": "見立ては{label}で、その可能性が高い。",
    "moderate": "見立ては{label}で、その可能性がある。",
    "low": "見立ては{label}だが、確証はなく、その可能性も否定できない段階にとどまる。",
}
NO_MOVED = "本期間に変化した判定はない。"
NO_STANDING = "継続中の判定はない。"
NO_RELATIONS = "判定間の関係は検出されていない。"
NO_PIR = "SIR に紐づく判定はない。"
_INDICATORS_MAX = 5


class ClauseItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    judgment_id: str = ""
    world_clause: str = ""
    implication_clause: str = ""

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        return require_all_properties(dict(handler(core_schema)))


class WireJudgmentClauses(BaseModel):
    """v9 の LLM 出力 (n20 の学習の形と同じ: headline_clause / items / watch_clause)。"""

    model_config = ConfigDict(extra="ignore")
    headline_clause: str = ""
    items: list[ClauseItem] = Field(default_factory=list, json_schema_extra={"maxItems": ITEMS_MAX})
    watch_clause: str = ""

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        return require_all_properties(dict(handler(core_schema)))


@dataclass(frozen=True)
class SlotStats:
    """関門の件数 (log 用。本文は持たない)。"""

    uncovered: int = 0
    unknown_ids: int = 0
    banned_replaced: int = 0
    causal_replaced: int = 0


def _sentence(text: str) -> str:
    t = text.strip()
    if not t:
        return ""
    return t if t.endswith(("。", "！", "？", ".")) else t + "。"


_PROMPT_SECTION = re.compile(r"【変化した判定】(.*?)【判定間の関係】", re.S)
_PROMPT_JUDGMENT = re.compile(r"^\[([^\]\s]+)\]【[^】]*】", re.M)
_PROMPT_DELTA_NOTE = re.compile(r"^\[([^\]\s]+)\]【[^】]*】.*\n.*?/ 変化の内容: (.*)$", re.M)


def prompt_wanted_ids(prompt: str) -> list[str]:
    """v9 のプロンプトから items に書かせる判定 id (変化した判定 + 継続中の判定) を順に読む。

    ``assemble_sections`` の ``wanted`` (= ``plan.moved_ids + plan.standing_ids``) と同じ列。
    プロンプトしか持たない学習の形の検査 (scripts/sft_gate_n20_schemas.py) が使う。
    「そのほかの動き (一覧)」(``- [id]``) と見出し対象は含まない。
    """
    m = _PROMPT_SECTION.search(prompt)
    return _PROMPT_JUDGMENT.findall(m.group(1)) if m else []


def prompt_delta_notes(prompt: str) -> dict[str, str]:
    """v9 のプロンプトの判定 id → 変化の内容 (delta_note。因果の接続語の照合範囲)。"""
    m = _PROMPT_SECTION.search(prompt)
    return dict(_PROMPT_DELTA_NOTE.findall(m.group(1))) if m else {}


def causal_unsupported(world: str, delta_note: str) -> bool:
    """材料 (delta_note) に無い因果の接続語を world が持つか (``_checked_clauses`` と同じ判定)。"""
    return any(c in world and c not in delta_note for c in CAUSAL_CONNECTORS)


def has_banned(text: str) -> bool:
    return any(w in text for w in BANNED_IN_CLAUSE)


def certainty_sentence(j: KeyJudgment) -> str:
    """判定の確度の定型文 (確度語と見立ての名をコードが付ける)。"""
    tmpl = CERTAINTY_SENTENCE.get(j.confidence, CERTAINTY_SENTENCE["low"])
    return tmpl.format(label=f"「{_hyp_label(j.leading_hypothesis)}」")


def render_judgment(j: KeyJudgment, world: str, implication: str, *, with_delta: bool) -> str:
    """1 判定 = 世界の文 + 確度の定型文 + (反証) + (対立仮説) + 含意。"""
    delta = _DELTA_JA.get(j.delta_type, j.delta_type) if with_delta else ""
    parts = [(f"【{delta}】" if delta else "") + _sentence(world), certainty_sentence(j)]
    if j.adversarial_refuted:
        note = j.adversarial_note.strip()
        parts.append(
            f"ただし検証では反証が示されている ({note})。"
            if note
            else "ただし検証では反証が示されている。"
        )
    counter = _counter(j)
    if counter:
        parts.append(f"対立仮説として{counter}も残る。")
    parts.append(_sentence(implication))
    return "".join(p for p in parts if p)


def _checked_clauses(
    j: KeyJudgment, item: ClauseItem | None, counts: dict[str, int]
) -> tuple[str, str]:
    """LLM の文を関門に通す。だめなら台帳の値 (claim / implication) に置き換える。"""
    if item is None:
        counts["uncovered"] += 1
        return j.claim, j.implication
    world, implication = item.world_clause.strip(), item.implication_clause.strip()
    if not world or has_banned(world):
        counts["banned_replaced"] += bool(world)
        world = j.claim
    elif causal_unsupported(world, j.delta_note):
        counts["causal_replaced"] += 1
        world = j.claim
    if not implication or has_banned(implication):
        counts["banned_replaced"] += bool(implication)
        implication = j.implication
    return world, implication


def _listed_paragraph(listed: list[KeyJudgment]) -> str:
    if not listed:
        return ""
    by_delta: dict[str, int] = {}
    for j in listed:
        label = _DELTA_JA.get(j.delta_type, j.delta_type) or "その他"
        by_delta[label] = by_delta.get(label, 0) + 1
    counts = "・".join(f"{k} {v} 件" for k, v in by_delta.items())
    return f"そのほかの動き (根拠は本文で扱わない判定): {counts}。"


def _spillover(watch: str, shown: list[KeyJudgment]) -> str:
    fired = list(dict.fromkeys(i for j in shown for i in j.fired_indicators))[:_INDICATORS_MAX]
    open_ = list(dict.fromkeys(i for j in shown for i in j.indicators[:2]))[:_INDICATORS_MAX]
    parts = [_sentence(watch)]
    if fired:
        parts.append("発火した監視指標: " + "、".join(fired) + "。")
    if open_:
        parts.append("開いている監視指標: " + "、".join(open_) + "。")
    return "".join(p for p in parts if p) or "特記なし。"


def _pir_section(est: Estimate) -> str:
    lines = [
        f"- {p['pir_title']}: "
        + "、".join(f"{e['claim'] or e['id']} ({e['confidence_ja']})" for e in p["entries"])
        for p in _pir_rollup(est.judgments)
    ]
    return "\n".join(lines) or NO_PIR


def assemble_sections(
    clauses: WireJudgmentClauses, *, est: Estimate, plan: RenderPlan
) -> tuple[_WireSections, SlotStats]:
    """LLM の部品と台帳の値から 6 節を組む (純粋関数)。"""
    by_id = {j.id: j for j in est.judgments}
    wanted = [*plan.moved_ids, *plan.standing_ids]
    got: dict[str, ClauseItem] = {}
    unknown = 0
    for item in clauses.items:
        if item.judgment_id not in wanted:
            unknown += 1
        elif item.judgment_id not in got:
            got[item.judgment_id] = item
    counts = {"uncovered": 0, "banned_replaced": 0, "causal_replaced": 0}
    moved = [by_id[i] for i in plan.moved_ids if i in by_id]
    standing = [by_id[i] for i in plan.standing_ids if i in by_id]
    rendered = {
        j.id: render_judgment(
            j, *_checked_clauses(j, got.get(j.id), counts), with_delta=j.id in plan.moved_ids
        )
        for j in (*moved, *standing)
    }
    weight = "\n".join(rendered[j.id] for j in moved) or NO_MOVED
    listed = _listed_paragraph([by_id[i] for i in plan.listed_ids if i in by_id])
    headline = _headline(clauses.headline_clause, plan.head, counts)
    watch = "" if has_banned(clauses.watch_clause) else clauses.watch_clause
    sections = _WireSections(
        headline=headline,
        weight_section=f"{weight}\n{listed}" if listed else weight,
        chain_section="\n".join(f"- {r}" for r in plan.relation_lines) or NO_RELATIONS,
        cog_section="\n".join(rendered[j.id] for j in standing) or NO_STANDING,
        spillover_section=_spillover(watch, [*moved, *standing]),
        pir_section=_pir_section(est),
    )
    stats = SlotStats(
        uncovered=counts["uncovered"],
        unknown_ids=unknown,
        banned_replaced=counts["banned_replaced"],
        causal_replaced=counts["causal_replaced"],
    )
    return sections, stats


def _headline(clause: str, head: KeyJudgment | None, counts: dict[str, int]) -> str:
    """見出し = LLM の 1 文 (世界側) + 指名判定の確度の定型文。空・禁止語なら判定の claim。"""
    if head is None:
        return _sentence(clause) if clause.strip() and not has_banned(clause) else ""
    text = clause.strip()
    if not text or has_banned(text):
        counts["banned_replaced"] += bool(text)
        text = head.claim
    return _sentence(text) + certainty_sentence(head) + _sentence(head.implication)


async def generate_clauses(
    *, llm: LLMClient, est: Estimate, period_label: str
) -> tuple[WireJudgmentClauses, RenderPlan]:
    plan = build_render_plan(
        est=est, period_label=period_label, cot_notes=False, template=TEMPLATE_V9
    )
    raw: WireJudgmentClauses = await llm.generate_structured(
        plan.prompt,
        WireJudgmentClauses,
        temperature=_TEMPERATURE,
        max_tokens=_MAX_TOKENS_BY_PERIOD.get(est.period_type, _MAX_TOKENS),
        think=False,
    )
    return raw, plan


async def render_slot_sections(
    *, llm: LLMClient, est: Estimate, period_label: str
) -> _WireSections:
    """on: v9 で部品を書かせ、節はコードが組む。"""
    clauses, plan = await generate_clauses(llm=llm, est=est, period_label=period_label)
    sections, stats = assemble_sections(clauses, est=est, plan=plan)
    _log.info("synthesis_slot_render", period=period_label, **stats.__dict__)
    return sections


async def choose_sections(
    *,
    llm: LLMClient,
    est: Estimate,
    period_label: str,
    legacy: Callable[[], Awaitable[_WireSections]],
) -> _WireSections:
    """旗 ``SLOT_RENDER`` で節の作り方を選ぶ。off は ``legacy`` (render.j2) だけを呼ぶ。"""
    from src.tools.n20_flags import slot_render_mode

    mode = slot_render_mode()
    if mode == "on":
        return await render_slot_sections(llm=llm, est=est, period_label=period_label)
    sections = await legacy()
    if mode == "shadow":
        await shadow_measure(llm=llm, est=est, period_label=period_label)
    return sections


async def shadow_measure(*, llm: LLMClient, est: Estimate, period_label: str) -> None:
    """shadow: 旧の出力で配信した後に v9 を別に生成し、関門の件数だけ記録する。"""
    try:
        clauses, plan = await generate_clauses(llm=llm, est=est, period_label=period_label)
        _, stats = assemble_sections(clauses, est=est, plan=plan)
    except Exception as e:  # noqa: BLE001 — shadow は配信に影響させない
        _log.warning("synthesis_slot_render_shadow_failed", error=str(e)[:160])
        return
    _log.info(
        "synthesis_slot_render_shadow",
        period=period_label,
        items=len(clauses.items),
        **stats.__dict__,
    )
