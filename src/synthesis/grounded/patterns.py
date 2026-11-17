"""状況総括の「横断のパターン」まとめ (決定論・純粋関数)。

設計: docs/synthesis_pattern_redesign.md §3.2 / §3.3。

1. 根拠記事の事象の根を共有する判定は同じ事案 (``PatternCase``) に畳む。
2. 事案を 地域 (日本 / 海外) × 種別 (侵害・漏えい / 悪用・攻撃活動) でまとめ、2 件以上でパターン。
3. パターンに印 (件数・S3・新規/拡大などの内訳・確度の内訳) を付ける。
4. 属性が無い・1 件しかないキーの事案は「そのほかの動き」。ただし S3 の単独事案は
   「注目の単独事案」(最大 ``NOTABLE_MAX`` 件) として前に出す。
重さは ``2 × S3 件数 + 件数`` で日本を先に。**記事の件数・言及量は使わない** (CLAUDE.md §7)。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from src.assessment.salience import rank_judgments
from src.storage.repo_judgment_attrs import JudgmentAttrs
from src.synthesis.grounded.estimate import KeyJudgment

KIND_BREACH = "breach"
KIND_EXPLOITATION = "exploitation"
#: 種別キーの表示名。ここに無い種別 (advisory / other 等) はパターンの軸に使わない
KIND_LABEL: Mapping[str, str] = {
    KIND_BREACH: "侵害・情報漏えい",
    KIND_EXPLOITATION: "脆弱性の悪用・攻撃活動",
}
REGION_JAPAN = "日本"
REGION_OVERSEAS = "海外"
JAPAN_ISO = "JP"
SEVERITY_S3 = "S3"
#: パターンになる最小の事案数
MIN_PATTERN_CASES = 2
#: 重さ = S3_WEIGHT × S3 件数 + 件数
S3_WEIGHT = 2
#: 事案の短い題の最大字数
TITLE_MAX_CHARS = 60
#: 題は第 1 文のうち、この字数以降の最初の「、」まで (短い節で切れて意味が消えるのを避ける)
TITLE_MIN_CHARS = 25
TITLE_ELLIPSIS = "…"
#: 注目の単独事案の上限
NOTABLE_MAX = 2
#: 継続中の評価に載せる件数 (上位)
STANDING_MAX = 4
NO_CHANGE_TYPES = frozenset({"", "no_change"})


def short_title(claim: str) -> str:
    """claim の第 1 文を、``TITLE_MIN_CHARS`` 字以降の最初の「、」までにし、上限で「…」。"""
    cut = claim.find("。")
    sentence = (claim if cut < 0 else claim[:cut]).strip()
    comma = sentence.find("、", TITLE_MIN_CHARS)
    if comma >= 0:
        sentence = sentence[:comma]
    if len(sentence) > TITLE_MAX_CHARS:
        return sentence[: TITLE_MAX_CHARS - len(TITLE_ELLIPSIS)] + TITLE_ELLIPSIS
    return sentence


@dataclass(frozen=True)
class PatternCase:
    """同じ事象に属する判定を畳んだ 1 事案。"""

    judgment_ids: tuple[str, ...]
    title: str
    claims: tuple[str, ...]
    delta_type: str
    confidence: str
    is_s3: bool
    is_japan: bool
    #: salience 順での位置 (小さいほど重い)
    rank: int = 0


@dataclass(frozen=True)
class Pattern:
    pattern_id: str
    region: str
    kind: str
    cases: tuple[PatternCase, ...]

    @property
    def kind_label(self) -> str:
        return KIND_LABEL[self.kind]

    @property
    def count(self) -> int:
        return len(self.cases)

    @property
    def s3_count(self) -> int:
        return sum(c.is_s3 for c in self.cases)

    @property
    def weight(self) -> int:
        return S3_WEIGHT * self.s3_count + self.count

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
class PatternPlan:
    patterns: tuple[Pattern, ...]
    rest: tuple[PatternCase, ...]
    notable: tuple[NotableCase, ...]
    standing: tuple[KeyJudgment, ...]
    moved_count: int
    #: パターンが無い日の最後の手段に使う、最も重い動いた判定 (規模の項を足さない salience 順)
    top_judgment: KeyJudgment | None = None


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


def _merged_attrs(
    members: Sequence[KeyJudgment], attrs: Mapping[str, JudgmentAttrs]
) -> JudgmentAttrs:
    parts = [attrs.get(j.id, JudgmentAttrs()) for j in members]
    return JudgmentAttrs(
        countries=frozenset().union(*(p.countries for p in parts)),
        kinds=frozenset().union(*(p.kinds for p in parts)),
        severities=frozenset().union(*(p.severities for p in parts)),
        event_roots=frozenset().union(*(p.event_roots for p in parts)),
    )


def pattern_kind(kinds: frozenset[str]) -> str | None:
    """事案の種別キー。侵害を優先し、無ければ悪用。どちらも無ければ None (パターンの軸にしない)。"""
    if KIND_BREACH in kinds:
        return KIND_BREACH
    return KIND_EXPLOITATION if KIND_EXPLOITATION in kinds else None


def _make_case(
    members: Sequence[KeyJudgment], attrs: Mapping[str, JudgmentAttrs], rank: int
) -> tuple[PatternCase, str | None]:
    rep = members[0]  # ranked 順の先頭 = 最も重い判定
    merged = _merged_attrs(members, attrs)
    is_japan = JAPAN_ISO in merged.countries or any(j.japan_related for j in members)
    case = PatternCase(
        judgment_ids=tuple(j.id for j in members),
        title=short_title(rep.claim),
        claims=tuple(j.claim for j in members),
        delta_type=rep.delta_type,
        confidence=rep.confidence,
        is_s3=SEVERITY_S3 in merged.severities,
        is_japan=is_japan,
        rank=rank,
    )
    return case, pattern_kind(merged.kinds)


def build_patterns(
    judgments: Sequence[KeyJudgment], attrs: Mapping[str, JudgmentAttrs]
) -> PatternPlan:
    """期間の判定と属性から ``PatternPlan`` を組む。判定の順序・属性の欠けに依らず決定論。"""
    ranked = rank_judgments(tuple(judgments))
    moved = [j for j in ranked if j.delta_type not in NO_CHANGE_TYPES]
    standing = tuple(j for j in ranked if j.delta_type in NO_CHANGE_TYPES)[:STANDING_MAX]
    keyed: dict[tuple[str, str], list[PatternCase]] = {}
    rest: list[PatternCase] = []
    order = {j.id: i for i, j in enumerate(moved)}
    for members in _root_groups(moved, attrs):
        case, kind = _make_case(members, attrs, order[members[0].id])
        if kind is None:
            rest.append(case)
            continue
        region = REGION_JAPAN if case.is_japan else REGION_OVERSEAS
        keyed.setdefault((region, kind), []).append(case)
    drafts: list[tuple[str, str, list[PatternCase]]] = []
    for (region, kind), cases in keyed.items():
        if len(cases) >= MIN_PATTERN_CASES:
            drafts.append((region, kind, cases))
        else:
            rest.extend(cases)
    rest, notable = _split_notable(rest)
    drafts.sort(key=lambda d: (d[0] != REGION_JAPAN, -_weight(d[2])))
    patterns = tuple(
        Pattern(pattern_id=f"P{n}", region=r, kind=k, cases=tuple(cs))
        for n, (r, k, cs) in enumerate(drafts, start=1)
    )
    return PatternPlan(
        patterns=patterns,
        rest=tuple(rest),
        notable=notable,
        standing=standing,
        moved_count=len(moved),
        top_judgment=moved[0] if moved else (ranked[0] if ranked else None),
    )


def _weight(cases: Sequence[PatternCase]) -> int:
    return S3_WEIGHT * sum(c.is_s3 for c in cases) + len(cases)


def _split_notable(
    rest: Sequence[PatternCase],
) -> tuple[list[PatternCase], tuple[NotableCase, ...]]:
    """そのほかの動きのうち S3 の事案を、重い順に最大 ``NOTABLE_MAX`` 件「注目」へ移す。"""
    ordered = sorted(rest, key=lambda c: c.rank)
    picked = [c for c in ordered if c.is_s3][:NOTABLE_MAX]
    notable = tuple(NotableCase(f"S{n}", c) for n, c in enumerate(picked, start=1))
    return [c for c in ordered if c not in picked], notable
