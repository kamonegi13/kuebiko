"""事象の焦点 (focus) の計算と照合 (docs/eventnews_focus_redesign.md §2・§3、2026-10-24 F0)。

焦点 = 錨 (seed 記事の名前、落とさない) + 育った名前 (支持記事の保有率 θ 以上) + 行為 (種別の過半)。
焦点を共有しない記事は「外れ」で焦点を動かさない。純粋関数 (DB / LLM 非依存) で、
記事の参加順の再生で決まる。DB の値はそのキャッシュにすぎず、いつでも作り直せる。

F0 では記録するだけで、統合・群化の判断には使わない。
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from src.eventnews.event_kind import ACTION_KINDS, DERIVATIVE_KIND, focus_kind_of
from src.eventnews.models import FOCAL_CVE_MAX

#: 規則の版 (θ・型・除外の規則を変えたら上げる。表は本番の版だけ持つ)
RULE_VERSION = "f0-v1"
#: 支持記事の保有率がこれ以上の名前が「育つ」
THETA = 0.5

ORIGIN_ANCHOR = "anchor"
ORIGIN_GROWN = "grown"

CLASS_SUPPORT = "support"
CLASS_WEAK = "weak"
CLASS_CONFLICT = "conflict"
CLASS_LINEAGE = "lineage"
#: 食い違いを見る名前の型 (別の CVE / 別の被害組織)
CONFLICT_TYPES: tuple[str, ...] = ("cve", "victim_org")

Name = tuple[str, str]


@dataclass(frozen=True)
class FocusRule:
    theta: float = THETA
    #: 名前が育つのに要る保有記事数の下限 (変種の比較用。既定は 1)
    min_growers: int = 1
    version: str = RULE_VERSION


DEFAULT_RULE = FocusRule()


@dataclass(frozen=True)
class FocusArticle:
    """焦点の計算に渡す記事 (結合信号の名前と種別だけ)。"""

    article_id: str
    names: frozenset[Name]
    kind: str = ""
    is_roundup: bool = False


@dataclass(frozen=True)
class FocusName:
    type: str
    value: str
    origin: str
    share: float


@dataclass(frozen=True)
class Focus:
    anchor_article: str
    names: tuple[FocusName, ...]
    kind: str
    supporters: int
    outsiders: int

    @property
    def name_set(self) -> frozenset[Name]:
        return frozenset((n.type, n.value) for n in self.names)

    @property
    def is_empty(self) -> bool:
        return not self.names

    def names_of(self, entity_type: str) -> frozenset[str]:
        return frozenset(n.value for n in self.names if n.type == entity_type)


def contributed_names(article: FocusArticle) -> frozenset[Name]:
    """記事が焦点に寄与する名前。一括勧告 (CVE が ``FOCAL_CVE_MAX`` 超) は CVE だけ除く。"""
    cve_count = sum(1 for t, _ in article.names if t == "cve")
    if cve_count > FOCAL_CVE_MAX:
        return frozenset(n for n in article.names if n[0] != "cve")
    return article.names


def _grown(
    counts: Counter[Name], supporters: int, anchor: frozenset[Name], rule: FocusRule
) -> set[Name]:
    if supporters == 0:
        return set()
    return {
        n
        for n, c in counts.items()
        if n not in anchor and c >= rule.min_growers and c / supporters >= rule.theta
    }


def _majority_kind(kinds: Sequence[str]) -> str:
    counted = [k for k in kinds if k in ACTION_KINDS]
    if not counted:
        return ""
    kind, n = Counter(counted).most_common(1)[0]
    return kind if n * 2 > len(counted) else ""


def compute_focus(articles: Sequence[FocusArticle], rule: FocusRule = DEFAULT_RULE) -> Focus:
    """参加順の記事列から焦点を作る (決定論)。まとめ記事は支持にも外れにも数えない。"""
    anchor: frozenset[Name] = frozenset()
    anchor_article = ""
    counts: Counter[Name] = Counter()
    support_kinds: list[str] = []
    supporters = outsiders = 0
    for art in articles:
        if art.is_roundup:
            continue
        names = contributed_names(art)
        if not anchor_article:
            if not names:
                outsiders += 1
                continue
            anchor, anchor_article = names, art.article_id
        elif not (names & (anchor | _grown(counts, supporters, anchor, rule))):
            outsiders += 1
            continue
        supporters += 1
        counts.update(names)
        support_kinds.append(focus_kind_of(art.kind))
    grown = _grown(counts, supporters, anchor, rule)
    out = tuple(
        FocusName(
            type=t,
            value=v,
            origin=ORIGIN_ANCHOR if (t, v) in anchor else ORIGIN_GROWN,
            share=round(counts[(t, v)] / supporters, 3),
        )
        for t, v in sorted(anchor | grown)
    )
    return Focus(anchor_article, out, _majority_kind(support_kinds), supporters, outsiders)


# ---------- 直列化 ----------


def focus_to_dict(focus: Focus) -> dict[str, Any]:
    return {
        "anchor_article": focus.anchor_article,
        "names": [
            {"type": n.type, "value": n.value, "origin": n.origin, "share": n.share}
            for n in focus.names
        ],
        "kind": focus.kind,
        "supporters": focus.supporters,
        "outsiders": focus.outsiders,
    }


def focus_from_dict(data: Mapping[str, Any]) -> Focus:
    return Focus(
        anchor_article=str(data.get("anchor_article", "")),
        names=tuple(
            FocusName(str(n["type"]), str(n["value"]), str(n["origin"]), float(n["share"]))
            for n in data.get("names", [])
        ),
        kind=str(data.get("kind", "")),
        supporters=int(data.get("supporters", 0)),
        outsiders=int(data.get("outsiders", 0)),
    )


def focus_to_json(focus: Focus) -> str:
    return json.dumps(focus_to_dict(focus), ensure_ascii=False, sort_keys=True)


def focus_from_json(text: str) -> Focus:
    return focus_from_dict(json.loads(text))


# ---------- 照合 (§3.1 の 4 値) ----------


@dataclass(frozen=True)
class FocusVerdict:
    klass: str
    #: 共有する名前 (型, 値)
    shared: tuple[Name, ...] = ()
    #: 食い違った型 (conflict のとき)
    conflicts: tuple[str, ...] = ()
    #: weak / lineage の理由 (empty / no_shared / actor_only /
    #: advisory_vs_exploitation / derivative)
    reason: str = ""


def _conflicting_types(a: Focus, b: Focus) -> tuple[str, ...]:
    out = []
    for t in CONFLICT_TYPES:
        sa, sb = a.names_of(t), b.names_of(t)
        if sa and sb and not (sa & sb):
            out.append(t)
    return tuple(out)


def _lineage_reason(a: Focus, b: Focus, shared: frozenset[Name]) -> str:
    kinds = {a.kind, b.kind}
    if len(kinds) == 2 and kinds == {"advisory", "exploitation"}:
        return "advisory_vs_exploitation" if any(t == "cve" for t, _ in shared) else ""
    if DERIVATIVE_KIND in kinds and len(kinds - {""}) == 2:
        return "derivative"
    return ""


def match_focus(a: Focus, b: Focus) -> FocusVerdict:
    """焦点どうしの照合。**止める (conflict / lineage) のは食い違いが明示的なときだけ**。

    材料が足りない組 (片側が空・アクター名だけの共有・共有なし) は weak として後段の判定器へ回す。
    """
    conflicts = _conflicting_types(a, b)
    if conflicts:
        return FocusVerdict(CLASS_CONFLICT, conflicts=conflicts)
    shared = a.name_set & b.name_set
    shared_t = tuple(sorted(shared))
    lineage = _lineage_reason(a, b, shared) if shared else ""
    if lineage:
        return FocusVerdict(CLASS_LINEAGE, shared_t, reason=lineage)
    if a.is_empty or b.is_empty:
        return FocusVerdict(CLASS_WEAK, reason="empty")
    if not shared:
        return FocusVerdict(CLASS_WEAK, reason="no_shared")
    if all(t == "actor" for t, _ in shared):
        return FocusVerdict(CLASS_WEAK, shared_t, reason="actor_only")
    return FocusVerdict(CLASS_SUPPORT, shared_t)


def verdict_to_dict(verdict: FocusVerdict) -> dict[str, Any]:
    return {
        "class": verdict.klass,
        "shared": [list(n) for n in verdict.shared],
        "conflicts": list(verdict.conflicts),
        "reason": verdict.reason,
    }
