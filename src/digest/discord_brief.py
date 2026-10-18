"""朝夕ブリーフの Discord 要点版 (2026-10-18 再設計)。

旧要点版は分析の文 (比重・重心) と台帳の操作記録、機械で 160 字に切った PIR Focus、
z 値つきの急増を流していた。Discord は「何が起きたか・どの問いが動いたか・何を確かめるか」
だけを 1 通 (3,500 字) に収め、分析の文と全文は Web (daily_briefs → /app/daily-brief) で読む。

入力は ``build_brief_payload`` の形 (daily_briefs.payload と同じ) の dict なので、
保存済みのブリーフからも同じ要点版を組み直せる (比較・検証用)。純粋関数・LLM 呼出なし。

構成 (朝): 冒頭 1 行 → 高脅威 → PIR ブリーフ (動いた問いだけ) → 状況総括 1 文 → 急増 → PIR Focus。
収まらないときは後ろの節から件数とリンクに畳む。
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from src.cti.source_basis import SOURCE_TIER_MARKER

#: 要点版を使う (0 で旧版)。旧版は runners._compose_compact_summary。
ENV_FLAG = "BRIEF_DISCORD_V2"

#: 1 通 (publisher の DESC_CHUNK_SAFE=3500) に収める本文の上限。BLUF・段落整形の余白を引いてある
COMPACT_BUDGET = 3300
HIGH_THREAT_MAX_ITEMS = 8
HIGH_THREAT_COLLAPSED_ITEMS = 4
TITLE_MAX = 90
BURST_MAX_ITEMS = 3
#: 1 文の目安上限。超えたら読点で切り、読点が無ければ全文を残す (hard cap まで)
SENTENCE_SOFT_MAX = 110
SENTENCE_HARD_FACTOR = 2
#: PIR の答え (claim) は結論そのものなので Focus の要点より長く許す
CLAIM_SENTENCE_MAX = 160

_WEB_BRIEF_PATH = "/app/daily-brief"
_WEB_HIGH_PATH = "/app/news?importance=high"
_NEW_BASELINE_LABEL = "新出"


class BurstLike(Protocol):
    """急増 1 件の表示に要る属性 (forecast.burst.BurstLike が満たす)。

    burst モジュール自体は import しない (test_burst_boundary: 参照してよいのは
    runners の本文組み立てだけ。この module は渡された値を表示するだけ)。
    """

    @property
    def scope(self) -> str: ...
    @property
    def label(self) -> str: ...
    @property
    def today_count(self) -> int: ...
    @property
    def baseline_daily_avg(self) -> float: ...
    @property
    def matched_pir_ids(self) -> tuple[str, ...]: ...


def is_enabled() -> bool:
    return os.environ.get(ENV_FLAG, "1").strip() != "0"


def first_sentence(text: str, max_chars: int = SENTENCE_SOFT_MAX) -> str:
    """最初の 1 文を返す (機械の字数切りをしない)。

    1 文が max_chars を超えるとき: max_chars 以内の最後の読点 (、) で切って「…」を付ける。
    読点が前半に無ければ文全体を残す (語の途中で切らない)。ただし hard cap
    (max_chars × 2) を超える異常に長い文だけは max_chars で切る。
    """
    flat = " ".join(text.split())
    if not flat:
        return ""
    end = flat.find("。")
    sentence = flat[: end + 1] if end >= 0 else flat
    if len(sentence) <= max_chars:
        return sentence
    window = sentence[:max_chars]
    comma = window.rfind("、")
    if comma >= max_chars // 2:
        return window[:comma] + "…"
    if len(sentence) > max_chars * SENTENCE_HARD_FACTOR:
        return window + "…"
    return sentence


def _clip_title(title: str) -> str:
    t = " ".join(title.split())
    return t if len(t) <= TITLE_MAX else t[: TITLE_MAX - 1] + "…"


# ── 各節 ────────────────────────────────────────────────────────────


def _high_threat_lines(items: list[dict[str, Any]], limit: int) -> list[str]:
    lines: list[str] = []
    for it in items[:limit]:
        flag = "🇯🇵 " if it.get("is_japan") else ""
        marker = SOURCE_TIER_MARKER.get(str(it.get("source_tier") or ""), "")
        cat = str(it.get("category_label") or "")
        cat_part = f" ({cat})" if cat else ""
        line = f"・{flag}{_clip_title(str(it.get('title') or ''))}{cat_part}{marker}"
        if it.get("url"):
            line += f" <{it['url']}>"
        lines.append(line)
    return lines


def _high_threat_block(
    ht: dict[str, Any] | None, *, limit: int, base_url: str | None, since_morning: bool
) -> str:
    total = int((ht or {}).get("total") or 0)
    items = list((ht or {}).get("items") or [])
    label = "朝以降の高脅威" if since_morning else "本日の高脅威"
    if total == 0 or not items:
        return f"🔴 **{label}** 0 件"
    shown = _high_threat_lines(items, limit)
    lines = [f"🔴 **{label} (要確認 · {total} 件)**", *shown]
    rest = total - len(shown)
    if rest > 0:
        tail = f"…他 {rest} 件"
        if base_url:
            tail += f" → {base_url}{_WEB_HIGH_PATH}"
        lines.append(tail)
    return "\n".join(lines)


def _pir_brief_block(pb: dict[str, Any] | None, *, collapsed: bool = False) -> str:
    if not pb or not int(pb.get("total") or 0):
        return ""
    moved = list(pb.get("moved") or [])
    head = f"**PIR** 本日、{pb['total']} 問中 {len(moved)} 問の答えが動いた"
    if pb.get("unassessed"):
        head += f" (未評価 {pb['unassessed']} 問)"
    if not moved or collapsed:
        return head
    lines = [head]
    for q in moved:
        claim = first_sentence(str(q.get("claim") or ""), CLAIM_SENTENCE_MAX)
        conf = _CONF_JA.get(str(q.get("confidence") or ""), str(q.get("confidence") or ""))
        lines.append(f"・{q.get('question', '')}")
        stale = "・文面は前回の見立てのまま" if q.get("claim_unrevised") else ""
        lines.append(f"　→ {claim} ({conf}{stale})")
    return "\n".join(lines)


_CONF_JA: dict[str, str] = {"high": "高確度", "moderate": "中確度", "low": "低確度"}


def _synthesis_line(syn: dict[str, Any] | None) -> str:
    headline = str((syn or {}).get("headline") or "")
    sentence = first_sentence(headline, SENTENCE_SOFT_MAX * 2)
    return f"📊 **状況総括** {sentence}" if sentence else ""


def _burst_ratio_label(e: BurstLike) -> str:
    if e.baseline_daily_avg <= 0:
        return _NEW_BASELINE_LABEL
    ratio = e.today_count / e.baseline_daily_avg
    return f"通常の約 {ratio:.0f} 倍" if ratio >= 10 else f"通常の約 {ratio:.1f} 倍"


def _burst_block(events: Sequence[BurstLike]) -> str:
    if not events:
        return ""
    prefix = {"actor": "アクター", "sector": "セクター", "country": "被害国"}
    lines = ["📈 **急増** (言及件数の統計。活動量そのものではない)"]
    for e in events[:BURST_MAX_ITEMS]:
        flag = " ⚑PIR" if e.scope == "actor" and e.matched_pir_ids else ""
        lines.append(
            f"・{prefix.get(e.scope, e.scope)} {e.label}: 本日 {e.today_count} 件 "
            f"({_burst_ratio_label(e)}){flag}"
        )
    rest = len(events) - BURST_MAX_ITEMS
    if rest > 0:
        lines.append(f"…他 {rest} 件")
    return "\n".join(lines)


def _focus_gist(section: dict[str, Any]) -> str:
    gist = first_sentence(str(section.get("summary") or ""), SENTENCE_SOFT_MAX)
    if gist:
        return gist
    for m in section.get("matches") or []:
        if str(m.get("importance")) == "high":
            return _clip_title(str(m.get("title") or ""))
    return ""


def _focus_variants(sections: list[dict[str, Any]]) -> list[str]:
    """長い順の候補。🔴 (high を含む) 領域だけ 1 文で出し、残りは件数。

    収まらないときに使う短い候補 (🔴 を半分・3 件・件数のみ) を後ろに並べる。
    """
    if not sections:
        return [""]
    red = [
        s
        for s in sections
        if any(str(m.get("importance")) == "high" for m in s.get("matches") or [])
    ]
    count_only = f"📍 **PIR Daily Focus** {len(sections)} 領域 (🔴 {len(red)}) → Web"
    if not red:
        return [count_only]

    def render(n: int) -> str:
        shown = red[:n]
        lines = [f"📍 **PIR Daily Focus** (last 24h · {len(sections)} 領域のうち 🔴 {len(red)})"]
        for s in shown:
            highs = [m for m in s.get("matches") or [] if str(m.get("importance")) == "high"]
            all_social = bool(highs) and all(str(m.get("tier")) == "social" for m in highs)
            marker = SOURCE_TIER_MARKER["social"] if all_social else ""
            gist = _focus_gist(s)
            line = f"🔴 **{s.get('title', '')}** ({s.get('total', 0)} 件)"
            line += f" — {gist}{marker}" if gist else marker
            lines.append(line)
        if len(sections) - len(shown) > 0:
            lines.append(f"他 {len(sections) - len(shown)} 領域 → Web")
        return "\n".join(lines)

    sizes = [len(red)]
    for n in (len(red) // 2, 3):
        if 0 < n < sizes[-1]:
            sizes.append(n)
    return [*(render(n) for n in sizes), count_only]


# ── 合成 ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Block:
    name: str
    #: 長い順の候補 (先頭 = 全文)。後ろほど短い。"" = 畳むと消える
    variants: tuple[str, ...]


@dataclass(frozen=True)
class CompactBrief:
    text: str
    collapsed: tuple[str, ...]


def _link_line(base_url: str | None) -> str:
    if base_url:
        return f"📎 全文 (根拠・記事一覧): {base_url}{_WEB_BRIEF_PATH}"
    return "📎 全文 (根拠・記事一覧) は Web「日次ブリーフ」で"


def _join(parts: list[str]) -> str:
    return "\n\n".join(p for p in parts if p)


def _fit(blocks: list[_Block], link: str, budget: int) -> CompactBrief:
    """収まるまで後ろの節から短い候補へ畳む。候補が尽きたらそのまま返す (publisher が分割)。"""
    pick = [0] * len(blocks)

    def render() -> str:
        return _join([b.variants[i] for b, i in zip(blocks, pick, strict=True)] + [link])

    collapsed: list[str] = []
    for idx in range(len(blocks) - 1, -1, -1):
        while len(render()) > budget and pick[idx] < len(blocks[idx].variants) - 1:
            pick[idx] += 1
        if pick[idx] > 0:
            collapsed.append(blocks[idx].name)
        if len(render()) <= budget:
            break
    return CompactBrief(text=render(), collapsed=tuple(reversed(collapsed)))


def _summary_line(
    payload: dict[str, Any], bursts: Sequence[BurstLike], *, since_morning: bool
) -> str:
    ht_total = int((payload.get("high_threats") or {}).get("total") or 0)
    parts = [f"高脅威 {ht_total} 件" + ("(朝以降)" if since_morning else "")]
    pb = payload.get("pir_brief")
    if pb and int(pb.get("total") or 0):
        parts.append(f"PIR {len(pb.get('moved') or [])}/{pb['total']} 問が動いた")
    if bursts:
        parts.append(f"急増 {len(bursts)} 件")
    return "📌 " + " / ".join(parts)


def compose_morning(
    payload: dict[str, Any],
    *,
    bursts: Sequence[BurstLike] = (),
    base_url: str | None,
    budget: int = COMPACT_BUDGET,
) -> CompactBrief:
    """朝ブリーフの Discord 要点版。"""
    ht = payload.get("high_threats")
    focus_variants = _focus_variants(list(payload.get("pir") or []))
    high_full = _high_threat_block(
        ht, limit=HIGH_THREAT_MAX_ITEMS, base_url=base_url, since_morning=False
    )
    high_short = _high_threat_block(
        ht, limit=HIGH_THREAT_COLLAPSED_ITEMS, base_url=base_url, since_morning=False
    )
    pb = payload.get("pir_brief")
    blocks = [
        _Block("summary", (_summary_line(payload, bursts, since_morning=False),)),
        _Block("high_threats", (high_full, high_short)),
        _Block("pir_brief", (_pir_brief_block(pb), _pir_brief_block(pb, collapsed=True))),
        _Block("synthesis", (_synthesis_line(payload.get("synthesis")),)),
        _Block("burst", (_burst_block(bursts), "")),
        _Block("pir_focus", tuple(focus_variants)),
    ]
    return _fit(blocks, _link_line(base_url), budget)


def compose_evening(
    payload: dict[str, Any],
    *,
    prior_headline: str = "",
    base_url: str | None,
    budget: int = COMPACT_BUDGET,
) -> CompactBrief:
    """夕ブリーフの Discord 要点版。高脅威 0 件で見出しも朝と同じなら短い 1 行。"""
    ht = payload.get("high_threats")
    ht_total = int((ht or {}).get("total") or 0)
    syn = payload.get("synthesis")
    headline = str((syn or {}).get("headline") or "").strip()
    link = _link_line(base_url)
    if ht_total == 0 and headline and headline == prior_headline.strip():
        return CompactBrief(
            text=f"🌆 夕の更新: 朝以降の高脅威 0 件、状況総括も朝から変化なし\n{link}", collapsed=()
        )
    high_full = _high_threat_block(
        ht, limit=HIGH_THREAT_MAX_ITEMS, base_url=base_url, since_morning=True
    )
    high_short = _high_threat_block(
        ht, limit=HIGH_THREAT_COLLAPSED_ITEMS, base_url=base_url, since_morning=True
    )
    blocks = [
        _Block("summary", (_summary_line(payload, (), since_morning=True),)),
        _Block("high_threats", (high_full, high_short)),
        _Block("synthesis", (_synthesis_line(syn),)),
    ]
    return _fit(blocks, link, budget)
