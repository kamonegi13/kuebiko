"""サイバー事象と地政学事象をつなぐ線の候補 (純粋関数、2026-10-09 試作)。

国家で橋をかける (地政学側の当事国 × サイバー側の帰属国・被害国)。4 つの型:
- ``trigger`` 契機: 地政学の出来事が先、同じ国家系アクターのサイバー活動が後 (14 日以内)
- ``dyad`` 当事国の対: 地政学の出来事が、サイバー側の帰属国と被害国の **両方** を当事国にしている
- ``response`` 応答: サイバー活動が先、後に公式の帰属・制裁・起訴の報 (45 日以内)
- ``intent`` 意図の整合: 社会政治的な意図 (威圧・影響・体制転覆・領土・抑止) のサイバー活動と、
  同じ国の地政学の出来事が 14 日以内

⚠ 共起だけでは因果を言えない (2026-06-22 の決定)。``response`` 以外は「関連がありうる」止まり。
同じ国・同じ時期は偶然でも多く起きるため、当事国をランダムに入れ替えた対照との比 (lift) で
偶然を上回るかを測る (``permuted_baseline``)。
"""

from __future__ import annotations

import random
import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

KINDS: tuple[str, ...] = ("trigger", "dyad", "response", "intent")
TRIGGER_WINDOW = timedelta(days=14)
DYAD_WINDOW = timedelta(days=14)
INTENT_WINDOW = timedelta(days=14)
RESPONSE_WINDOW = timedelta(days=45)
SOCIO_POLITICAL_INTENTS = frozenset(
    {"coercion", "influence", "subversion", "territorial", "deterrence"}
)
_RESPONSE_HEADLINE = re.compile(
    r"制裁|起訴|名指し|帰属|公式に|sanction|indict|charge|attribut|accuse|blame",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CrossEvent:
    """橋に必要な事象の属性 (国は ISO 2 文字の大文字)。"""

    item_id: str
    first: datetime
    domain: str  # "cyber" | "geo"
    involved: frozenset[str] = frozenset()  # 地政学側の当事国
    actor_nations: frozenset[str] = frozenset()  # サイバー側: 国家系アクターの帰属国
    victim_countries: frozenset[str] = frozenset()
    intents: frozenset[str] = frozenset()
    headline: str = ""


@dataclass(frozen=True)
class CrossLine:
    kind: str
    geo: str
    cyber: str
    nations: tuple[str, ...]
    #: ``cyber.first - geo.first`` の日数 (正 = 地政学の出来事が先)
    gap_days: int


def _line(kind: str, geo: CrossEvent, cyber: CrossEvent, nations: frozenset[str]) -> CrossLine:
    gap = (cyber.first - geo.first).days
    return CrossLine(kind, geo.item_id, cyber.item_id, tuple(sorted(nations)), gap)


def derive_crossdomain(events: Sequence[CrossEvent]) -> list[CrossLine]:
    """サイバー事象ごとに、帰属国が当事国に入っている地政学事象を引いて 4 型を判定する。"""
    geo_by_nation: dict[str, list[CrossEvent]] = defaultdict(list)
    for e in events:
        if e.domain == "geo":
            for n in e.involved:
                geo_by_nation[n].append(e)
    out: list[CrossLine] = []
    for cyber in events:
        if cyber.domain != "cyber" or not cyber.actor_nations:
            continue
        seen: set[str] = set()
        for nation in cyber.actor_nations:
            for geo in geo_by_nation.get(nation, ()):
                if geo.item_id in seen:
                    continue
                seen.add(geo.item_id)
                out.extend(_judge(geo, cyber))
    return out


def _judge(geo: CrossEvent, cyber: CrossEvent) -> list[CrossLine]:
    shared = cyber.actor_nations & geo.involved
    delta = cyber.first - geo.first
    found: list[CrossLine] = []
    if timedelta(0) <= delta <= TRIGGER_WINDOW:
        found.append(_line("trigger", geo, cyber, shared))
    victims = (cyber.victim_countries & geo.involved) - cyber.actor_nations
    if victims and abs(delta) <= DYAD_WINDOW:
        found.append(_line("dyad", geo, cyber, shared | victims))
    if timedelta(0) < -delta <= RESPONSE_WINDOW and _RESPONSE_HEADLINE.search(geo.headline):
        found.append(_line("response", geo, cyber, shared))
    if cyber.intents & SOCIO_POLITICAL_INTENTS and abs(delta) <= INTENT_WINDOW:
        found.append(_line("intent", geo, cyber, shared))
    return found


def count_by_kind(lines: Sequence[CrossLine]) -> dict[str, int]:
    counts = dict.fromkeys(KINDS, 0)
    for line in lines:
        counts[line.kind] += 1
    return counts


def permuted_baseline(
    events: Sequence[CrossEvent], *, rounds: int = 20, seed: int = 0
) -> dict[str, float]:
    """地政学事象の当事国の集合を事象間でランダムに入れ替えた対照の、型ごとの平均本数。

    時期・見出し・サイバー側は据え置くので、「国が同じ」という結びつきだけが壊れる。
    実測の本数をこの値で割った比 (lift) が 1 に近ければ、線は偶然と区別できない。
    """
    rng = random.Random(seed)
    geo_idx = [i for i, e in enumerate(events) if e.domain == "geo"]
    totals = dict.fromkeys(KINDS, 0.0)
    for _ in range(rounds):
        shuffled = [events[i].involved for i in geo_idx]
        rng.shuffle(shuffled)
        trial = list(events)
        for i, inv in zip(geo_idx, shuffled, strict=True):
            trial[i] = replace(events[i], involved=inv)
        for kind, n in count_by_kind(derive_crossdomain(trial)).items():
            totals[kind] += n
    return {k: v / rounds for k, v in totals.items()}
