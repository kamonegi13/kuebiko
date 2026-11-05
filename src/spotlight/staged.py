"""多段 Spotlight (n20 T5、旗 ``SPOTLIGHT_STAGED``、利用者判断 U-A)。設計 D §2.2「Spotlight」。

段 1 events (見出し + 主要事象 {index, rationale, anchor}) → 段 2 notes (主要事象ごとの
{index, kind=caveat|unknown, text, anchor}) → 段 3 outlook (観点 a|b の要点 ≤3)。
(c) 推奨・情報源の留保はコードが描き (``staged_render``)、(d) は外す。3 段を合わせた形
(``StagedSpotlight``) は n20 の学習の関門 (scripts/sft_gate_n20_schemas.py) の形と同じ。

コードの検査: index の範囲と重複 / anchor の逐語実在 (主要事象は記録のみ、但し書きは落とす) /
観点の値・件数 / 確度を強める語 (要点を落とす)。

本番の 1 段と同じ関門を多段にも当てる (H5、11-05):
- 尾部の最小件数 (``tail_gate``): 合成後の caveats / unknowns が足りなければ段 2 (notes) を
  同じプロンプトで再サンプルし、最も充足した候補を採る (上限 ``tail_gate.MAX_RESAMPLES``)
- 識別子 (``identifier_check``、厳密文法の kind): 段ごとに生成文の識別子をその段のプロンプトと
  照合し、無いものがあればその段を 1 回だけ書き直させる。残れば記録のみ (保存は止めない)。
  段 1 の書き直しは見出しだけを採る (主要事象の選定は段 2・3 の前提なので変えない)

⚠ 未実装 (T5 の未決事項): 前期の監視指標の「観測 / 未観測」の定型文 (設計 D の 4 段目)。前期の
指標が構造で保存されておらず (outlook は自由文)、何をもって「当たる記事」とするかが決まっていない。
"""

from __future__ import annotations

import asyncio
import dataclasses
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jinja2
from pydantic import BaseModel, ConfigDict, Field

from src.assessment.evidence_verify import normalize_for_match
from src.logging_config import get_logger
from src.spotlight import staged_render
from src.tools.llm_client import LLMClient

_log = get_logger(__name__)

TEMPLATE = "spotlight/pir_spotlight_staged_v7.j2"
KEY_EVENTS_MIN = 5
KEY_EVENTS_MAX = 8
EVENT_NOTES_MAX = 16
OUTLOOK_POINTS_MAX = 3
ASPECTS = frozenset({"a", "b"})
NOTE_KINDS = frozenset({"caveat", "unknown"})
ANCHOR_MIN_CHARS = 20
ANCHOR_MAX_CHARS = 120
#: 確度を強める語 (学習の関門の CERTAINTY_WORDS と同じ)。要点に入っていれば落とす
CERTAINTY_WORDS: tuple[str, ...] = (
    "確実に",
    "間違いなく",
    "疑いなく",
    "疑いの余地",
    "断言",
    "必ず",
    "明らかに",
)
_TEMPERATURE = 0.3
_MAX_TOKENS = 3072


class StagedKeyEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")
    index: int = 0
    rationale: str = ""
    anchor: str = ""


class StagedNote(BaseModel):
    model_config = ConfigDict(extra="ignore")
    index: int = 0
    kind: str = Field(default="caveat", description="caveat か unknown")
    text: str = ""
    anchor: str = ""


class OutlookPoint(BaseModel):
    model_config = ConfigDict(extra="ignore")
    aspect: str = Field(default="a", description="a (今期のパターン) か b (来週の指標)")
    text: str = ""


class _StageEvents(BaseModel):
    model_config = ConfigDict(extra="ignore")
    headline: str = ""
    key_events: list[StagedKeyEvent] = Field(
        default_factory=list, json_schema_extra={"maxItems": KEY_EVENTS_MAX}
    )


class _StageNotes(BaseModel):
    model_config = ConfigDict(extra="ignore")
    event_notes: list[StagedNote] = Field(
        default_factory=list, json_schema_extra={"maxItems": EVENT_NOTES_MAX}
    )


class _StageOutlook(BaseModel):
    model_config = ConfigDict(extra="ignore")
    outlook_points: list[OutlookPoint] = Field(
        default_factory=list, json_schema_extra={"maxItems": OUTLOOK_POINTS_MAX}
    )


class StagedSpotlight(BaseModel):
    """3 段を合わせた形 (= n20 の学習の形)。"""

    model_config = ConfigDict(extra="ignore")
    headline: str = ""
    key_events: list[StagedKeyEvent] = Field(default_factory=list)
    event_notes: list[StagedNote] = Field(default_factory=list)
    outlook_points: list[OutlookPoint] = Field(default_factory=list)


