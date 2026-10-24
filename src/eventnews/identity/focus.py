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

from src.cti.org_normalizer import org_names_compatible
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
#: 照合の規則の版 (F1b: 被害組織の緩い一致・別名、CVE の信頼条件)
MATCH_RULE_VERSION = "f1b-v1"

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

    @property
    def article_count(self) -> int:
        """焦点の計算に数えた記事数 (支持 + 外れ。まとめ記事は含まない)。"""
        return self.supporters + self.outsiders


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


def trusted_names(focus: Focus, entity_type: str) -> frozenset[str]:
    """事象の記事の**過半**が持つ名前だけ (F1b)。

    焦点の名前は支持記事の保有率で育つが、外れの多い事象 (複数 CVE の続報を束ねたハブ) では
    錨の CVE を持つ記事が全体の数 % しかなく、その CVE が事象を代表しない。食い違いを
    止める根拠にしてよいのは、事象の記事の過半で支持される名前だけ。
    """
    total = focus.article_count
    if total == 0:
        return frozenset()
    return frozenset(
        n.value
        for n in focus.names
        if n.type == entity_type and n.share * focus.supporters * 2 > total
    )


def trusted_cves(focus: Focus) -> frozenset[str]:
    return trusted_names(focus, "cve")


def _org_names_clash(
    a: Focus, b: Focus, alias: Mapping[str, str], *, victim_trust: bool = False
) -> bool:
    """両側が被害組織を名指しし、どの組も同じ組織に読めない (表記ゆれ・別名を除いて食い違う)。"""
    if victim_trust:
        names_a, names_b = trusted_names(a, "victim_org"), trusted_names(b, "victim_org")
    else:
        names_a, names_b = a.names_of("victim_org"), b.names_of("victim_org")
    if not names_a or not names_b:
        return False
    for x in names_a:
        for y in names_b:
            if alias.get(x, x) == alias.get(y, y) or org_names_compatible(x, y):
                return False
    return True


def _conflicting_types(
    a: Focus,
    b: Focus,
    org_alias: Mapping[str, str] | None = None,
    *,
    victim_trust: bool = False,
) -> tuple[str, ...]:
    out = []
    cves_a, cves_b = trusted_cves(a), trusted_cves(b)
    if cves_a and cves_b and not (cves_a & cves_b):
        out.append("cve")
    if _org_names_clash(a, b, org_alias or {}, victim_trust=victim_trust):
        out.append("victim_org")
    return tuple(out)


def _lineage_reason(a: Focus, b: Focus, shared: frozenset[Name]) -> str:
    kinds = {a.kind, b.kind}
    if len(kinds) == 2 and kinds == {"advisory", "exploitation"}:
        return "advisory_vs_exploitation" if any(t == "cve" for t, _ in shared) else ""
    if DERIVATIVE_KIND in kinds and len(kinds - {""}) == 2:
        return "derivative"
    return ""


def match_focus(
    a: Focus,
    b: Focus,
    *,
    org_alias: Mapping[str, str] | None = None,
    victim_trust: bool = False,
) -> FocusVerdict:
    """焦点どうしの照合。**止める (conflict) のは食い違いが明示的なときだけ**。

    材料が足りない組 (片側が空・アクター名だけの共有・共有なし) は weak として後段の判定器へ回す。
    lineage は F2 では止めず関係 (系統) の候補として渡す (F1b、``RELATION_CANDIDATE_CLASSES``)。

    ``org_alias`` (照合キー → 代表キー、別名判定の「同一」) は被害組織の conflict を**消す**
    方向にだけ使う。共有名には数えない (= 別名で結合は増えない)。
    ``victim_trust`` は被害組織にも CVE と同じ「事象の過半で支持」の条件を足す変種 (既定 off)。
    """
    conflicts = _conflicting_types(a, b, org_alias, victim_trust=victim_trust)
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
