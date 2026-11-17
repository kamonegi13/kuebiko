"""状況総括の「横断のパターン」まとめ (決定論・純粋関数)。

設計: docs/synthesis_pattern_redesign.md §3.2 / §3.3。事案の部品は ``pattern_case``。

1. 根拠記事の事象の根を共有する判定は同じ事案 (``PatternCase``) に畳む。
2. 事案を 地域 (日本 / 海外) × 種別 (侵害・漏えい / 悪用・攻撃活動) でまとめ、2 件以上でパターン。
3. パターンに印 (件数・S3・内訳・本文で裏付けられた「共通の手口」) を付ける。事案はパターン内で
   重い順 (``PatternCase.weight``、同点は salience 順)。
4. 属性が無い・1 件しかないキーの事案は「そのほかの動き」。ただし重い単独事案
   (``NOTABLE_MIN_WEIGHT`` 以上 = S3 か大規模) は「注目の単独事案」(最大 ``NOTABLE_MAX`` 件) へ。
5. 見出しの主語 (パターンの事案と注目の単独事案のうち最も重い 1 件) をコードが指名する。
   2 番目に重い S3 の注目の単独事案は、見出しに無ければ決定論で後ろに添える (``headline_extra``)。
6. 継続中 (動いていない) の判定は全件を 1 行 (題・確度・最後に動いた日) の材料にする。
7. 事案どうしのつながりは、共通の実体 (被害組織・マルウェア・キャンペーン・アクター・CVE。
   一般語・国名は除く) を持つ組だけを決定論で出す (``RELATIONS_MAX`` 件まで)。
   事象の根を共有する判定は事案に畳み済み。
パターンの重さ = 件数 + 事案の重さの合計。日本を先に。**記事の件数・言及量は使わない** (§7)。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from itertools import combinations
from zoneinfo import ZoneInfo

from src.assessment.name_relevance import entity_names
from src.assessment.salience import rank_judgments
from src.storage.repo_judgment_attrs import LINK_ENTITY_TYPES, JudgmentAttrs
from src.synthesis.grounded.estimate import KeyJudgment
from src.synthesis.grounded.pattern_case import (
    PatternCase,
    best_event_headline,
    first_full_sentence,
    make_case,
)

KIND_BREACH = "breach"
KIND_EXPLOITATION = "exploitation"
#: 種別キーの表示名。ここに無い種別 (advisory / other 等) はパターンの軸に使わない
KIND_LABEL: Mapping[str, str] = {
    KIND_BREACH: "侵害・情報漏えい",
    KIND_EXPLOITATION: "脆弱性の悪用・攻撃活動",
}
REGION_JAPAN = "日本"
REGION_OVERSEAS = "海外"
#: パターンになる最小の事案数
MIN_PATTERN_CASES = 2
#: 注目の単独事案の上限と、なれる最小の重さ (S3 = 2、大規模 + 万の位の件数 = 2)
NOTABLE_MAX = 2
NOTABLE_MIN_WEIGHT = 2
#: 「共通の手口」の印: 2 事案以上で共有される手法・製品だけ。パターンあたりの上限
COMMON_MARK_MIN_CASES = 2
COMMON_MARKS_MAX = 3
#: 継続中の評価のうち、注視点のためにプロンプトへ渡す上位の件数
STANDING_MAX = 4
#: 表示する事案どうしのつながりの上限
RELATIONS_MAX = 3
NO_CHANGE_TYPES = frozenset({"", "no_change"})
_JST = ZoneInfo("Asia/Tokyo")


@dataclass(frozen=True)
class CommonMark:
    """パターン内で 2 事案以上が共有する手口の手がかり。"""

    kind: str  # "technique" | "product"
    key: str
    label: str
    cases: int


@dataclass(frozen=True)
class Pattern:
    pattern_id: str
    region: str
    kind: str
    cases: tuple[PatternCase, ...]
    common_marks: tuple[CommonMark, ...] = ()

    @property
    def kind_label(self) -> str:
        return KIND_LABEL[self.kind]

    @property
    def count(self) -> int:
        return len(self.cases)

    @property
    def method_words(self) -> tuple[str, ...]:
        """「共通の手口」の関門が照合する語 (手法の ID・名前・製品名)。"""
        words = [w for m in self.common_marks for w in (m.key, m.label)]
        return tuple(dict.fromkeys(w for w in words if w))

    @property
    def s3_count(self) -> int:
        return sum(c.is_s3 for c in self.cases)

    @property
    def weight(self) -> int:
        return self.count + sum(c.weight for c in self.cases)

    @property
    def delta_counts(self) -> tuple[tuple[str, int], ...]:
        return tuple(Counter(c.delta_type for c in self.cases).most_common())

    @property
    def confidence_counts(self) -> tuple[tuple[str, int], ...]:
        return tuple(Counter(c.confidence for c in self.cases).most_common())


@dataclass(frozen=True)
class NotableCase:
    """注目の単独事案 (パターンに入らない S3 の事案)。``item_id`` は S1, S2。"""

    item_id: str
    case: PatternCase


@dataclass(frozen=True)
class CaseLink:
    """共通の実体を持つ 2 事案。``shared`` は実体の値 (種類の接頭辞を除いた表示用)。"""

    a: PatternCase
    b: PatternCase
    shared: tuple[str, ...]


@dataclass(frozen=True)
class StandingItem:
    judgment_id: str
    title: str
    confidence: str
    #: 最後に根拠が足された日 (JST、YYYY-MM-DD)。台帳に無ければ空
    last_moved: str = ""


@dataclass(frozen=True)
class PatternPlan:
    patterns: tuple[Pattern, ...]
    rest: tuple[PatternCase, ...]
    notable: tuple[NotableCase, ...]
    #: 注視点のためにプロンプトへ渡す継続中の評価の上位 (``STANDING_MAX`` 件)
    standing: tuple[KeyJudgment, ...]
    moved_count: int
    #: パターンが無い日の最後の手段に使う、最も重い動いた判定 (salience 順)
    top_judgment: KeyJudgment | None = None
    #: 継続中 (動いていない) の判定の全件。1 行 = 題・確度・最後に動いた日
    standing_items: tuple[StandingItem, ...] = ()
    #: 見出しで必ず触れる事案 (最も重い 1 件。無ければ空)
    headline_subjects: tuple[PatternCase, ...] = ()
    #: 見出しに名前が無ければ決定論で「注目: …」と添える、2 番目に重い S3 の注目の単独事案
    headline_extra: PatternCase | None = None
    #: 共通の実体を持つ事案の組 (決定論、``RELATIONS_MAX`` 件まで)
    links: tuple[CaseLink, ...] = ()

    def all_cases(self) -> tuple[PatternCase, ...]:
        """C 番号の順 (パターン → 注目 → そのほか) の全事案。"""
        return (
            *(c for p in self.patterns for c in p.cases),
            *(n.case for n in self.notable),
            *self.rest,
        )


def _root_groups(
    moved: Sequence[KeyJudgment], attrs: Mapping[str, JudgmentAttrs]
) -> list[list[KeyJudgment]]:
    """事象の根を共有する判定を同じ群にする (union-find)。順序は ``moved`` の先頭の出現順。"""
    parent = list(range(len(moved)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    owner: dict[str, int] = {}
    for i, j in enumerate(moved):
        for root in attrs.get(j.id, JudgmentAttrs()).event_roots:
            if root in owner:
                parent[find(i)] = find(owner[root])
            else:
                owner[root] = i
    groups: dict[int, list[KeyJudgment]] = {}
    for i, j in enumerate(moved):
        groups.setdefault(find(i), []).append(j)
    return list(groups.values())


def pattern_kind(kinds: frozenset[str]) -> str | None:
    """事案の種別キー。侵害を優先し、無ければ悪用。どちらも無ければ None (パターンの軸にしない)。"""
    if KIND_BREACH in kinds:
        return KIND_BREACH
    return KIND_EXPLOITATION if KIND_EXPLOITATION in kinds else None


def _heaviest_first(cases: Sequence[PatternCase]) -> list[PatternCase]:
    return sorted(cases, key=lambda c: (-c.weight, c.rank))


def _common_marks(
    cases: Sequence[PatternCase], technique_names: Mapping[str, str]
) -> tuple[CommonMark, ...]:
    """2 事案以上が共有する手法 (本文の裏付けを通した ATT&CK) と製品。共有が多い順に上限まで。"""
    marks: list[CommonMark] = []
    for kind, values in (
        ("technique", [t for c in cases for t in c.techniques]),
        ("product", [x for c in cases for x in c.products]),
    ):
        for key, n in Counter(values).items():
            if n < COMMON_MARK_MIN_CASES:
                continue
            name = technique_names.get(key, "") if kind == "technique" else ""
            marks.append(CommonMark(kind, key, f"{key} {name}".strip(), n))
    marks.sort(key=lambda m: (-m.cases, m.kind, m.key))
    return tuple(marks[:COMMON_MARKS_MAX])


def _split_notable(
    rest: Sequence[PatternCase],
) -> tuple[list[PatternCase], tuple[NotableCase, ...]]:
    """そのほかの動きのうち重い事案を、重い順に最大 ``NOTABLE_MAX`` 件「注目」へ移す。"""
    picked = [c for c in _heaviest_first(rest) if c.weight >= NOTABLE_MIN_WEIGHT][:NOTABLE_MAX]
    notable = tuple(NotableCase(f"S{n}", c) for n, c in enumerate(picked, start=1))
    return sorted((c for c in rest if c not in picked), key=lambda c: c.rank), notable


def _jst_date(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone(_JST).date().isoformat()
    except ValueError:
        return ""


def _standing_items(
    judgments: Sequence[KeyJudgment], attrs: Mapping[str, JudgmentAttrs]
) -> tuple[StandingItem, ...]:
    items = []
    for j in judgments:
        a = attrs.get(j.id, JudgmentAttrs())
        title = best_event_headline(a.event_headlines) or first_full_sentence(j.claim)
        items.append(StandingItem(j.id, title, j.confidence, _jst_date(a.last_evidence_at)))
    return tuple(items)


def _number_cases(
    drafts: Sequence[tuple[str, str, Sequence[PatternCase]]],
    notable: tuple[NotableCase, ...],
    rest: Sequence[PatternCase],
) -> tuple[list[list[PatternCase]], tuple[NotableCase, ...], list[PatternCase]]:
    """C1… の id を、パターン → 注目 → そのほかの順に振る。"""
    n = 0

    def tag(c: PatternCase) -> PatternCase:
        nonlocal n
        n += 1
        return replace(c, case_id=f"C{n}")

    return (
        [[tag(c) for c in cs] for _, _, cs in drafts],
        tuple(NotableCase(x.item_id, tag(x.case)) for x in notable),
        [tag(c) for c in rest],
    )


def build_patterns(
    judgments: Sequence[KeyJudgment],
    attrs: Mapping[str, JudgmentAttrs],
    technique_names: Mapping[str, str] | None = None,
) -> PatternPlan:
    """期間の判定と属性から ``PatternPlan`` を組む。判定の順序・属性の欠けに依らず決定論。"""
    ranked = rank_judgments(tuple(judgments))
    moved = [j for j in ranked if j.delta_type not in NO_CHANGE_TYPES]
    still = [j for j in ranked if j.delta_type in NO_CHANGE_TYPES]
    keyed: dict[tuple[str, str], list[PatternCase]] = {}
    rest: list[PatternCase] = []
    order = {j.id: i for i, j in enumerate(moved)}
    for members in _root_groups(moved, attrs):
        case, kind = make_case(members, attrs, order[members[0].id])
        if kind is None:
            rest.append(case)
            continue
        region = REGION_JAPAN if case.is_japan else REGION_OVERSEAS
        keyed.setdefault((region, kind), []).append(case)
    drafts: list[tuple[str, str, list[PatternCase]]] = []
    for (region, kind), cases in keyed.items():
        if len(cases) >= MIN_PATTERN_CASES:
            drafts.append((region, kind, _heaviest_first(cases)))
        else:
            rest.extend(cases)
    rest, notable = _split_notable(rest)
    drafts.sort(key=lambda d: (d[0] != REGION_JAPAN, -(len(d[2]) + sum(c.weight for c in d[2]))))
    numbered, notable, rest = _number_cases(drafts, notable, rest)
    names = technique_names or {}
    patterns = tuple(
        Pattern(f"P{i}", r, k, tuple(cs), _common_marks(cs, names))
        for i, ((r, k, _), cs) in enumerate(zip(drafts, numbered, strict=True), start=1)
    )
    heavy = [*(c for p in patterns for c in p.cases), *(n.case for n in notable)]
    subject = max(heavy, key=lambda c: (c.weight, -c.rank), default=None)
    extra = next((n.case for n in notable if n.case.is_s3 and n.case != subject), None)
    return PatternPlan(
        patterns=patterns,
        rest=tuple(rest),
        notable=notable,
        standing=tuple(still[:STANDING_MAX]),
        moved_count=len(moved),
        top_judgment=moved[0] if moved else (ranked[0] if ranked else None),
        standing_items=_standing_items(still, attrs),
        headline_subjects=(subject,) if subject else (),
        headline_extra=extra,
        links=_case_links([*heavy, *rest]),
    )


def _link_value(key: str) -> str:
    return key.split(":", 1)[1]


def _case_links(cases: Sequence[PatternCase]) -> tuple[CaseLink, ...]:
    """共通の実体の数が多い組から ``RELATIONS_MAX`` 件 (同数は salience 順)。"""
    found = []
    for a, b in combinations(sorted(cases, key=lambda c: c.rank), 2):
        shared = sorted(
            _link_value(k)
            for k in a.links & b.links
            if k.split(":", 1)[0] in LINK_ENTITY_TYPES and entity_names([k])
        )
        if shared:
            found.append(CaseLink(a, b, tuple(shared)))
    found.sort(key=lambda link: -len(link.shared))  # 安定ソート: 同数は salience 順のまま
    return tuple(found[:RELATIONS_MAX])