@dataclass(frozen=True)
class StagedResult:
    """検査と描画の後の、本番の SpotlightRecord に載せる値。"""

    headline: str
    key_event_indexes: tuple[int, ...]
    outlook: str
    caveats: tuple[str, ...]
    unknowns: tuple[str, ...]
    merged: StagedSpotlight
    counts: dict[str, int]


def _anchor_ok(anchor: str, candidate: dict[str, Any]) -> bool:
    a = anchor.strip()
    if not ANCHOR_MIN_CHARS <= len(a) <= ANCHOR_MAX_CHARS:
        return False
    needle = normalize_for_match(a)
    hay = normalize_for_match(f"{candidate.get('title', '')}\n{candidate.get('summary', '')}")
    return bool(needle) and needle in hay


def check_and_render(merged: StagedSpotlight, candidates: Sequence[dict[str, Any]]) -> StagedResult:
    """3 段の出力を検査し、本番の値 (見通し・但し書き・未解明点) を描く (純粋関数)。"""
    n = len(candidates)
    counts = dict.fromkeys(
        (
            "event_index_bad",
            "event_anchor_bad",
            "note_dropped",
            "point_dropped",
            "recommendations",
        ),
        0,
    )
    indexes: list[int] = []
    for ev in merged.key_events[:KEY_EVENTS_MAX]:
        if not 1 <= ev.index <= n or ev.index in indexes:
            counts["event_index_bad"] += 1
            continue
        indexes.append(ev.index)
        if not _anchor_ok(ev.anchor, candidates[ev.index - 1]):
            counts["event_anchor_bad"] += 1  # 主要事象は index で選ばれる — 記録のみ
    caveats: list[str] = []
    unknowns: list[str] = []
    for note in merged.event_notes:
        ok = (
            note.index in indexes
            and note.kind in NOTE_KINDS
            and note.text.strip()
            and _anchor_ok(note.anchor, candidates[note.index - 1])
        )
        if not ok:
            counts["note_dropped"] += 1
            continue
        (caveats if note.kind == "caveat" else unknowns).append(note.text.strip())
    points: list[tuple[str, str]] = []
    for p in merged.outlook_points[:OUTLOOK_POINTS_MAX]:
        if (
            p.aspect not in ASPECTS
            or not p.text.strip()
            or any(w in p.text for w in CERTAINTY_WORDS)
        ):
            counts["point_dropped"] += 1
            continue
        points.append((p.aspect, p.text))
    chosen = [candidates[i - 1] for i in indexes]
    recs = staged_render.recommendations(chosen)
    counts["recommendations"] = len(recs)
    tier_caveats = staged_render.source_caveats([(i, candidates[i - 1]) for i in indexes])
    return StagedResult(
        headline=merged.headline.strip(),
        key_event_indexes=tuple(indexes),
        outlook=staged_render.render_outlook(points, recs),
        caveats=tuple(tier_caveats + caveats),
        unknowns=tuple(unknowns),
        merged=merged,
        counts=counts,
    )


_PROMPT_CANDIDATE = re.compile(r"^\[(\d+)\] \*\*(.*)\*\* \(.*$", re.M)
_PROMPT_SUMMARY = re.compile(r"^  summary: (.*)$", re.M)
_PROMPT_CHOSEN = re.compile(r"^- \[(\d+)\] ", re.M)
_PROMPT_CHOSEN_HEAD = "## 選ばれた主要事象"


def prompt_candidates(prompt: str) -> list[dict[str, str]]:
    """描画済みの段のプロンプトから候補 (title と見せた summary) を [N] の順に読み戻す。

    プロンプトしか持たない学習の形の検査が、本番の ``_anchor_ok`` に同じ照合範囲を渡すため。
    """
    body = prompt.split(_PROMPT_CHOSEN_HEAD, 1)[0]
    heads = list(_PROMPT_CANDIDATE.finditer(body))
    out: list[dict[str, str]] = []
    for k, h in enumerate(heads):
        end = heads[k + 1].start() if k + 1 < len(heads) else len(body)
        summary = _PROMPT_SUMMARY.search(body, h.end(), end)
        out.append({"title": h.group(2), "summary": summary.group(1) if summary else ""})
    return out


def prompt_chosen_indexes(prompt: str) -> list[int]:
    """notes・outlook の段のプロンプトに載せた主要事象 (段 1 の結果) の [N]。"""
    if _PROMPT_CHOSEN_HEAD not in prompt:
        return []
    tail = prompt.split(_PROMPT_CHOSEN_HEAD, 1)[1].split("\n## ", 1)[0]
    return [int(x) for x in _PROMPT_CHOSEN.findall(tail)]


