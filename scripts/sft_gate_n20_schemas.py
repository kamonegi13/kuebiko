"""n20 の課題 (event_news / spotlight / synthesis / deep_dive_select / deep_dive) の出力検査。

契約は data/eval/n20_contract_2026_11_02.md §1 (4)。入力 (user) を見ないと決まらない検査
(anchor の逐語実在・index の範囲・判定 id の所属) があるため、各検査は ``(j, user) -> errors``。
翻訳 (translate) と MITRE 和訳 (actor_sync) は自由文の課題で、JSON でないことだけを
``sft_quality_gate.FREE_TEXT_TASKS`` が見る。

Spotlight は利用者判断 U-A: 観点は a|b だけを LLM が書き、(c) は決定論の描画、
(d) (speculation) は外す。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

#: anchor (原文の逐語) の長さ (字)
ANCHOR_MIN_CHARS = 20
ANCHOR_MAX_CHARS = 120
KEY_EVENTS_RANGE = (5, 8)
OUTLOOK_POINTS_MAX = 3
OUTLOOK_ASPECTS = frozenset({"a", "b"})
DEEP_DIVE_SELECT_MAX = 50  # RUBRIC_CHUNK_SIZE (25) * 2
DEEP_DIVE_CHUNK_SIZE = 25
DEEP_DIVE_SECTIONS_MAX = 7
DEEP_DIVE_HEADING_MAX = 40
SCORE_KEYS = frozenset({"pir", "roi", "novelty"})
SCORE_RANGE = (0.0, 5.0)

#: 確度を LLM が断定する語 (確度語はコードが描く / 断定を強めない契約)
CERTAINTY_WORDS = ("確実に", "間違いなく", "疑いなく", "疑いの余地", "断言", "必ず", "明らかに")
#: 状況総括の LLM 出力に含めない語 (反証・対立仮説・確度はコード描画 — U-B)
SYNTHESIS_CODE_RENDERED_WORDS = ("反証", "対立仮説", "確度")

EVENT_NEWS_KEYS = frozenset(
    {"headline", "bluf", "key_points", "facts", "discrepancies", "caveats", "unknowns"}
)
ANCHORED_SECTIONS = ("facts", "caveats", "discrepancies")
SYNTHESIS_KEYS = frozenset({"headline_clause", "items", "watch_clause"})
SYNTHESIS_ITEM_KEYS = frozenset({"judgment_id", "world_clause", "implication_clause"})

_CANDIDATE_LINE = re.compile(r"^\[(\d+)\] \*\*", re.M)
_ID_TOKEN = re.compile(r"\{I(\d+)\}")
_CATALOG_LINE = re.compile(r"^I(\d+) = ", re.M)
_JUDGMENT_ID = re.compile(r"\[(s-[0-9a-z\-]+)\]")
_WS = re.compile(r"\s+")


def _squash(text: str) -> str:
    return _WS.sub(" ", text).strip()


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


def _anchor_errors(item: Any, user: str, squashed_user: str, label: str) -> list[str]:
    if not isinstance(item, dict) or "anchor" not in item:
        return [f"anchor_missing:{label}"]
    anchor = item["anchor"]
    if not isinstance(anchor, str) or not (ANCHOR_MIN_CHARS <= len(anchor) <= ANCHOR_MAX_CHARS):
        return [f"anchor_length:{label}"]
    if anchor not in user and _squash(anchor) not in squashed_user:
        return [f"anchor_not_verbatim:{label}"]
    return []


def schema_errors_event_news(j: Any, user: str) -> list[str]:
    if not isinstance(j, dict):
        return ["not_object"]
    errs = [f"extra_key:{k}" for k in j if k not in EVENT_NEWS_KEYS]
    errs += [f"missing_key:{k}" for k in EVENT_NEWS_KEYS if k not in j]
    if not str(j.get("bluf") or "").strip():
        errs.append("empty_bluf")
    n_src = len(set(_CANDIDATE_LINE.findall(user)))
    squashed = _squash(user)
    for section in ANCHORED_SECTIONS:
        items = j.get(section) or []
        if not isinstance(items, list):
            errs.append(f"not_list:{section}")
            continue
        for i, item in enumerate(items):
            errs += _anchor_errors(item, user, squashed, f"{section}[{i}]")
            idx = item.get("source_index", 0) if isinstance(item, dict) else 0
            if section == "facts" and isinstance(idx, int) and idx and not 1 <= idx <= n_src:
                errs.append(f"source_index_out_of_range:{section}[{i}]")
    catalog = {int(x) for x in _CATALOG_LINE.findall(user)}
    if {int(x) for x in _ID_TOKEN.findall("\n".join(_texts(j)))} - catalog:
        errs.append("identifier_token_not_in_catalog")
    return errs


def _spotlight_events(j: dict[str, Any], n_cand: int) -> list[str]:
    errs: list[str] = []
    key_events = j.get("key_events")
    if not isinstance(key_events, list):
        return ["key_events_not_list"]
    if not KEY_EVENTS_RANGE[0] <= len(key_events) <= KEY_EVENTS_RANGE[1]:
        errs.append(f"key_events_count:{len(key_events)}")
    indexes = [k.get("index") if isinstance(k, dict) else None for k in key_events]
    if any(not isinstance(i, int) or not 1 <= i <= n_cand for i in indexes):
        errs.append("key_events_index_out_of_range")
    if len(set(indexes)) != len(indexes):
        errs.append("key_events_index_duplicate")
    for note in j.get("event_notes") or []:
        i = note.get("index") if isinstance(note, dict) else None
        if not isinstance(i, int) or not 1 <= i <= n_cand:
            errs.append("event_notes_index_out_of_range")
    return errs


def schema_errors_spotlight(j: Any, user: str) -> list[str]:
    if not isinstance(j, dict):
        return ["not_object"]
    errs = [f"forbidden_key:{k}" for k in ("speculation", "outlook") if k in j]
    for key in ("headline", "key_events", "outlook_points"):
        if key not in j:
            errs.append(f"missing_key:{key}")
    errs += _spotlight_events(j, len(set(_CANDIDATE_LINE.findall(user))))
    squashed = _squash(user)
    for section in ("key_events", "event_notes"):
        for i, item in enumerate(j.get(section) or []):
            errs += _anchor_errors(item, user, squashed, f"{section}[{i}]")
    points = j.get("outlook_points") or []
    if not isinstance(points, list):
        errs.append("outlook_points_not_list")
    else:
        if len(points) > OUTLOOK_POINTS_MAX:
            errs.append(f"outlook_points_over_max:{len(points)}")
        for p in points:
            aspect = p.get("aspect") if isinstance(p, dict) else None
            if aspect not in OUTLOOK_ASPECTS:
                errs.append(f"outlook_aspect_out_of_range:{aspect}")
    return errs + _banned_words(j, CERTAINTY_WORDS)


def schema_errors_synthesis(j: Any, user: str) -> list[str]:
    if not isinstance(j, dict):
        return ["not_object"]
    errs = [f"extra_key:{k}" for k in j if k not in SYNTHESIS_KEYS]
    errs += [f"missing_key:{k}" for k in SYNTHESIS_KEYS if k not in j]
    for key in ("headline_clause", "watch_clause"):
        if not str(j.get(key) or "").strip():
            errs.append(f"empty:{key}")
    known = set(_JUDGMENT_ID.findall(user))
    seen: set[str] = set()
    for i, item in enumerate(j.get("items") or []):
        if not isinstance(item, dict) or set(item) != SYNTHESIS_ITEM_KEYS:
            errs.append(f"item_keys:{i}")
            continue
        jid = item["judgment_id"]
        if jid in seen:
            errs.append(f"judgment_id_duplicate:{i}")
        if known and jid not in known:
            errs.append(f"judgment_id_not_in_input:{i}")
        seen.add(jid)
        if not all(str(item[k] or "").strip() for k in ("world_clause", "implication_clause")):
            errs.append(f"empty_clause:{i}")
    return errs + _banned_words(j, CERTAINTY_WORDS + SYNTHESIS_CODE_RENDERED_WORDS)


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


def _deep_dive_section_errors(sec: Any, i: int, user: str) -> list[str]:
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
        elif any(not isinstance(a, str) or a not in user for a in ids):
            errs.append(f"article_id_not_in_input:{i}.{k}")
    return errs


def schema_errors_deep_dive(j: Any, user: str) -> list[str]:
    if not isinstance(j, dict):
        return ["not_object"]
    errs = [f"extra_key:{k}" for k in j if k != "sections"]
    sections = j.get("sections")
    if not isinstance(sections, list) or not sections:
        return errs + ["sections_empty"]
    if len(sections) > DEEP_DIVE_SECTIONS_MAX:
        errs.append(f"sections_over_max:{len(sections)}")
    for i, sec in enumerate(sections):
        errs += _deep_dive_section_errors(sec, i, user)
    return errs + _banned_words(j, CERTAINTY_WORDS)


#: 入力 (user) を見る検査。課題の印 -> 検査
INPUT_AWARE_CHECKS: dict[str, Callable[[Any, str], list[str]]] = {
    "event_news": schema_errors_event_news,
    "spotlight": schema_errors_spotlight,
    "synthesis": schema_errors_synthesis,
    "deep_dive_select": schema_errors_deep_dive_select,
    "deep_dive": schema_errors_deep_dive,
}
