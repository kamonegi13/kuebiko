"""深掘りの要点ごとの裏付け関門 (n20 T5、旗 ``DEEP_DIVE_POINT_GROUNDING``)。

EVAL-N20 の読み (data/eval/n20_e1/deep_dive_read.md §1・§3-1) で、別の事案の事実・名前を
別の要点へ移す取り違えが 1 run に 1 回起きた。要点 {text, article_ids} は根拠の記事を持つのに、
``to_recap`` が節へ平らにして以降は何も突き合わせていなかった。

要点の固有名詞・識別子・単位つきの数値が、**その要点の根拠の記事**
(見出し + 要約。DigestCandidate が持つのはここまで。本文は無い) のどれにも無ければ、
その要点を本文に入れない (決定論)。
**語は「事案固有の名前」だけを照合する**: 取り違えは別の事案の名前を移すことなので、
この深掘りの入力の**少数 (3 件以下) の記事**の見出し・要約に出る語か、辞書の既知のアクター名・別名
(``config/cti/actor_aliases.yaml``) だけが対象。SaaS・CloudTrail・MSSQL・IoC 等の一般の技術語は
どの事案にも属さず移植にならないので見ない (EVAL で誤って落とした要点の約半数がこれだった)。
識別子と単位つきの数値は従来どおり常に照合する。
照合は ``assessment.claim_grounding`` (正規化・表記ゆれ・一般語の除外・出典名の扱い) と
``tools.identifier_match`` (CVE・IP・版数等のトークン境界つき照合) を再利用し、複製しない。
判定の材料が無い要点 (根拠の記事の見出し・要約が空) は判定できないので通す (fail-open)。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import structlog

from src.assessment.claim_grounding import GroundingSources, check_grounding
from src.cti.actor_normalizer import ActorAliasRegistry, load_actor_aliases
from src.digest.recap_points import RecapPoint, RecapPointSection, RecapPointsOutput
from src.tools.identifier_match import contains_identifier, extract_identifiers

_log = structlog.get_logger(__name__)

#: 単位つきの数値 (「19 組織」「12個」「3,000 件」)。小さい数は LLM の数え直しが多いので見ない
_COUNT_UNIT_RE = re.compile(r"(\d[\d,]*)\s*(?:組織|社|件|個|台|人|カ国|か国|ヶ国|万|億|%)")
_MIN_COUNT = 10
#: カタカナだけの語は照合しない。EVAL の再現で、要約が別の訳語・原綴りで書く一般の外来語
#: (パッチ・ウォレット・リポジトリ等) の誤検出が違反の大半を占めた。取り違えの本体は
#: 英字の名前・識別子・数値で、そちらは照合する
_KATAKANA_ONLY_RE = re.compile(r"[ァ-ヴー・]+")
#: 識別子のうち照合する種別 (proper_noun は ``claim_grounding`` の語の照合が担う)
#: 語が何件までの記事に出れば「事案固有」とみなすか。それより多くの記事に出る語は一般語
_MAX_CASE_DOC_FREQ = 3
_IDENT_KINDS = frozenset({"cve", "ip", "hash", "domain", "version", "cvss", "actor_id"})


@dataclass(frozen=True)
class PointSource:
    """要点の根拠の記事 1 件の照合の材料。``text`` = 見出し + 要約、``attribution`` = 媒体名。"""

    text: str
    attribution: str = ""


def _digits(s: str) -> str:
    return unicodedata.normalize("NFKC", s).replace(",", "")


def _numbers_missing(text: str, haystack: str) -> list[str]:
    hay = _digits(haystack)
    missing: list[str] = []
    for m in _COUNT_UNIT_RE.finditer(_digits(text)):
        n = m.group(1)
        if int(n) < _MIN_COUNT:
            continue
        if not re.search(rf"(?<!\d){re.escape(n)}(?!\d)", hay):
            missing.append(n)
    return missing


def _norm(s: str) -> str:
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", s).casefold())


def _known_actor_tokens(text: str, actors: ActorAliasRegistry | None) -> frozenset[str]:
    """``text`` に出る辞書の既知のアクター名・別名を語に割った正規形 (「Volt Typhoon」→ 両語)。"""
    if actors is None:
        return frozenset()
    names = (n for a in actors.find_all(text) for n in actors.matched_names_for(a, text))
    return frozenset(_norm(tok) for n in names for tok in n.split())


def _case_specific(
    terms: list[str], text: str, corpus: Sequence[str], actors: ActorAliasRegistry | None
) -> list[str]:
    """``terms`` のうち事案固有の名前。

    入力の記事のうち 1〜``_MAX_CASE_DOC_FREQ`` 件の見出し・要約に出る語 (多くの記事に出る語は
    ISC・Windows のような一般の語で、移植の対象にならない)、または辞書の既知の名前。
    どの記事にも出ない語は一般語として見ない。
    """
    haystacks = [_norm(t) for t in corpus]
    known = _known_actor_tokens(text, actors)
    out: list[str] = []
    for term in terms:
        key = _norm(term)
        df = sum(key in h for h in haystacks) if key else 0
        if 1 <= df <= _MAX_CASE_DOC_FREQ or _norm(term) in known:
            out.append(term)
    return out


def point_violations(
    text: str,
    sources: list[PointSource],
    *,
    corpus: Sequence[str] | None = None,
    actors: ActorAliasRegistry | None = None,
) -> tuple[str, ...]:
    """要点の文の中で、根拠の記事のどれにも無い語・識別子・数値 (出現順)。判定不能なら空。

    ``corpus`` (入力の記事ごとの見出し + 要約) を渡すと、語は事案固有の名前
    (corpus の少数の記事に出る語・``actors`` の既知の名前) だけを違反に数える。
    None なら全ての語を見る。
    """
    material = "\n".join(s.text for s in sources).strip()
    if not material:
        return ()  # 判定材料が無い → 通す (fail-open)
    attribution = "\n".join(s.attribution for s in sources)
    report = check_grounding(text, GroundingSources(text=material, attribution=attribution))
    idents = [
        i.raw for i in extract_identifiers(text) if i.kind in _IDENT_KINDS
        and not contains_identifier(material, i)
    ]  # fmt: skip
    terms = [v for v in report.violations if not _KATAKANA_ONLY_RE.fullmatch(v)]
    if corpus is not None:
        terms = _case_specific(terms, text, corpus, actors)
    return (*terms, *idents, *_numbers_missing(text, material))


def ground_points(
    out: RecapPointsOutput,
    *,
    sources: Mapping[str, PointSource],
    apply: bool,
) -> RecapPointsOutput:
    """裏付けの無い要点を数え、``apply`` なら落とした新しい出力を返す (元は不変)。

    ログ ``deep_dive_point_ungrounded`` は節・要点の位置・根拠 id・違反の数だけ
    (本文・語は出さない)。
    ``apply=False`` (shadow) は出力をそのまま返す。
    """
    corpus = [s.text for s in sources.values()]
    actors = load_actor_aliases()
    sections: list[RecapPointSection] = []
    dropped = 0
    for si, sec in enumerate(out.sections):
        kept: list[RecapPoint] = []
        for pi, p in enumerate(sec.points):
            own = [sources[a] for a in p.article_ids if a in sources]
            bad = point_violations(p.text, own, corpus=corpus, actors=actors) if own else ()
            if not bad:
                kept.append(p)
                continue
            dropped += 1
            _log.warning(
                "deep_dive_point_ungrounded",
                section=si,
                point=pi,
                article_ids=list(p.article_ids),
                violations=len(bad),
                applied=apply,
            )
            if not apply:
                kept.append(p)
        sections.append(sec.model_copy(update={"points": kept}))
    if dropped:
        _log.info("deep_dive_point_grounding", ungrounded=dropped, applied=apply)
    return out.model_copy(update={"sections": sections}) if apply else out