def key_events_range(n_candidates: int) -> tuple[int, int]:
    """主要事象の件数の範囲: 5〜8、候補が 5 件未満なら候補の件数まで (1 件以上)。"""
    low = max(1, min(KEY_EVENTS_MIN, n_candidates))
    return low, KEY_EVENTS_MAX


def anchor_ok(anchor: str, candidate: dict[str, Any]) -> bool:
    """anchor (strip 後 20〜120 字) が候補の title か summary に逐語で在るか (正規化後)。"""
    return _anchor_ok(anchor, candidate)


def _template() -> jinja2.Template:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(Path("prompts"))),
        autoescape=False,
        keep_trailing_newline=True,
    )
    return env.get_template(TEMPLATE)


def build_stage_prompt(stage: str, *, chosen: Sequence[dict[str, Any]] = (), **ctx: Any) -> str:
    """段のプロンプト (ctx = pir / period_label / candidate_articles / ledger_situations)。"""
    return _template().render(stage=stage, chosen=list(chosen), **ctx)


async def _call(llm: LLMClient, prompt: str, schema: type[BaseModel]) -> Any:
    return await llm.generate_structured(
        prompt=prompt, schema=schema, temperature=_TEMPERATURE, max_tokens=_MAX_TOKENS, think=False
    )


@dataclass
class _Tail:
    caveats: list[str]
    unknowns: list[str]


def _merge(events: _StageEvents, notes: _StageNotes, outlook: _StageOutlook) -> StagedSpotlight:
    return StagedSpotlight(
        headline=events.headline,
        key_events=events.key_events,
        event_notes=notes.event_notes,
        outlook_points=outlook.outlook_points,
    )


def _tail_of(
    events: _StageEvents, notes: _StageNotes, candidates: Sequence[dict[str, Any]]
) -> _Tail:
    r = check_and_render(_merge(events, notes, _StageOutlook()), candidates)
    return _Tail(caveats=list(r.caveats), unknowns=list(r.unknowns))


def _tail_worse(new: _Tail, old: _Tail) -> bool:
    from src.spotlight import tail_gate

    if not tail_gate.gate_enabled():
        return False
    return len(tail_gate.tail_deficit(new)) > len(tail_gate.tail_deficit(old))


async def _notes_with_tail_gate(
    llm: LLMClient,
    prompt: str,
    events: _StageEvents,
    notes: _StageNotes,
    candidates: Sequence[dict[str, Any]],
    counts: dict[str, int],
) -> _StageNotes:
    """尾部の最小件数の関門 (本番の 1 段と同じ下限・回数)。段 2 だけを再サンプルする。"""
    from src.spotlight import tail_gate

    if not tail_gate.gate_enabled():
        return notes

    def tail(n: _StageNotes) -> _Tail:
        r = check_and_render(_merge(events, n, _StageOutlook()), candidates)
        return _Tail(caveats=list(r.caveats), unknowns=list(r.unknowns))

    best, best_tail = notes, tail(notes)
    for _ in range(tail_gate.MAX_RESAMPLES):
        if not tail_gate.tail_deficit(best_tail):
            break
        counts["tail_resamples"] += 1
        try:
            cand: _StageNotes = await _call(llm, prompt, _StageNotes)
        except Exception as e:  # noqa: BLE001 - 手元の最良 (有効な 1 回目) を捨てない
            _log.warning("spotlight_staged_tail_resample_failed", error_type=type(e).__name__)
            break
        cand_tail = tail(cand)
        if tail_gate.tail_score(cand_tail) > tail_gate.tail_score(best_tail):
            best, best_tail = cand, cand_tail
    counts["tail_unmet"] = int(bool(tail_gate.tail_deficit(best_tail)))
    return best


def _stage_text(stage: str, out: BaseModel) -> str:
    if isinstance(out, _StageEvents):
        return "\n".join([out.headline, *(e.rationale for e in out.key_events)])
    if isinstance(out, _StageNotes):
        return "\n".join(n.text for n in out.event_notes)
    if isinstance(out, _StageOutlook):
        return "\n".join(p.text for p in out.outlook_points)
    raise TypeError(stage)


