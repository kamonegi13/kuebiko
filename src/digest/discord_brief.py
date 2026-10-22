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
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from src.cti.source_basis import SOURCE_TIER_MARKER
from src.digest import focus_v6

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

#: 載せる事象がこの数以下の回は「薄い日」として別チャンネルの件数を 1 行足す
THIN_EVENTS_MAX = 3
_OTHER_CHANNEL_LABELS: tuple[tuple[str, str], ...] = (
    ("alert", "alert"),
    ("japan_watch", "日本関連の注視"),
)
_KANA_RE = re.compile(r"[\u3041-\u309f\u30a0-\u30ff]")
_HAN_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_WORD_RE = re.compile(r"[A-Za-z]{3,}")
_FOREIGN_MIN_LATIN_WORDS = 3

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
    """最初の 1 文を返す。文の途中で切らない (読点切りの「…」は出さない)。

    1 文が max_chars × 2 以内ならそのまま返す。超える文は述語を残したまま短くできないので
    空文字を返す (呼び出し側が領域名・件数だけにする)。
    """
    flat = " ".join(text.split())
    if not flat:
        return ""
    end = flat.find("。")
    sentence = flat[: end + 1] if end >= 0 else flat
    if len(sentence) > max_chars * SENTENCE_HARD_FACTOR:
        return ""
    return sentence


def is_foreign_title(title: str) -> bool:
    """かなを含まず、漢字 (中国語) か英単語 3 語以上で書かれた見出し (= 日本語の見出しではない)。"""
    if not title or _KANA_RE.search(title):
        return False
    if _HAN_RE.search(title):
        return True
    return len(_LATIN_WORD_RE.findall(title)) >= _FOREIGN_MIN_LATIN_WORDS


def _clip_title(title: str) -> str:
    t = " ".join(title.split())
    return t if len(t) <= TITLE_MAX else t[: TITLE_MAX - 1] + "…"


# ── 各節 ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _ThreatGroup:
    """同じ事象の高脅威記事のまとまり (事象の無い記事は 1 件だけの群)。"""

    members: tuple[dict[str, Any], ...]

    @property
    def lead(self) -> dict[str, Any]:
        return self.members[0]


def _group_threats(items: list[dict[str, Any]]) -> list[_ThreatGroup]:
    """事象 id (event_items の群) ごとにまとめる。出現順を保ち、群の無い記事はそのまま 1 群。"""
    order: list[str] = []
    buckets: dict[str, list[dict[str, Any]]] = {}
    for idx, it in enumerate(items):
        key = str(it.get("event_id") or "") or f"_solo{idx}"
        if key not in buckets:
            order.append(key)
            buckets[key] = []
        buckets[key].append(it)
    return [_ThreatGroup(tuple(buckets[k])) for k in order]


def _title_candidates(group: _ThreatGroup) -> list[str]:
    """表示する見出しの候補 (優先順)。複数報道は事象の見出しを先に、外国語の見出しは
    日本語の欄 (title_ja → 事象の見出し) があればそれに替える。"""
    lead = group.lead
    headline = str(lead.get("event_headline") or "")
    title = str(lead.get("title") or "")
    ja = str(lead.get("title_ja") or "")
    if len(group.members) > 1 and headline:
        return [headline, title]
    if not is_foreign_title(title):
        return [title, headline] if headline and not is_foreign_title(headline) else [title]
    japanese = [t for t in (ja, headline) if t and not is_foreign_title(t)]
    return [*japanese, title]


def _threat_title(group: _ThreatGroup) -> str:
    """候補のうち 1 行に収まる最初のもの。どれも長ければ最も短い候補を (最後の手段で) 切る。"""
    candidates = [c for c in _title_candidates(group) if c]
    for c in candidates:
        if len(" ".join(c.split())) <= TITLE_MAX:
            return c
    return min(candidates, key=len) if candidates else ""


def _group_marker(group: _ThreatGroup) -> str:
    """SNS 由来の印は、まとまり全体が SNS のときだけ付ける。"""
    tiers = {str(m.get("source_tier") or "") for m in group.members}
    return SOURCE_TIER_MARKER.get(next(iter(tiers)), "") if len(tiers) == 1 else ""


