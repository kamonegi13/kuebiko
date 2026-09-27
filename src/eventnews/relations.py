"""事象どうしの関係を、事象 → 指標の線から導く (2026-09-27)。

設計: docs/research/event_knowledge_graph.md §11 / §12 / §16。
⭐ 関係は **表に書かない**。事象ごとの指標の集合 (主題アクター・マルウェア・ツール・焦点の CVE・
被害組織・業種・国・種別・時期) から、その都度計算する。指標とずれる表を持たず、定義を変えれば
過去にも一様に効く。

関係の種類 (§11 の規則の初版。精度は種類ごとに盲検で測ってから使う):
- ``follow_up`` 続報: 同じ被害組織 + 前の事象の終わりから 7 日以上あとに始まる
- ``side`` 側面: 同じ焦点 CVE か被害組織 + 事象の種別が違う (勧告と悪用 等)。種別が分かる事象だけ
- ``campaign`` 同一キャンペーン: 同じ主題アクター + 珍しい能力 (マルウェア・ツール・CVE) を共有 +
  被害が別 + 60 日以内。帰属の無いものは、とても珍しい能力 + 同じ業種か国 + 14 日以内
  (主題アクターが食い違えば結ばない)
- ``supplier`` 共通の供給元: 同じ帰属国の **別** アクターが珍しいマルウェア・ツールを共有
  (部隊と上位組織は別アクターと数えない)
- ``contains`` 包含: まとめの事象が、個別の事象と焦点 CVE か被害組織を共有

珍しさ = その指標を持つ事象の数 (df)。汎用の道具・頻出の国は線にしない (§12)。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

#: 珍しい指標の上限 (これ以下の事象にしか現れない)。盲検の結果で調整する
RARE_DF = 8
#: とても珍しい (帰属の無いキャンペーンの根拠にできる) 上限
VERY_RARE_DF = 3
#: 続報とみなす間隔
FOLLOW_UP_GAP = timedelta(days=7)
#: キャンペーンの時間窓 (帰属あり / なし)
CAMPAIGN_WINDOW = timedelta(days=60)
UNATTRIBUTED_WINDOW = timedelta(days=14)

#: 共通の供給元を導くか (盲検の初回 0/4 のため既定 off)
SUPPLIER_ENABLED = False

RELATION_TYPES: tuple[str, ...] = ("follow_up", "side", "campaign", "supplier", "contains")
RELATION_LABELS: dict[str, str] = {
    "follow_up": "続報",
    "side": "側面",
    "campaign": "同一キャンペーン",
    "supplier": "共通の供給元",
    "contains": "包含",
}


@dataclass(frozen=True)
class EventFeatures:
    """1 事象の指標の集合 (構成記事の和集合)。"""

    item_id: str
    first: datetime
    last: datetime
    subjects: frozenset[str] = frozenset()  # 信頼できる経路の主題アクター (group のみ)
    malware: frozenset[str] = frozenset()
    tools: frozenset[str] = frozenset()
    cves: frozenset[str] = frozenset()  # 焦点の CVE (CVE が 3 件以下の記事のもの)
    victims: frozenset[str] = frozenset()  # 正規化済みの被害組織
    sectors: frozenset[str] = frozenset()
    countries: frozenset[str] = frozenset()
    kinds: frozenset[str] = frozenset()  # 事象の種別 (記事の種別の和集合、分からなければ空)
    roundup: bool = False


@dataclass(frozen=True)
class DerivedRelation:
    """導いた関係 1 本 (``a`` は時期が先の事象)。"""

    a: str
    b: str
    rel_type: str
    basis: tuple[str, ...]  # 根拠の指標 ("victim:…" 等)
    extra: Mapping[str, str] = field(default_factory=dict)


def _df(events: Iterable[EventFeatures], attr: str) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for e in events:
        for v in getattr(e, attr):
            out[v] += 1
    return out


@dataclass(frozen=True)
class _Stats:
    df: Mapping[str, Mapping[str, int]]

    def rare(self, attr: str, values: Iterable[str], limit: int = RARE_DF) -> set[str]:
        table = self.df[attr]
        return {v for v in values if table.get(v, 0) <= limit}


_INDEXED = ("subjects", "malware", "tools", "cves", "victims")


def _candidate_pairs(events: list[EventFeatures], stats: _Stats) -> set[tuple[int, int]]:
    """珍しい指標を 1 つでも共有する事象の組 (添字)。"""
    index: dict[tuple[str, str], list[int]] = defaultdict(list)
    for i, e in enumerate(events):
        for attr in _INDEXED:
            # 主題アクターは珍しくなくても候補にする (同じアクターの別事象がキャンペーンの母集団)
            vals = getattr(e, attr) if attr == "subjects" else stats.rare(attr, getattr(e, attr))
            for v in vals:
                index[(attr, v)].append(i)
    pairs: set[tuple[int, int]] = set()
    for members in index.values():
        if (
            len(members) > 200
        ):  # 頻出の主題アクター等は組を作りすぎる — 珍しさで絞れないものは使わない
            continue
        for x in range(len(members)):
            for y in range(x + 1, len(members)):
                pairs.add((members[x], members[y]))
    return pairs


def _ordered(a: EventFeatures, b: EventFeatures) -> tuple[EventFeatures, EventFeatures]:
    return (a, b) if a.first <= b.first else (b, a)


def classify_pair(
    a: EventFeatures,
    b: EventFeatures,
    stats: _Stats,
    *,
    nation_of: Callable[[str], str | None],
    related_actors: Callable[[str, str], bool],
) -> DerivedRelation | None:
    """2 事象の関係 (規則の優先順: 包含 > 続報 > 側面 > キャンペーン > 供給元)。無ければ None。"""
    a, b = _ordered(a, b)
    shared_victims = stats.rare("victims", a.victims & b.victims)
    shared_cves = stats.rare("cves", a.cves & b.cves)
    if a.roundup != b.roundup and (shared_victims or shared_cves):
        outer, inner = (a, b) if a.roundup else (b, a)
        basis = tuple(
            sorted({f"victim:{v}" for v in shared_victims} | {f"cve:{c}" for c in shared_cves})
        )
        return DerivedRelation(outer.item_id, inner.item_id, "contains", basis)
    if a.roundup or b.roundup:
        return None
    if shared_victims and b.first - a.last >= FOLLOW_UP_GAP:
        return DerivedRelation(
            a.item_id, b.item_id, "follow_up", tuple(f"victim:{v}" for v in sorted(shared_victims))
        )
    if (shared_victims or shared_cves) and a.kinds and b.kinds and not (a.kinds & b.kinds):
        basis = tuple(
            sorted({f"victim:{v}" for v in shared_victims} | {f"cve:{c}" for c in shared_cves})
        )
        return DerivedRelation(a.item_id, b.item_id, "side", basis)
    capability = (
        stats.rare("malware", a.malware & b.malware)
        | stats.rare("tools", a.tools & b.tools)
        | shared_cves
    )
    gap = b.first - a.last
    shared_subjects = a.subjects & b.subjects
    victims_differ = not (a.victims & b.victims)
    if shared_subjects and capability and victims_differ and gap <= CAMPAIGN_WINDOW:
        basis = tuple(
            sorted({f"actor:{s}" for s in shared_subjects} | {f"cap:{c}" for c in capability})
        )
        return DerivedRelation(a.item_id, b.item_id, "campaign", basis)
    conflict = bool(a.subjects and b.subjects and not shared_subjects)
    very_rare = (
        stats.rare("malware", a.malware & b.malware, VERY_RARE_DF)
        | stats.rare("tools", a.tools & b.tools, VERY_RARE_DF)
        | stats.rare("cves", a.cves & b.cves, VERY_RARE_DF)
    )
    same_target = (a.sectors & b.sectors) or (a.countries & b.countries)
    if very_rare and same_target and not conflict and victims_differ and gap <= UNATTRIBUTED_WINDOW:
        basis = tuple(sorted(f"cap:{c}" for c in very_rare))
        return DerivedRelation(a.item_id, b.item_id, "campaign", basis, {"attributed": "no"})
    malware_tools = stats.rare("malware", a.malware & b.malware) | stats.rare(
        "tools", a.tools & b.tools
    )
    # 共通の供給元は盲検の初回で 4 組すべて無関係 (2026-09-27) — 指標の質が上がるまで導かない
    if SUPPLIER_ENABLED and conflict and malware_tools:
        pairs = [
            (x, y)
            for x in a.subjects
            for y in b.subjects
            if x != y and nation_of(x) and nation_of(x) == nation_of(y) and not related_actors(x, y)
        ]
        if pairs:
            x, y = pairs[0]
            basis = tuple(
                sorted({f"cap:{c}" for c in malware_tools} | {f"actor:{x}", f"actor:{y}"})
            )
            return DerivedRelation(
                a.item_id, b.item_id, "supplier", basis, {"nation": nation_of(x) or ""}
            )
    return None


def derive_relations(
    events: list[EventFeatures],
    *,
    nation_of: Callable[[str], str | None],
    related_actors: Callable[[str, str], bool],
) -> list[DerivedRelation]:
    """事象の集合から関係を導く (純粋関数)。"""
    stats = _Stats({attr: _df(events, attr) for attr in _INDEXED})
    out: list[DerivedRelation] = []
    for i, j in sorted(_candidate_pairs(events, stats)):
        rel = classify_pair(
            events[i], events[j], stats, nation_of=nation_of, related_actors=related_actors
        )
        if rel is not None:
            out.append(rel)
    return out


#: 画面・総括に出す種類 (盲検の精度が出たものだけ、2026-09-27)。空なら何も出さない
ENABLED_TYPES: frozenset[str] = frozenset()
#: 導いた関係のキャッシュの寿命 (秒)。事象は毎時更新されるので、それより短く
_CACHE_TTL_SECONDS = 1800.0
_cache: dict[int, tuple[float, dict[str, list[DerivedRelation]]]] = {}


def relations_by_event(repo: object, *, days: int = 60) -> dict[str, list[DerivedRelation]]:
    """事象 id → その事象が関わる関係 (キャッシュつき)。``ENABLED_TYPES`` の種類だけ。"""
    import time

    from src.eventnews.relation_features import actor_helpers, load_event_features

    now = time.monotonic()
    hit = _cache.get(days)
    if hit is not None and now - hit[0] < _CACHE_TTL_SECONDS:
        return hit[1]
    nation_of, related = actor_helpers()
    rels = derive_relations(
        load_event_features(repo, days=days),  # type: ignore[arg-type]
        nation_of=nation_of,
        related_actors=related,
    )
    index: dict[str, list[DerivedRelation]] = defaultdict(list)
    for r in rels:
        if r.rel_type in ENABLED_TYPES:
            index[r.a].append(r)
            index[r.b].append(r)
    _cache[days] = (now, dict(index))
    return _cache[days][1]