async def _identifier_gate(
    llm: LLMClient,
    prompts: dict[str, str],
    outs: dict[str, BaseModel],
    counts: dict[str, int],
    candidates: Sequence[dict[str, Any]],
) -> dict[str, BaseModel]:
    """段ごとに、生成文の厳密文法の識別子がその段のプロンプトに在るかを見る (本番と同じ判定)。"""
    from src.spotlight.identifier_check import (
        find_unsupported_identifiers,
        gate_enabled,
        render_identifier_feedback,
    )

    if not gate_enabled():
        return outs
    fixed = dict(outs)
    for stage, out in outs.items():
        unsupported = find_unsupported_identifiers(_stage_text(stage, out), prompts[stage])
        if not unsupported:
            continue
        counts["identifier_retry"] += 1
        again = await _call(
            llm, prompts[stage] + render_identifier_feedback(unsupported), type(out)
        )
        if isinstance(again, _StageEvents) and isinstance(out, _StageEvents):
            again = out.model_copy(update={"headline": again.headline})  # 選定は変えない
        if (
            isinstance(again, _StageNotes)
            and isinstance(out, _StageNotes)
            and _tail_worse(
                _tail_of(_as(outs["events"], _StageEvents), again, candidates),
                _tail_of(_as(outs["events"], _StageEvents), out, candidates),
            )
        ):
            counts["identifier_unverified"] += 1  # 尾部が悪化する書き直しは採らず、違反が残る扱い
            continue
        fixed[stage] = again
        if find_unsupported_identifiers(_stage_text(stage, again), prompts[stage]):
            counts["identifier_unverified"] += 1
    return fixed


async def generate_staged(llm: LLMClient, **ctx: Any) -> StagedResult:
    """3 段を順に呼び、尾部と識別子の関門を当てる (上限は呼び手の ``run_with_timeout``)。"""
    candidates: list[dict[str, Any]] = list(ctx["candidate_articles"])
    gate_counts = dict.fromkeys(
        ("tail_resamples", "tail_unmet", "identifier_retry", "identifier_unverified"), 0
    )
    prompts = {"events": build_stage_prompt("events", **ctx)}
    events: _StageEvents = await _call(llm, prompts["events"], _StageEvents)
    chosen = [
        {
            "index": e.index,
            "title": candidates[e.index - 1].get("title", ""),
            "rationale": e.rationale,
        }
        for e in events.key_events
        if 1 <= e.index <= len(candidates)
    ]
    prompts["notes"] = build_stage_prompt("notes", chosen=chosen, **ctx)
    notes: _StageNotes = await _call(llm, prompts["notes"], _StageNotes)
    notes = await _notes_with_tail_gate(
        llm, prompts["notes"], events, notes, candidates, gate_counts
    )
    prompts["outlook"] = build_stage_prompt("outlook", chosen=chosen, **ctx)
    outlook: _StageOutlook = await _call(llm, prompts["outlook"], _StageOutlook)
    outs = await _identifier_gate(
        llm,
        prompts,
        {"events": events, "notes": notes, "outlook": outlook},
        gate_counts,
        candidates,
    )
    merged = _merge(
        _as(outs["events"], _StageEvents),
        _as(outs["notes"], _StageNotes),
        _as(outs["outlook"], _StageOutlook),
    )
    result = check_and_render(merged, candidates)
    return dataclasses.replace(result, counts={**result.counts, **gate_counts})


def _as[M: BaseModel](out: BaseModel, cls: type[M]) -> M:
    if not isinstance(out, cls):
        raise TypeError(f"{type(out).__name__} is not {cls.__name__}")
    return out


async def run_with_timeout(
    llm: LLMClient, *, timeout_seconds: float, pir_id: str, shadow: bool, **ctx: Any
) -> StagedResult | None:
    """多段を全体の上限つきで走らせ、所要と件数を log する。上限超過・失敗は None。"""
    event = "spotlight_staged_shadow" if shadow else "spotlight_staged"
    started = time.monotonic()
    try:
        result = await asyncio.wait_for(generate_staged(llm, **ctx), timeout=timeout_seconds)
    except TimeoutError:
        _log.warning(
            f"{event}_timeout", pir_id=pir_id, timeout_s=timeout_seconds, elapsed_s=_since(started)
        )
        return None
    except Exception as e:  # noqa: BLE001 — 呼び手が旧の 1 段へ倒す / shadow は配信に影響させない
        _log.warning(f"{event}_failed", pir_id=pir_id, error=str(e)[:160])
        return None
    _log.info(
        event,
        pir_id=pir_id,
        elapsed_s=_since(started),
        timeout_s=timeout_seconds,
        key_events=len(result.key_event_indexes),
        caveats=len(result.caveats),
        unknowns=len(result.unknowns),
        **result.counts,
    )
    return result


def _since(started: float) -> float:
    return round(time.monotonic() - started, 1)
