"""線でたどった関連事象の節を組む — GraphRAG 共通の render (純粋関数、2026-10-08)。

``src/spotlight/graph_context.py`` の初版 (2026-09-29、Opus 盲検で 12:3 勝ち) を一般化。
実測で、節に乗った線の 97% が「同じアクター」で中身は攻撃者名だけだったため格上げ (同一
キャンペーン扱い) を誘発した (docs/research/llm_training/next_models_s22_n20.md §8.6-8.7)。
ここでは線 1 本ごとの中身を厚くする:

- 相手事象の種別・被害業種/国・時間差・要点 1 文 (最新版の先頭文)
- 共有指標の珍しさ (df) — 「CVE-…: この 60 日で 3 事象」
- 同じ出来事の関連は分類器の確率を「強/中」の言葉で
- 「同じアクター」には種類ごとの凡例を節の冒頭に 1 回 (線ごとに繰り返さない)

取り方: 呼び手が優先したい指標 (アクター id 群・業種・国・CVE) に一致する線を先に、
同じアクターはハブ抑制 (1 アクターあたり節全体で上限) する。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

#: 節に載せる線の上限 (プロンプトの肥大を抑える)
MAX_LINES = 25
#: 1 アクターあたり、節全体で「同じアクター」の線を何本まで残すか (ハブ抑制)
SAME_ACTOR_HUB_CAP = 3
#: 分類器の確率がこれ以上なら「強」、それ未満は「中」(確度の言葉だけ出す — 数字は出さない)
STRONG_CONFIDENCE_P = 0.85
#: 相手事象の要点を切る目安の字数 (文の境界で切る。機械的な字数切りはしない)
SUMMARY_CLIP_CHARS = 120

_REL_LABELS = {
    "follow_up": "続報",
    "incident": "同じ出来事の関連",
    "side": "同じ出来事の別の側面",
    "campaign": "同一キャンペーン",
    "contains": "包含 (まとめ記事)",
    "same_actor": "同じアクター",
    "same_capability": "珍しい道具の共有",
    "same_nation": "同じ帰属国 (別アクター)",
    "same_target": "同じ業種・国の被害",
}
_BASIS = {
    "victim": "被害組織",
    "cve": "CVE",
    "cap": "道具",
    "actor": "攻撃者",
    "nation": "帰属国",
    "target": "標的",
}
_NOTE = (
    "上の線は、事象が共有する指標 (被害組織・CVE・道具・攻撃者) から機械的に導いたもの"
    "である。書くときは線を根拠として引用してよいが、線に無い事象どうしの関係は推測しない。"
)
_SAME_ACTOR_LEGEND = (
    "「同じアクター」の線は攻撃者名の共有だけを示す。同一キャンペーン・同一の侵入経路の"
    "根拠にはならない。"
)
#: 節全体での「同じ帰属国」の上限 (アクターより弱い線なので最後に、少なく)
SAME_NATION_CAP = 5
_SAME_NATION_LEGEND = (
    "「同じ帰属国」の線は、別々の国家系アクターの帰属国が同じことだけを示す。同じ作戦・"
    "同じ指揮系統・連携の根拠にはならない。"
)
_CANDIDATE_HEADERS = ("## この SIR にマッチした", "## この PIR にマッチした")
#: 同じ出来事の系統 (同じアクターより先に出す)
_INCIDENT_LIKE = frozenset({"follow_up", "side", "contains", "incident"})


@dataclass(frozen=True)
class EventContext:
    """「相手事象」の表示を厚くする補助情報 (すべて省略可、既定は空)。"""

    kind: str = ""
    sector: str = ""
    country: str = ""
    #: 最新版の要約の先頭文 (120 字程度。呼び手が ``_clip_sentence`` 相当で整えておく)
    summary: str = ""


@dataclass(frozen=True)
class PriorityHint:
    """呼び手が優先したい指標。一致する線を節の先頭側に出す。"""

    actor_ids: frozenset[str] = frozenset()
    sectors: frozenset[str] = frozenset()
    countries: frozenset[str] = frozenset()
    cves: frozenset[str] = frozenset()

    def is_empty(self) -> bool:
        return not (self.actor_ids or self.sectors or self.countries or self.cves)


@dataclass(frozen=True)
class _Found:
    """1 本の線 (節に出す前の中間表現)。"""

    group: int  # 0=同じ出来事の系統、1=同じアクター (系統を先に出す)
    rel_type: str
    neg_ts: float
    priority_hit: bool
    actor_ids: frozenset[str]
    line: str


def clip_sentence(text: str, limit: int = SUMMARY_CLIP_CHARS) -> str:
    """文の境界で切る (字数で機械的に切らない — 末尾の留保が落ちて誤読を生むため)。"""
    text = text.strip()
    if len(text) <= limit:
        return text
    head = text[: limit + 20]  # 境界探索の余白
    for mark in ("。", "．", ". "):
        idx = head.rfind(mark, 0, limit + len(mark))
        if idx > 0:
            return head[: idx + len(mark.rstrip())]
    return text[:limit].rstrip() + "…"


def _basis_label(
    basis: str,
    actor_name: Callable[[str], str],
    rarity: Mapping[str, int],
    rarity_window_days: int,
) -> str:
    kind, _, value = basis.partition(":")
    label_value = actor_name(value) if kind == "actor" else value
    text = f"{_BASIS.get(kind, kind)}: {label_value}"
    df = rarity.get(basis)
    if df is not None:
        text += f" (この {rarity_window_days} 日で {df} 事象)"
    return text


def _confidence_word(p: str) -> str:
    try:
        value = float(p)
    except ValueError:
        return ""
    return "強" if value >= STRONG_CONFIDENCE_P else "中"


def _gap_label(ev_first: datetime | None, other_first: datetime) -> str:
    if ev_first is None:
        return ""
    gap_days = (ev_first.date() - other_first.date()).days
    if gap_days > 0:
        return f"{gap_days} 日前"
    if gap_days < 0:
        return f"{-gap_days} 日後"
    return "同日"


def _refs(numbers: Sequence[int]) -> str:
    return "".join(f"[{n}]" for n in sorted(set(numbers)))


def _priority_hit(
    basis: Sequence[str],
    ctx: EventContext | None,
    priority: PriorityHint,
) -> bool:
    if priority.is_empty():
        return False
    for b in basis:
        kind, _, value = b.partition(":")
        if kind == "actor" and value in priority.actor_ids:
            return True
        if kind == "cve" and value in priority.cves:
            return True
        if kind == "target":
            sector, _, country = value.partition("|")
            if sector in priority.sectors or country in priority.countries:
                return True
    if ctx is not None:
        if ctx.sector and ctx.sector in priority.sectors:
            return True
        if ctx.country and ctx.country in priority.countries:
            return True
    return False


def _actor_ids_in(basis: Sequence[str]) -> frozenset[str]:
    out: set[str] = set()
    for b in basis:
        kind, _, value = b.partition(":")
        if kind == "actor":
            out.add(value)
    return frozenset(out)


def _apply_hub_cap(found: list[_Found], cap: int) -> list[_Found]:
    """「同じアクター」はアクターごとに節全体で ``cap`` 本まで (多産なアクターの独占を防ぐ)。"""
    kept: list[_Found] = []
    seen: dict[str, int] = defaultdict(int)
    for item in sorted(found, key=lambda f: (not f.priority_hit, f.neg_ts)):
        if item.group != 1:
            kept.append(item)
            continue
        actor_over_cap = any(seen[a] >= cap for a in item.actor_ids)
        if actor_over_cap:
            continue
        for a in item.actor_ids:
            seen[a] += 1
        kept.append(item)
    return kept


def _interleave_by_type(items: list[_Found]) -> list[_Found]:
    """同じ出来事の系統内で種類が偏らないよう、種類ごとの順をラウンドロビンで混ぜる。"""
    by_type: dict[str, list[_Found]] = defaultdict(list)
    order: list[str] = []
    for item in items:
        if item.rel_type not in by_type:
            order.append(item.rel_type)
        by_type[item.rel_type].append(item)
    out: list[_Found] = []
    while any(by_type[t] for t in order):
        for t in order:
            if by_type[t]:
                out.append(by_type[t].pop(0))
    return out


def render_relation_section(
    article_ids: Sequence[str],
    *,
    members: Mapping[str, Sequence[str]],
    relations: Mapping[str, Sequence[Any]],
    first_reported: Mapping[str, datetime],
    headlines: Mapping[str, str],
    window_end: datetime,
    actor_name: Callable[[str], str],
    max_lines: int = MAX_LINES,
    event_context: Mapping[str, EventContext] | None = None,
    indicator_rarity: Mapping[str, int] | None = None,
    rarity_window_days: int = 60,
    priority: PriorityHint | None = None,
    same_actor_cap: int = SAME_ACTOR_HUB_CAP,
) -> str:
    """線の節のテキスト (純粋関数)。線が無ければ空文字。

    ``article_ids`` はプロンプトの候補一覧の順 (番号 = 位置 + 1)。すべての拡張引数
    (``event_context`` 等) は省略可で、省略時は旧 ``render_graph_context`` と同じ出力になる。
    """
    event_context = event_context or {}
    indicator_rarity = indicator_rarity or {}
    priority = priority or PriorityHint()

    events: dict[str, list[int]] = defaultdict(list)
    for n, aid in enumerate(article_ids, start=1):
        for ev in members.get(aid, ()):
            events[ev].append(n)

    # 候補外の同じ事象へ複数の候補から線が伸びるとき (多産な国・アクター) は 1 行にまとめ、
    # 起点の記事番号を並べる。行ごとに繰り返すと 1 事象が節を占める (2026-10-08 実測で 12 行)
    sources: dict[tuple[str, str], list[int]] = defaultdict(list)
    for ev, nums in events.items():
        for r in relations.get(ev, ()):
            other = r.b if r.a == ev else r.a
            if other not in events:
                sources[(other, r.rel_type)].extend(nums)
    emitted: set[tuple[str, str]] = set()
    seen_pairs: set[tuple[str, str, str]] = set()
    found: list[_Found] = []
    for ev in events:
        for r in relations.get(ev, ()):
            other = r.b if r.a == ev else r.a
            if r.rel_type == "same_nation" and other in events:
                # 候補どうしの同じ帰属国は自明 (国別の SIR では候補の全員が同じ国) — 候補外だけ
                continue
            if other not in events:
                if (other, r.rel_type) in emitted:
                    continue
                emitted.add((other, r.rel_type))
            first = first_reported.get(other)
            if other == ev or first is None or first >= window_end:
                continue
            key = (min(ev, other), max(ev, other), r.rel_type)
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            if other in events:
                target = "記事 " + _refs(events[other])
            elif headlines.get(other):
                target = f"候補外の事象「{headlines[other]}」({first.date().isoformat()})"
            else:
                continue
            ctx = event_context.get(other)
            basis_labels = [
                _basis_label(b, actor_name, indicator_rarity, rarity_window_days) for b in r.basis
            ]
            basis = "、".join(basis_labels) or "要約の類似"
            label = _REL_LABELS.get(r.rel_type, r.rel_type)
            origin = sources.get((other, r.rel_type)) or events[ev]
            parts = [f"- 記事 {_refs(origin)} ↔ {target}: {label}"]
            if r.rel_type == "incident" and "p" in getattr(r, "extra", {}):
                word = _confidence_word(r.extra["p"])
                if word:
                    parts.append(f" (確度: {word})")
            parts.append(f" (共有: {basis})")
            # まとめた行の時間差は起点ごとに違うので出さない (相手の日付は見出しの後ろにある)
            gap = _gap_label(first_reported.get(ev), first) if len(set(origin)) == 1 else ""
            if gap:
                parts.append(f" ({gap})")
            if ctx is not None:
                detail_bits = []
                if ctx.kind:
                    detail_bits.append(f"種別: {ctx.kind}")
                if ctx.sector or ctx.country:
                    where = ctx.sector + ("/" + ctx.country if ctx.country else "")
                    detail_bits.append(f"被害: {where}")
                if detail_bits:
                    parts.append(" [" + "、".join(detail_bits) + "]")
                if ctx.summary:
                    parts.append(f" — {clip_sentence(ctx.summary)}")
            line = "".join(parts)
            found.append(
                _Found(
                    group=0 if r.rel_type in _INCIDENT_LIKE else 1,
                    rel_type=r.rel_type,
                    neg_ts=-first.timestamp(),
                    priority_hit=_priority_hit(r.basis, ctx, priority),
                    actor_ids=_actor_ids_in(r.basis),
                    line=line,
                )
            )
    if not found:
        return ""

    incident = _interleave_by_type(
        sorted(
            (f for f in found if f.group == 0),
            key=lambda f: (not f.priority_hit, f.neg_ts),
        )
    )
    same_actor = _apply_hub_cap(
        [f for f in found if f.group == 1 and f.rel_type != "same_nation"], same_actor_cap
    )
    same_nation = sorted(
        (f for f in found if f.rel_type == "same_nation"),
        key=lambda f: (not f.priority_hit, f.neg_ts),
    )[:SAME_NATION_CAP]
    # 同じアクターが節を埋めても、同じ帰属国の枠 (上限まで) は残す
    room = max(0, max_lines - len(incident) - len(same_nation))
    ordered = incident + same_actor[:room] + same_nation
    lines = [f.line for f in ordered[:max_lines]]
    shown = {f.rel_type for f in ordered[:max_lines]}
    legends = [
        text
        for rel_type, text in (
            ("same_actor", _SAME_ACTOR_LEGEND),
            ("same_nation", _SAME_NATION_LEGEND),
        )
        if rel_type in shown
    ]
    legend = "\n".join([*legends, _NOTE])
    return (
        f"## 線でたどった関連事象 ({len(lines)} 本 / 全 {len(found)} 本)\n"
        + "\n".join(lines)
        + f"\n\n{legend}\n\n"
    )


def insert_after_candidates(prompt: str, block: str) -> str:
    """候補一覧の直後 (次の節の見出しの前) に節を差し込む。見出しが無ければ末尾へ。"""
    if not block:
        return prompt
    head = max(prompt.find(h) for h in _CANDIDATE_HEADERS)
    nxt = prompt.find("\n## ", head + 5) if head >= 0 else -1
    if nxt < 0:
        return prompt + "\n\n" + block
    return prompt[: nxt + 1] + block + prompt[nxt + 1 :]


__all__ = [
    "MAX_LINES",
    "SAME_ACTOR_HUB_CAP",
    "EventContext",
    "PriorityHint",
    "clip_sentence",
    "insert_after_candidates",
    "render_relation_section",
]