def _group_url(group: _ThreatGroup) -> str:
    for m in group.members:
        if m.get("url") and str(m.get("source_tier") or "") != "social":
            return str(m["url"])
    return str(group.lead.get("url") or "")


def _high_threat_lines(groups: list[_ThreatGroup], limit: int) -> list[str]:
    lines: list[str] = []
    for g in groups[:limit]:
        flag = "🇯🇵 " if any(m.get("is_japan") for m in g.members) else ""
        cat = str(g.lead.get("category_label") or "")
        cat_part = f" ({cat})" if cat else ""
        count = f" (報道 {len(g.members)} 件)" if len(g.members) > 1 else ""
        line = f"・{flag}{_clip_title(_threat_title(g))}{cat_part}{count}{_group_marker(g)}"
        if url := _group_url(g):
            line += f" <{url}>"
        lines.append(line)
    return lines


def _threat_group_total(ht: dict[str, Any] | None) -> int:
    """事象数 (payload に載っていない分は 1 件 1 事象として数える)。"""
    items = list((ht or {}).get("items") or [])
    total = int((ht or {}).get("total") or 0)
    return len(_group_threats(items)) + max(0, total - len(items))


def _high_threat_block(
    ht: dict[str, Any] | None, *, limit: int, base_url: str | None, since_morning: bool
) -> str:
    total = int((ht or {}).get("total") or 0)
    items = list((ht or {}).get("items") or [])
    label = "朝以降の高脅威" if since_morning else "本日の高脅威"
    if total == 0 or not items:
        return f"🔴 **{label}** 0 件"
    groups = _group_threats(items)
    shown = _high_threat_lines(groups, limit)
    n_events = _threat_group_total(ht)
    grouped = n_events < total
    count_part = f"{total} 件 · {n_events} 事象" if grouped else f"{total} 件"
    lines = [f"🔴 **{label} (要確認 · {count_part})**", *shown]
    rest = n_events - len(shown)
    if rest > 0:
        tail = f"…他 {rest} {'事象' if grouped else '件'}"
        if base_url:
            tail += f" → {base_url}{_WEB_HIGH_PATH}"
        lines.append(tail)
    return "\n".join(lines)


def _other_channels_block(payload: dict[str, Any]) -> str:
    """薄い回の補足: 同じ時間窓で別チャンネルへ流れた件数。多い回・件数 0 の回は出さない。"""
    other = payload.get("other_channels")
    if not other or _threat_group_total(payload.get("high_threats")) > THIN_EVENTS_MAX:
        return ""
    parts = [
        f"{label} {int(other.get(key) or 0)} 件"
        for key, label in _OTHER_CHANNEL_LABELS
        if int(other.get(key) or 0) > 0
    ]
    return f"📨 別のチャンネル: {'・'.join(parts)}" if parts else ""


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
        lines.append(f"・{q.get('question', '')}" + ("" if claim else f" ({conf})"))
        if claim:
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


def _applicable_summary(summary: str) -> str:
    """先頭の「本 SIR には該当しない」の明示文を飛ばした要点 (v6.2 の長い要点用)。

    該当しない文しか無い、または明示文が無い (従来の短い要点) ときは入力をそのまま返す。
    """
    sentences = [p for p in summary.replace("\n", " ").split("。") if p.strip()]
    if not any(focus_v6.is_not_applicable(p) for p in sentences):
        return summary
    kept = [p for p in sentences if not focus_v6.is_not_applicable(p)]
    return "。".join(kept) + "。" if kept else summary


def _focus_gist(section: dict[str, Any]) -> str:
    summary = _applicable_summary(str(section.get("summary") or ""))
    if summary.strip():
        # 文として収まらない要点は出さない (見出しで代用もしない — 領域名と件数だけにする)
        return first_sentence(summary, SENTENCE_SOFT_MAX)
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
        _Block("other_channels", (_other_channels_block(payload), "")),
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
        _Block("other_channels", (_other_channels_block(payload), "")),
        _Block("synthesis", (_synthesis_line(syn),)),
    ]
    return _fit(blocks, link, budget)
