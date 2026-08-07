"""日次ブリーフの構造化 payload (Web 構造描画用、2026-07-12)。

daily_briefs.summary は Discord 向けに合成した 1 本のテキストで、Web でそのまま
展開すると壁のようなプローズになる (ユーザー指摘)。canonical な構造
(StatusSynthesisRecord の節 column / PirFocusSection) から JSON payload を生成時に
併せて永続化し、frontend はテキスト parse でなく構造から描画する
(状態中心アーキテクチャ: 報告は射影 — Web 射影がテキスト射影を経由しない)。

payload 形:
{
  "synthesis": {
    "headline": str,
    "sections": [{"key", "label", "text"}],          # 空節は除外
    "tradecraft": {"leading_assessment", "alternatives", "key_assumptions",
                   "indicators"} | null
  } | null,
  "pir": [{"title", "total", "summary",
           "matches": [{"title", "url", "importance", "feed", "tier"}]}]
}
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from src.synthesis.discord import _SECTION_HEADERS

if TYPE_CHECKING:
    from src.digest.pir_daily_focus import PirFocusSection
    from src.storage.run_history import StatusSynthesisRecord

# tradecraft JSON のうち payload に通す list キー (未知キーは通さない)
_TRADECRAFT_LIST_KEYS: tuple[str, ...] = ("alternatives", "key_assumptions", "indicators")


def build_brief_payload(
    *,
    syn_record: StatusSynthesisRecord | None,
    sections: list[PirFocusSection],
) -> dict[str, Any]:
    """daily brief の構造化 payload を組み立てる (純粋関数)。"""
    return {
        "synthesis": _synthesis_payload(syn_record),
        "pir": [_pir_section_payload(s) for s in sections],
    }


def _synthesis_payload(record: StatusSynthesisRecord | None) -> dict[str, Any] | None:
    if record is None:
        return None
    secs: list[dict[str, str]] = []
    for col, header in _SECTION_HEADERS:
        text = (getattr(record, col, "") or "").strip()
        if text:
            secs.append({"key": col, "label": header.removeprefix("■ ").strip(), "text": text})
    return {
        "headline": record.headline.strip(),
        "sections": secs,
        "tradecraft": _parse_tradecraft(record.tradecraft),
    }


def _parse_tradecraft(raw: str) -> dict[str, Any] | None:
    """tradecraft JSON を防御的に parse し、既知キーのみ通す (壊れていれば None)。"""
    if not raw:
        return None
    try:
        tc = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(tc, dict):
        return None
    out: dict[str, Any] = {}
    leading = str(tc.get("leading_assessment", "")).strip()
    if leading:
        out["leading_assessment"] = leading
    for key in _TRADECRAFT_LIST_KEYS:
        items = tc.get(key)
        if not isinstance(items, list):
            continue
        cleaned = [str(it).strip() for it in items if str(it).strip()]
        if cleaned:
            out[key] = cleaned
    return out or None


def _pir_section_payload(section: PirFocusSection) -> dict[str, Any]:
    from src.cti.source_basis import classify_source_tier

    matches: list[dict[str, str]] = []
    for m in section.matches:
        matches.append(
            {
                "title": (m.title or m.article_id).strip(),
                "url": m.url or "",
                "importance": (m.importance or "low").lower(),
                "feed": m.feed_title or "",
                "tier": classify_source_tier(m.feed_title or "", m.feed_url or ""),
            }
        )
    return {
        "title": section.pir.title,
        "total": section.total_match_count,
        "summary": section.llm_summary,
        "matches": matches,
    }
