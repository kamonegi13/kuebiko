"""純関数: 本番の保存値 → 審査役に見せる出力の文・材料の文。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

_EV_SECTIONS = (
    ("key_points", "要点"),
    ("facts", "事実"),
    ("caveats", "但し書き"),
    ("unknowns", "未解明点"),
    ("discrepancies", "食い違い"),
)
_SYN_SECTIONS = (
    ("weight_section", "重み"),
    ("chain_section", "連鎖"),
    ("cog_section", "重心"),
    ("spillover_section", "波及"),
    ("pir_section", "情報要求"),
)
_MATERIAL_START = "## 対象記事"
_MATERIAL_END = "\n## 出力"
_JUDGMENT_KEYS = ("id", "claim", "confidence", "claim_type", "domain", "evidence", "hypotheses")
_EVIDENCE_KEYS = ("excerpt", "polarity", "article_id", "attribution_basis", "source_tier")
_ESTIMATE_EXTRA = (
    "forecasts",
    "indicators",
    "alternatives",
    "key_assumptions",
    "missing_evidence",
    "source_caveat",
    "freshness_note",
)


def _entry_text(e: Any) -> str:
    if isinstance(e, Mapping):
        text = str(e.get("text", ""))
        src = e.get("source_index")
        return f"[記事 {src}] {text}" if src is not None else text
    return str(e)


def event_news_output(headline: str, body: Mapping[str, Any]) -> str:
    lines = [f"見出し: {body.get('headline') or headline}", f"BLUF: {body.get('bluf', '')}"]
    for key, label in _EV_SECTIONS:
        entries = body.get(key) or []
        if entries:
            lines.append(f"{label}:")
            lines.extend(f"- {_entry_text(e)}" for e in entries)
    return "\n".join(lines)


def event_news_material(prompt_text: str) -> str:
    """prompt_text から材料 (対象記事 + 識別子カタログ) だけを切り出す。指示の節は外す。"""
    start = prompt_text.find(_MATERIAL_START)
    if start < 0:
        return prompt_text
    end = prompt_text.find(_MATERIAL_END, start)
    return prompt_text[start : end if end > 0 else len(prompt_text)].strip()


def synthesis_output(row: Mapping[str, Any]) -> str:
    parts = [f"見出し: {row.get('headline', '')}"]
    for key, label in _SYN_SECTIONS:
        text = str(row.get(key) or "").strip()
        if text:
            parts.append(f"## {label}\n{text}")
    return "\n\n".join(parts)


def synthesis_material(tradecraft: Mapping[str, Any]) -> str:
    """tradecraft の grounded_estimate (描画の入力 = 台帳の判定) と見通しの欄を JSON にする。"""
    est = tradecraft.get("grounded_estimate") or {}
    judgments = []
    for j in est.get("judgments") or []:
        jj = {k: j.get(k) for k in _JUDGMENT_KEYS if k in j}
        jj["evidence"] = [
            {k: ev.get(k) for k in _EVIDENCE_KEYS if k in ev} for ev in j.get("evidence") or []
        ]
        judgments.append(jj)
    extra = {k: tradecraft.get(k) for k in _ESTIMATE_EXTRA if tradecraft.get(k)}
    return json.dumps({"judgments": judgments, **extra}, ensure_ascii=False, indent=0)


def spotlight_output(row: Mapping[str, Any]) -> str:
    parts = [f"見出し: {row.get('headline', '')}", f"見通し:\n{row.get('outlook', '')}"]
    for key, label in (("caveats", "但し書き"), ("unknowns", "未解明点")):
        raw = row.get(key)
        entries: Sequence[Any]
        try:
            entries = json.loads(raw) if isinstance(raw, str) and raw.startswith("[") else []
        except json.JSONDecodeError:
            entries = []
        if not entries and raw:
            entries = [raw]
        if entries:
            parts.append(f"{label}:\n" + "\n".join(f"- {e}" for e in entries))
    return "\n\n".join(parts)


def spotlight_material(
    articles: Sequence[Mapping[str, Any]], previous: Mapping[str, Any] | None
) -> str:
    blocks = []
    for i, a in enumerate(articles, 1):
        blocks.append(
            f"[主要事象 {i}] {a.get('title', '')} ({a.get('feed_title', '')}, "
            f"{a.get('published_at', '')}, 重要度 {a.get('importance', '')})\n"
            f"要約: {a.get('summary') or ''}\n本文の冒頭: {a.get('body_head') or ''}"
        )
    if previous:
        blocks.append(
            f"[前期の Spotlight] 見出し: {previous.get('headline', '')}\n"
            f"見通し: {previous.get('outlook', '')}"
        )
    return "\n\n".join(blocks)
