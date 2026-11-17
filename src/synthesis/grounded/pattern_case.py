"""横断パターンの「事案」(同じ事象の判定を畳んだ 1 件) の部品 (決定論・純粋関数)。

- 題: 根拠記事の事象ニュースの見出し (最新版、根拠記事が最も多い事象。同数なら新しい版)。
  事象が無い判定だけ claim の第 1 文を **切らずに** 使う (「…」で切らない、2026-11-17)。
- 名前: 事案を名指ししているかの照合語 (題・claim の主語と固有名詞候補)。見出しの関門と
  委託先等の語の関門が共有する。
- 重さ = ``S3_WEIGHT × S3 + 大規模 (severity_basis=large_magnitude) + 被害の件数の桁``。
  件数は本文に書かれた被害の件数 (``importance_v2.stated_victim_count``) を claim と事象の見出しから
  読んだもの。**記事の件数・言及量は使わない** (CLAUDE.md §7)。
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from src.assessment.claim_grounding import extract_terms
from src.cti.importance_v2 import stated_victim_count
from src.storage.repo_judgment_attrs import EventHeadline, JudgmentAttrs
from src.synthesis.grounded.estimate import KeyJudgment

JAPAN_ISO = "JP"
SEVERITY_S3 = "S3"
#: 重さ = S3_WEIGHT × S3 + 大規模の印 (1) + 被害の件数の桁 (``magnitude_bucket``)
S3_WEIGHT = 2
LARGE_MAGNITUDE_WEIGHT = 1
#: 被害の件数の桁: この件数以上で 1 (万の位)、桁が 1 つ上がるごとに +1、上限まで
MAGNITUDE_MIN_COUNT = 10_000
MAGNITUDE_MAX_BUCKET = 4
_MAGNITUDE_MIN_EXPONENT = round(math.log10(MAGNITUDE_MIN_COUNT))
_SUBJECT = re.compile(r"^(.{2,16}?)(?:は|が|で|の|に|を|、)")


def magnitude_bucket(count: int) -> int:
    """件数 → 0 (万未満) 〜 ``MAGNITUDE_MAX_BUCKET`` (10 の ``…`` 乗の桁)。"""
    if count < MAGNITUDE_MIN_COUNT:
        return 0
    return min(MAGNITUDE_MAX_BUCKET, int(math.log10(count)) - _MAGNITUDE_MIN_EXPONENT + 1)


def first_full_sentence(text: str) -> str:
    """最初の「。」までを切らずに返す (句点は付けない)。"""
    cut = text.find("。")
    return (text if cut < 0 else text[:cut]).strip()


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
    #: プロンプトで渡す安定 id (C1…)。パターン → 注目 → そのほかの順
    case_id: str = ""
    title_from_event: bool = False
    large_magnitude: bool = False
    stated_count: int = 0
    names: tuple[str, ...] = ()
    techniques: frozenset[str] = frozenset()
    products: frozenset[str] = frozenset()
    links: frozenset[str] = frozenset()

    @property
    def short_name(self) -> str:
        """一覧・つながりの行で使う短い名前 (題・claim の主語。無ければ題)。"""
        return self.names[0] if self.names else self.title

    @property
    def weight(self) -> int:
        return (
            S3_WEIGHT * self.is_s3
            + LARGE_MAGNITUDE_WEIGHT * self.large_magnitude
            + magnitude_bucket(self.stated_count)
        )


def case_names(title: str, claim: str) -> tuple[str, ...]:
    """題・claim の主語 + 固有名詞候補 (重複なし、出現順)。"""
    found: list[str] = []
    for text in (title, claim):
        m = _SUBJECT.match(text)
        if m:
            found.append(m.group(1))
        found.extend(extract_terms(text))
    return tuple(dict.fromkeys(n for n in found if n))


def merged_attrs(
    members: Sequence[KeyJudgment], attrs: Mapping[str, JudgmentAttrs]
) -> JudgmentAttrs:
    parts = [attrs.get(j.id, JudgmentAttrs()) for j in members]
    return JudgmentAttrs(
        countries=frozenset().union(*(p.countries for p in parts)),
        kinds=frozenset().union(*(p.kinds for p in parts)),
        severities=frozenset().union(*(p.severities for p in parts)),
        event_roots=frozenset().union(*(p.event_roots for p in parts)),
        large_magnitude=any(p.large_magnitude for p in parts),
        techniques=frozenset().union(*(p.techniques for p in parts)),
        products=frozenset().union(*(p.products for p in parts)),
        links=frozenset().union(*(p.links for p in parts)),
        event_headlines=tuple(h for p in parts for h in p.event_headlines),
    )


def best_event_headline(headlines: Sequence[EventHeadline]) -> str:
    """根拠記事が最も多い事象の見出し (同数なら新しい版)。無ければ空。"""
    per_root: dict[str, tuple[int, str, str]] = {}
    for h in headlines:
        n, gen, text = per_root.get(h.root, (0, "", ""))
        newest = (h.generated_at, h.headline) if h.generated_at >= gen else (gen, text)
        per_root[h.root] = (n + h.articles, *newest)
    if not per_root:
        return ""
    return max(per_root.values(), key=lambda v: (v[0], v[1]))[2]


def make_case(
    members: Sequence[KeyJudgment], attrs: Mapping[str, JudgmentAttrs], rank: int
) -> tuple[PatternCase, str | None]:
    """判定の群から事案を作る。2 つ目の値は事案の種別キー (``pattern_kind``) の材料の種別。"""
    from src.synthesis.grounded.patterns import pattern_kind

    rep = members[0]  # ranked 順の先頭 = 最も重い判定
    merged = merged_attrs(members, attrs)
    event_title = best_event_headline(merged.event_headlines)
    title = event_title or first_full_sentence(rep.claim)
    claims = tuple(j.claim for j in members)
    case = PatternCase(
        judgment_ids=tuple(j.id for j in members),
        title=title,
        claims=claims,
        delta_type=rep.delta_type,
        confidence=rep.confidence,
        is_s3=SEVERITY_S3 in merged.severities,
        is_japan=JAPAN_ISO in merged.countries or any(j.japan_related for j in members),
        rank=rank,
        title_from_event=bool(event_title),
        large_magnitude=merged.large_magnitude,
        stated_count=max(stated_victim_count(f"{c}\n{event_title}") for c in claims),
        names=case_names(title, rep.claim),
        techniques=merged.techniques,
        products=merged.products,
        links=merged.links,
    )
    return case, pattern_kind(merged.kinds)
