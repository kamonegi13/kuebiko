"""n20 の課題 (event_news / spotlight / synthesis / deep_dive_select / deep_dive) の出力検査。

契約は data/eval/n20_contract_2026_11_02.md §1 (4)。入力 (user) を見ないと決まらない検査
(anchor の逐語実在・index の範囲・判定 id の所属) があるため、各検査は ``(j, user) -> errors``。
翻訳 (translate) と MITRE 和訳 (actor_sync) は自由文の課題で、JSON でないことだけを
``sft_quality_gate.FREE_TEXT_TASKS`` が見る。

⭐ H2 (11-05): **判定は本番の関数を import して同じにする** (検査だけ別実装にすると、本番が落とす
行を学習が正例として教える / 本番が通す行を学習が捨てる)。共有するもの:
- event_news: anchor の照合 (``fact_anchor.line_issues`` = ``normalize_for_match``・その行の
  ``source_index`` の記事だけ・strip 後の長さ・留保語・数値)。記事の本文はプロンプトから
  ``generator.parse_prompt_member_texts`` で読み戻す
- synthesis: ``slot_render.BANNED_IN_CLAUSE`` / ``causal_unsupported`` / wanted (変化した判定 +
  継続中の判定、``prompt_wanted_ids``) の全件の網羅
- spotlight: ``staged`` の anchor 照合・index の範囲・件数 (候補 < 5 件は候補の件数まで)・
  event_notes は選ばれた主要事象の index だけ・確度を強める語。多段は**段ごとの例**
  (``spotlight_events`` / ``spotlight_notes`` / ``spotlight_outlook``) を検査できる。統合形
  (``spotlight``) の検査も残す
- deep_dive: ``recap_points.to_recap`` で本番の形へ写し ``uncited_articles`` / ``thin_sections``

Spotlight は利用者判断 U-A: 観点は a|b だけを LLM が書き、(c) は決定論の描画、(d) は外す。
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.digest.recap_points import (  # noqa: E402
    HEADING_MAX_CHARS,
    RecapPointsOutput,
    prompt_article_ids,
    to_recap,
)
from src.digest.recap_render import thin_sections, uncited_articles  # noqa: E402
from src.eventnews import fact_anchor  # noqa: E402
from src.eventnews.generator import parse_prompt_member_texts  # noqa: E402
from src.spotlight import staged  # noqa: E402
from src.synthesis.grounded.slot_render import (  # noqa: E402
    BANNED_IN_CLAUSE,
    causal_unsupported,
    prompt_delta_notes,
    prompt_wanted_ids,
)

#: anchor (原文の逐語) の長さ (字) — 本番の値をそのまま公開する
ANCHOR_MIN_CHARS = fact_anchor.ANCHOR_MIN_CHARS
ANCHOR_MAX_CHARS = fact_anchor.ANCHOR_MAX_CHARS
OUTLOOK_POINTS_MAX = staged.OUTLOOK_POINTS_MAX
OUTLOOK_ASPECTS = staged.ASPECTS
NOTE_KINDS = staged.NOTE_KINDS
DEEP_DIVE_SELECT_MAX = 50  # RUBRIC_CHUNK_SIZE (25) * 2
DEEP_DIVE_CHUNK_SIZE = 25
DEEP_DIVE_SECTIONS_MAX = 7
DEEP_DIVE_HEADING_MAX = HEADING_MAX_CHARS
SCORE_KEYS = frozenset({"pir", "roi", "novelty"})
SCORE_RANGE = (0.0, 5.0)

#: 確度を LLM が断定する語 (Spotlight・深掘りは staged の語、状況総括は BANNED_IN_CLAUSE)
CERTAINTY_WORDS = staged.CERTAINTY_WORDS

EVENT_NEWS_KEYS = frozenset(
    {"headline", "bluf", "key_points", "facts", "discrepancies", "caveats", "unknowns"}
)
ANCHORED_SECTIONS = fact_anchor.ANCHORED_SECTIONS
SYNTHESIS_KEYS = frozenset({"headline_clause", "items", "watch_clause"})
SYNTHESIS_ITEM_KEYS = frozenset({"judgment_id", "world_clause", "implication_clause"})

_ID_TOKEN = re.compile(r"\{I(\d+)\}")
_CATALOG_LINE = re.compile(r"^I(\d+) = ", re.M)


def _texts(o: Any) -> list[str]:
    if isinstance(o, str):
        return [o]
    if isinstance(o, dict):
        return [t for v in o.values() for t in _texts(v)]
    if isinstance(o, list):
        return [t for v in o for t in _texts(v)]
    return []


def _banned_words(j: Any, words: tuple[str, ...]) -> list[str]:
    body = "\n".join(_texts(j))
    return [f"banned_word:{w}" for w in words if w in body]


def _keys(j: dict[str, Any], keys: frozenset[str]) -> list[str]:
    errs = [f"extra_key:{k}" for k in j if k not in keys]
    return errs + [f"missing_key:{k}" for k in sorted(keys) if k not in j]


# --- event_news ------------------------------------------------------------


def _event_row_errors(item: Any, section: str, i: int, texts: dict[int, str]) -> list[str]:
    label = f"{section}[{i}]"
    if not isinstance(item, dict) or "anchor" not in item:
        return [f"anchor_missing:{label}"]
    try:
        row = fact_anchor.AnchoredFactItem.model_validate(item)
    except ValidationError:
        return [f"row_fields:{label}"]
    errs = [f"{k}:{label}" for k in fact_anchor.line_issues(row, texts, section=section)]
    if section == "facts" and not 1 <= row.source_index <= len(texts):
        errs.append(f"source_index_out_of_range:{label}")
    return errs


def schema_errors_event_news(j: Any, user: str) -> list[str]:
    if not isinstance(j, dict):
        return ["not_object"]
    errs = _keys(j, EVENT_NEWS_KEYS)
    if not str(j.get("bluf") or "").strip():
        errs.append("empty_bluf")
    texts = parse_prompt_member_texts(user)
    if not texts:
        return errs + ["no_members_in_prompt"]
    for section in ANCHORED_SECTIONS:
        items = j.get(section) or []
        if not isinstance(items, list):
            errs.append(f"not_list:{section}")
            continue
        for i, item in enumerate(items):
            errs += _event_row_errors(item, section, i, texts)
    catalog = {int(x) for x in _CATALOG_LINE.findall(user)}
    if {int(x) for x in _ID_TOKEN.findall("\n".join(_texts(j)))} - catalog:
        errs.append("identifier_token_not_in_catalog")
    return errs


# --- spotlight (多段: 段ごと + 統合形) ----------------------------------------


def _anchor_errors(item: dict[str, Any], cands: list[dict[str, str]], label: str) -> list[str]:
    anchor = item.get("anchor")
    if not isinstance(anchor, str):
        return [f"anchor_missing:{label}"]
    if not ANCHOR_MIN_CHARS <= len(anchor.strip()) <= ANCHOR_MAX_CHARS:
        return [f"anchor_length:{label}"]
    index = item.get("index")
    if not isinstance(index, int) or not 1 <= index <= len(cands):
        return []  # index の誤りは別に数える
    if not staged.anchor_ok(anchor, cands[index - 1]):
        return [f"anchor_not_verbatim:{label}"]
    return []


def _key_events_errors(j: dict[str, Any], cands: list[dict[str, str]]) -> list[str]:
    key_events = j.get("key_events")
    if not isinstance(key_events, list):
        return ["key_events_not_list"]
    errs: list[str] = []
    low, high = staged.key_events_range(len(cands))
    if not low <= len(key_events) <= high:
        errs.append(f"key_events_count:{len(key_events)}")
    indexes = [k.get("index") if isinstance(k, dict) else None for k in key_events]
    if any(not isinstance(i, int) or not 1 <= i <= len(cands) for i in indexes):
        errs.append("key_events_index_out_of_range")
    if len(set(indexes)) != len(indexes):
        errs.append("key_events_index_duplicate")
    for i, item in enumerate(key_events):
        if isinstance(item, dict):
            errs += _anchor_errors(item, cands, f"key_events[{i}]")
    return errs


def _notes_errors(j: dict[str, Any], cands: list[dict[str, str]], chosen: list[int]) -> list[str]:
    notes = j.get("event_notes") or []
    if not isinstance(notes, list):
        return ["event_notes_not_list"]
    errs: list[str] = []
    for i, note in enumerate(notes):
        index = note.get("index") if isinstance(note, dict) else None
        if not isinstance(note, dict) or index not in chosen:
            errs.append(f"event_notes_index_not_chosen:{i}")
            continue
        if note.get("kind") not in NOTE_KINDS:
            errs.append(f"event_notes_kind:{i}")
        if not str(note.get("text") or "").strip():
            errs.append(f"event_notes_empty_text:{i}")
        errs += _anchor_errors(note, cands, f"event_notes[{i}]")
    return errs


def _outlook_errors(j: dict[str, Any]) -> list[str]:
    points = j.get("outlook_points") or []
    if not isinstance(points, list):
        return ["outlook_points_not_list"]
    errs: list[str] = []
    if len(points) > OUTLOOK_POINTS_MAX:
        errs.append(f"outlook_points_over_max:{len(points)}")
    for p in points:
        aspect = p.get("aspect") if isinstance(p, dict) else None
        if aspect not in OUTLOOK_ASPECTS:
            errs.append(f"outlook_aspect_out_of_range:{aspect}")
    return errs


def _forbidden_spotlight_keys(j: dict[str, Any]) -> list[str]:
    return [f"forbidden_key:{k}" for k in ("speculation", "outlook") if k in j]


def schema_errors_spotlight_events(j: Any, user: str) -> list[str]:
    """段 1 (events) の例: {headline, key_events}。"""
    if not isinstance(j, dict):
        return ["not_object"]
    errs = _keys(j, frozenset({"headline", "key_events"}))
    if not str(j.get("headline") or "").strip():
        errs.append("empty_headline")
    errs += _key_events_errors(j, staged.prompt_candidates(user))
    return errs + _banned_words(j, CERTAINTY_WORDS)


def schema_errors_spotlight_notes(j: Any, user: str) -> list[str]:
    """段 2 (notes) の例: {event_notes}。index はプロンプトに載せた主要事象だけ。"""
    if not isinstance(j, dict):
        return ["not_object"]
    errs = _keys(j, frozenset({"event_notes"}))
    cands = staged.prompt_candidates(user)
    errs += _notes_errors(j, cands, staged.prompt_chosen_indexes(user))
    return errs + _banned_words(j, CERTAINTY_WORDS)


def schema_errors_spotlight_outlook(j: Any, user: str) -> list[str]:
    """段 3 (outlook) の例: {outlook_points}。"""
    if not isinstance(j, dict):
        return ["not_object"]
    errs = _keys(j, frozenset({"outlook_points"})) + _outlook_errors(j)
    return errs + _banned_words(j, CERTAINTY_WORDS)


def schema_errors_spotlight(j: Any, user: str) -> list[str]:
    """3 段を合わせた形 (``StagedSpotlight``)。user は段 1 のプロンプト (候補の一覧)。"""
    if not isinstance(j, dict):
        return ["not_object"]
    errs = _forbidden_spotlight_keys(j)
    errs += [f"missing_key:{k}" for k in ("headline", "key_events", "outlook_points") if k not in j]
    cands = staged.prompt_candidates(user)
    errs += _key_events_errors(j, cands)
    chosen = [k["index"] for k in j.get("key_events") or [] if isinstance(k, dict) and "index" in k]
    errs += _notes_errors(j, cands, chosen)
    errs += _outlook_errors(j)
    return errs + _banned_words(j, CERTAINTY_WORDS)


# --- synthesis ---------------------------------------------------------------


def _synthesis_item_errors(items: list[Any], wanted: list[str], notes: dict[str, str]) -> list[str]:
    errs: list[str] = []
    seen: list[str] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict) or set(item) != SYNTHESIS_ITEM_KEYS:
            errs.append(f"item_keys:{i}")
            continue
        jid = item["judgment_id"]
        if jid in seen:
            errs.append(f"judgment_id_duplicate:{i}")
        if jid not in wanted:
            errs.append(f"judgment_id_not_in_input:{i}")
        seen.append(jid)
        world, implication = str(item["world_clause"] or ""), str(item["implication_clause"] or "")
        if not world.strip() or not implication.strip():
            errs.append(f"empty_clause:{i}")
        if causal_unsupported(world, notes.get(jid, "")):
            errs.append(f"causal_connector:{i}")
    errs += [f"uncovered:{w}" for w in wanted if w not in seen]
    if [s for s in seen if s in wanted] != [w for w in wanted if w in seen]:
        errs.append("items_order")
    return errs


def schema_errors_synthesis(j: Any, user: str) -> list[str]:
    if not isinstance(j, dict):
        return ["not_object"]
    errs = _keys(j, SYNTHESIS_KEYS)
    for key in ("headline_clause", "watch_clause"):
        if not str(j.get(key) or "").strip():
            errs.append(f"empty:{key}")
    items = j.get("items") or []
    if not isinstance(items, list):
        return errs + ["items_not_list"]
    errs += _synthesis_item_errors(items, prompt_wanted_ids(user), prompt_delta_notes(user))
    return errs + _banned_words(j, BANNED_IN_CLAUSE)


# --- deep_dive -----------------------------------------------------------------


def _score_errors(entry: Any, i: int) -> list[str]:
    if not isinstance(entry, dict):
        return [f"entry_not_object:{i}"]
    errs: list[str] = []
    scores = entry.get("scores")
    if not isinstance(scores, dict) or set(scores) != SCORE_KEYS:
        errs.append(f"score_keys:{i}")
    else:
        lo, hi = SCORE_RANGE
        if any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi
            for v in scores.values()
        ):
            errs.append(f"score_out_of_range:{i}")
    if not str(entry.get("rationale") or "").strip():
        errs.append(f"empty_rationale:{i}")
    return errs


def schema_errors_deep_dive_select(j: Any, user: str) -> list[str]:
    if not isinstance(j, dict):
        return ["not_object"]
    errs = [f"extra_key:{k}" for k in j if k != "scored_articles"]
    scored = j.get("scored_articles")
    if not isinstance(scored, list) or not scored:
        return errs + ["scored_articles_empty"]
    if len(scored) > DEEP_DIVE_SELECT_MAX:
        errs.append(f"scored_articles_over_max:{len(scored)}")
    numbers = [e.get("no") if isinstance(e, dict) else None for e in scored]
    if any(not isinstance(n, int) or not 1 <= n <= DEEP_DIVE_CHUNK_SIZE for n in numbers):
        errs.append("no_out_of_range")
    if len(set(numbers)) != len(numbers):
        errs.append("no_duplicate")
    for i, entry in enumerate(scored):
        errs += _score_errors(entry, i)
    return errs


def _deep_dive_section_errors(sec: Any, i: int, known: frozenset[str]) -> list[str]:
    if not isinstance(sec, dict) or set(sec) != {"heading", "points"}:
        return [f"section_keys:{i}"]
    errs: list[str] = []
    heading = str(sec["heading"] or "").strip()
    if not heading or len(heading) > DEEP_DIVE_HEADING_MAX:
        errs.append(f"heading_length:{i}")
    points = sec["points"]
    if not isinstance(points, list) or not points:
        return errs + [f"points_empty:{i}"]
    for k, point in enumerate(points):
        if not isinstance(point, dict) or set(point) != {"text", "article_ids"}:
            errs.append(f"point_keys:{i}.{k}")
            continue
        ids = point["article_ids"]
        if not str(point["text"] or "").strip():
            errs.append(f"empty_text:{i}.{k}")
        if not isinstance(ids, list) or not ids:
            errs.append(f"article_ids_empty:{i}.{k}")
        elif any(not isinstance(a, str) or a not in known for a in ids):
            errs.append(f"article_id_not_in_input:{i}.{k}")
    return errs


def schema_errors_deep_dive(j: Any, user: str) -> list[str]:
    """形 {sections: [{heading, points: [{text, article_ids}]}]} (``RecapPointsOutput``)。

    本番と同じく ``to_recap`` で旧の形へ写し、入力記事の全件の網羅 (``uncited_articles``) と
    本文の短すぎる節 (``thin_sections``) を見る。
    """
    if not isinstance(j, dict):
        return ["not_object"]
    errs = [f"extra_key:{k}" for k in j if k != "sections"]
    sections = j.get("sections")
    if not isinstance(sections, list) or not sections:
        return errs + ["sections_empty"]
    if len(sections) > DEEP_DIVE_SECTIONS_MAX:
        errs.append(f"sections_over_max:{len(sections)}")
    selected = prompt_article_ids(user)
    known = frozenset(selected)
    for i, sec in enumerate(sections):
        errs += _deep_dive_section_errors(sec, i, known)
    try:
        recap = to_recap(RecapPointsOutput.model_validate(j), known_ids=known)
    except ValidationError:
        return errs + ["not_recap_points"]
    errs += [f"uncited_article:{a}" for a in uncited_articles(recap, selected_ids=selected)]
    errs += [f"thin_section:{h}" for h in thin_sections(recap)]
    return errs + _banned_words(j, CERTAINTY_WORDS)


#: 入力 (user) を見る検査。課題の印 -> 検査
INPUT_AWARE_CHECKS: dict[str, Callable[[Any, str], list[str]]] = {
    "event_news": schema_errors_event_news,
    "spotlight": schema_errors_spotlight,
    "spotlight_events": schema_errors_spotlight_events,
    "spotlight_notes": schema_errors_spotlight_notes,
    "spotlight_outlook": schema_errors_spotlight_outlook,
    "synthesis": schema_errors_synthesis,
    "deep_dive_select": schema_errors_deep_dive_select,
    "deep_dive": schema_errors_deep_dive,
}
