"""Grounded synthesis の LLM パス: 段0 claim ノミネート / 段1+2 証拠接地 + ACH。

設計: docs/synthesis_reliability_redesign.md。対称客観性 (原則0) をプロンプトに明示。
**source_tier と確度上限はコード側で決定論的に付与し LLM に委ねない**。parse は
generate_structured (pydantic 検証 + 修復) で頑健化し、未知値はコードで正規化する。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import jinja2
from pydantic import BaseModel, Field

from src.logging_config import get_logger
from src.synthesis.grounded.estimate import (
    AttributionBasis,
    Confidence,
    EvidenceItem,
    HypothesisScore,
    Polarity,
    derive_verdict,
)
from src.synthesis.grounded.hypotheses import Hypothesis, hypotheses_for_domain, is_known_hypothesis
from src.tools.llm_client import LLMClient

_log = get_logger(__name__)
_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"
# registry の legacy_path (相対 Path) との一致判定・build_prompt_template 呼出に使う相対 root
# (dispatch/digest 系の seam と同じ規約。_PROMPTS_DIR は絶対 path なので legacy_path と比較不可)。
_PROMPTS_ROOT = Path("prompts")

# template 引数 (env.get_template に渡す相対名) → registry の prompt_id。
# render.py もここを import して使う (層分け 7〜12 本目、2026-08-20: この 1 seam を
# 6 テンプレート全消費者 (passes/incremental/adversary/render) が共有する)。
_TEMPLATE_TO_PROMPT_ID: dict[str, str] = {
    "synthesis/nominate.j2": "nominate",
    "synthesis/ground_ach.j2": "ground_ach",
    "synthesis/ground_incremental.j2": "ground_incremental",
    "synthesis/detect_new.j2": "detect_new",
    "synthesis/adversarial.j2": "adversarial",
    "synthesis/render.j2": "synthesis_render",
}

_TEMPERATURE = 0.2
_MAX_TOKENS = 8_000
_MAX_BODY_CHARS = 4_000  # ソース本文の 1 記事あたり上限 (prompt 肥大防止)

_ATTRIBUTION_OPTIONS = (
    "govt_confirmed(政府/CERT確認) / vendor_confirmed(セキュリティベンダ確認) / "
    "victim_disclosed(被害組織の公表) / researcher_assessed(研究者の分析評価) / "
    "tooling_similarity(ツール/TTP類似のみ) / claimed_by_actor(攻撃者の自称) / "
    "state_media_claim(国営メディアの主張) / unattributed(帰属なし) / speculation(推測)"
)

_VALID_BASIS: frozenset[str] = frozenset(
    {
        "govt_confirmed",
        "vendor_confirmed",
        "victim_disclosed",
        "researcher_assessed",
        "tooling_similarity",
        "claimed_by_actor",
        "state_media_claim",
        "unattributed",
        "speculation",
    }
)
_VALID_POLARITY: frozenset[str] = frozenset({"supports", "contradicts", "neutral"})
_VALID_CONF: frozenset[str] = frozenset({"high", "moderate", "low"})


def _render(template: str, **ctx: object) -> str:
    """grounded ACH 群 6 本の共有 render seam (層分け 7〜12 本目、2026-08-20)。

    registry に spec があり flag が ON なら DB 合成テンプレートを使う。無ければ (flag OFF /
    DB 未投入 / 合成失敗) legacy .j2 に fail-safe で倒す (prompt_store 側が理由を WARNING に残す)。
    incremental.py / adversary.py / render.py はこの関数を import して使うため、ここ 1 箇所の
    改修で 6 テンプレート全消費者に効く。
    """
    from src.prompts.prompt_store import build_prompt_template
    from src.prompts.registry import get_spec

    tmpl: jinja2.Template | None = None
    prompt_id = _TEMPLATE_TO_PROMPT_ID.get(template)
    if prompt_id is not None:
        spec = get_spec(prompt_id)
        # ⚠ template は引数 — spec.legacy_path との一致を関門にする (llm_digest.py と同型):
        # マッピングと registry の legacy_path がずれたとき、要求と違うプロンプトが
        # 黙って合成に化けるのを防ぐ (不一致は legacy 経路へ)。
        if spec is not None and (_PROMPTS_ROOT / template) == spec.legacy_path:
            tmpl = build_prompt_template(spec, _PROMPTS_ROOT / template)
    if tmpl is None:
        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(_PROMPTS_DIR)),
            autoescape=False,
            undefined=jinja2.StrictUndefined,
        )
        tmpl = env.get_template(template)
    return tmpl.render(**ctx)


# 前方一致で id を引き当てる際の最小長。**本来のガードは一意性** (複数候補に当たる
# 断片は解決しない) で、この長さは "rss:" のような scheme だけの断片を落とすためのもの。
_MIN_ID_PREFIX = 16


def _norm_id(v: str) -> str:
    """id 照合用の正規化 (小文字化 + 空白除去)。"""
    return "".join((v or "").split()).casefold()


def _norm_basis(b: str) -> AttributionBasis:
    return cast(AttributionBasis, b) if b in _VALID_BASIS else "unattributed"


def _norm_polarity(p: str) -> Polarity:
    return cast(Polarity, p) if p in _VALID_POLARITY else "neutral"


def _norm_conf(c: str) -> Confidence:
    return cast(Confidence, c) if c in _VALID_CONF else "low"


# ---------- 段0: claim ノミネート ----------


class _WireNomination(BaseModel):
    model_config = {"extra": "ignore"}
    claim: str = ""
    domain: str = ""
    article_ids: list[str] = Field(default_factory=list)


class _WireNominationResult(BaseModel):
    model_config = {"extra": "ignore"}
    claims: list[_WireNomination] = Field(default_factory=list)


@dataclass(frozen=True)
class NominatedClaim:
    claim: str
    domain: str
    article_ids: tuple[str, ...]


async def nominate_claims(
    *, llm: LLMClient, articles: list[dict[str, object]], period_label: str, k: int
) -> list[NominatedClaim]:
    """段0: 集約から key judgment 候補を最大 k 件ノミネート (各々に支持 article_id)。"""
    if not articles:
        return []
    prompt = _render("synthesis/nominate.j2", articles=articles, period_label=period_label, k=k)
    result = await llm.generate_structured(
        prompt, _WireNominationResult, temperature=_TEMPERATURE, max_tokens=_MAX_TOKENS, think=False
    )
    out: list[NominatedClaim] = []
    for c in result.claims[:k]:
        claim = c.claim.strip()
        ids = tuple(a for a in c.article_ids if a)
        if claim and ids:
            # domain 不明は "unclassified" (cyber_incident に倒すと非サイバー事象に
            # cyber framing を被せる bias になる)。
            out.append(
                NominatedClaim(
                    claim=claim, domain=c.domain.strip() or "unclassified", article_ids=ids
                )
            )
    return out


# ---------- 段1+2: 証拠接地 + ACH ----------


class _WireEvidence(BaseModel):
    model_config = {"extra": "ignore"}
    # 候補一覧の [N] 番号 (1-based、0 = 未指定)。長い article_id を写させると転記が
    # 壊れるため **番号参照を主経路にする** (Spotlight の key_events と同じ設計)。
    # ``int | None`` は structured 生成で anyOf/null になり不安定なため 0 を番兵にする。
    index: int = 0
    article_id: str = ""
    attribution_basis: str = "unattributed"
    excerpt: str = ""
    polarity: str = "neutral"


class _WireHypothesis(BaseModel):
    model_config = {"extra": "ignore"}
    hypothesis: str = ""
    consistent: int = 0
    inconsistent: int = 0


class _WireAnalysis(BaseModel):
    model_config = {"extra": "ignore"}
    evidence: list[_WireEvidence] = Field(default_factory=list)
    hypotheses: list[_WireHypothesis] = Field(default_factory=list)
    leading_hypothesis: str = ""  # 既定は空 (実体 id にすると parse 欠落が確定判定に化ける)
    confidence: str = "low"
    key_assumptions: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    indicators: list[str] = Field(default_factory=list)
    # 段B: 判定の性質と含意 (証拠接地・確度準拠)。欠落時は空で後方互換。
    claim_type: str = ""
    implication: str = ""


_VALID_CLAIM_TYPES: frozenset[str] = frozenset({"ongoing_activity", "discrete_event", "structural"})


def norm_claim_type(v: str) -> str:
    """claim_type の未知値は ongoing_activity に倒す (減衰対象側 = 保守的)。"""
    return v if v in _VALID_CLAIM_TYPES else "ongoing_activity"


@dataclass(frozen=True)
class ClaimAnalysis:
    evidence: tuple[EvidenceItem, ...]
    hypotheses: tuple[HypothesisScore, ...]
    leading_hypothesis: str
    llm_confidence: Confidence
    key_assumptions: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    indicators: tuple[str, ...]
    claim_type: str = "ongoing_activity"
    implication: str = ""


def _verdict(
    h: _WireHypothesis, leading: str
) -> Literal["leading", "viable", "refuted", "unscored"]:
    # 導出実装は estimate.derive_verdict が SSoT (二重表現の整合不変量)。
    return derive_verdict(
        hypothesis=h.hypothesis,
        consistent=h.consistent,
        inconsistent=h.inconsistent,
        leading=leading,
    )



def _resolve_source_id(raw: str, known: dict[str, str]) -> str | None:
    """LLM が返した article_id を、prompt で渡した実 id へ寄せる。

    LLM は候補外の記事を引用できない (prompt に本文しか無い) ため、返ってくる id は
    **実在 id の転記**であって捏造ではない。実測される破損の型:
    - ``rss:https://example.com/x | The Register`` — feed 名を連結
    - ``example.com/?p=123`` — ``rss:https://`` prefix の欠落
    - 末尾の切り詰め / 前後の空白

    ``known`` は {正規化キー: 実 id}。曖昧一致 (複数候補) は解決しない — 誤った
    記事へ証拠を付けるより落とす方が安全 (台帳は帰属の台帳であるため)。
    """
    if not raw:
        return None
    candidates = [raw.strip()]
    # 「id | feed title」形式を分解 (feed 名側から id を引き当てない)
    if "|" in raw:
        candidates.append(raw.split("|", 1)[0].strip())
    expanded: list[str] = []
    for c in candidates:
        expanded.append(c)
        if not c.startswith(("rss:", "grok:", "rl_")):
            expanded.append(f"rss:https://{c.lstrip('/')}")
            expanded.append(f"rss:{c}")
    for c in expanded:
        hit = known.get(_norm_id(c))
        if hit:
            return hit
    # 前方一致 (末尾切り詰め)。一意に定まるときだけ採用する
    for c in expanded:
        key = _norm_id(c)
        if len(key) < _MIN_ID_PREFIX:
            continue
        matches = {v for k, v in known.items() if k.startswith(key) or key.startswith(k)}
        if len(matches) == 1:
            return next(iter(matches))
    return None



def _resolve_evidence_source(
    index: int | None, raw_id: str, sources: list[dict[str, str]]
) -> str | None:
    """証拠の出典参照を実 article_id に解決する。

    優先順 (Spotlight ``_resolve_event_match`` と同型):
      1. ``index`` (候補一覧の [N] 番号、1-based) — 転記が壊れないので最も確実
      2. ``article_id`` の修復解決 (``_resolve_source_id``)
    どちらも解けなければ None (誤った記事へ証拠を付けるより落とす)。
    """
    if index is not None and 1 <= index <= len(sources):
        return str(sources[index - 1].get("article_id", "")) or None
    # 実測: LLM は番号を article_id 側に文字列で入れることがある ("1")。
    # 候補一覧の範囲内の裸の整数は番号として解する (書式でなく意図に寄せる)。
    bare = (raw_id or "").strip()
    if bare.isdigit() and 1 <= int(bare) <= len(sources):
        return str(sources[int(bare) - 1].get("article_id", "")) or None
    known = {
        _norm_id(str(src.get("article_id", ""))): str(src.get("article_id", ""))
        for src in sources
    }
    known.pop("", None)
    return _resolve_source_id(raw_id, known)



def build_evidence_items(
    wire_evidence: Sequence[Any],
    *,
    sources: list[dict[str, str]],
    tier_by_id: dict[str, str],
    log_context: str,
) -> tuple[EvidenceItem, ...]:
    """LLM の証拠 wire を EvidenceItem へ落とす **ACH 共通の組み立て口**。

    ACH は初回 (``ground_and_score``) と増分 (``incremental_ground_and_score``) の 2 経路が
    あり、以前は同一のループを両方に複製していた。2026-08-22 に「出典参照の解決」を
    初回だけに入れて増分を取り残し本番で欠落を出したため、**組み立てを 1 箇所に寄せた**
    (規律を片方の経路にだけ入れる事故を構造的に起こせなくする)。

    規律: ①空 excerpt は捨てる ②出典は番号優先で実 article_id へ解決し、解けなければ
    捨てる (誤った記事へ証拠を付けない) ③source_tier は LLM でなくコードが付与する。
    """
    items: list[EvidenceItem] = []
    unresolved = 0
    for e in wire_evidence:
        if not e.excerpt.strip():
            continue
        aid = _resolve_evidence_source(e.index, e.article_id, sources)
        if aid is None:
            unresolved += 1
            continue
        items.append(
            EvidenceItem(
                article_id=aid,
                source_tier=tier_by_id.get(aid, "unknown"),  # tier はコードが付与
                attribution_basis=_norm_basis(e.attribution_basis),
                excerpt=e.excerpt.strip()[:500],
                polarity=_norm_polarity(e.polarity),
            )
        )
    if unresolved:
        _log.warning(
            "grounded_evidence_id_unresolved", context=log_context[:60], dropped=unresolved
        )
    return tuple(items)


async def ground_and_score(
    *,
    llm: LLMClient,
    claim: str,
    domain: str,
    sources: list[dict[str, str]],
    tier_by_id: dict[str, str],
    hypotheses_override: tuple[Hypothesis, ...] | None = None,
) -> ClaimAnalysis:
    """段1+2: ソース本文を読み、証拠台帳 + ACH 採点を得る。

    sources: ``[{article_id, feed_title, text}]`` (text は本文 or summary)。
    tier_by_id: article_id → source_tier (コードが classify_source_tier で算出済、LLM 不信)。
    domain: nominate の domain。サイバー/地政学で ACH 仮説セットを切り替える。
    """
    prompt = _render(
        "synthesis/ground_ach.j2",
        claim=claim,
        sources=sources,
        attribution_options=_ATTRIBUTION_OPTIONS,
        hypotheses=hypotheses_override or hypotheses_for_domain(domain),
    )
    a = await llm.generate_structured(
        prompt, _WireAnalysis, temperature=_TEMPERATURE, max_tokens=_MAX_TOKENS, think=False
    )
    if is_known_hypothesis(a.leading_hypothesis):
        leading = a.leading_hypothesis
    else:
        # parse 欠落/未知 id → unverified_or_false に倒すが silent にしない (品質監視)
        _log.warning("grounded_leading_unknown", claim=claim[:60], raw=a.leading_hypothesis[:40])
        leading = "unverified_or_false"
    # LLM が返す article_id は転記で壊れる (feed 名の連結 / prefix 欠落 / 切り詰め)。
    # prompt で渡した id 集合へ寄せてから採用する — 解決できないものは捨てる
    # (候補外を引用することは原理的に無いため、解決不能 = 転記不能な破損)。
    evidence = build_evidence_items(
        a.evidence, sources=sources, tier_by_id=tier_by_id, log_context=claim
    )
    hyps = tuple(
        HypothesisScore(
            hypothesis=h.hypothesis,
            consistent=max(0, h.consistent),
            inconsistent=max(0, h.inconsistent),
            verdict=_verdict(h, leading),
        )
        for h in a.hypotheses
        if is_known_hypothesis(h.hypothesis)
    )
    return ClaimAnalysis(
        evidence=evidence,
        hypotheses=hyps,
        leading_hypothesis=leading,
        llm_confidence=_norm_conf(a.confidence),
        key_assumptions=tuple(s.strip() for s in a.key_assumptions[:3] if s.strip()),
        missing_evidence=tuple(s.strip() for s in a.missing_evidence[:3] if s.strip()),
        indicators=tuple(s.strip() for s in a.indicators[:3] if s.strip()),
        claim_type=norm_claim_type(a.claim_type.strip()),
        implication=a.implication.strip()[:400],
    )


def truncate_body(text: str, *, max_chars: int = _MAX_BODY_CHARS) -> str:
    """ソース本文を prompt 用に上限まで切り詰める (文境界優先で中途切断を緩和)。

    過去文脈ソースは max_chars を短くして prompt 肥大を抑える (現在の事象を主に読む)。
    """
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    # 上限手前の最後の文末/改行で切る (なければハード切断)
    boundary = max(cut.rfind("。"), cut.rfind("\n"), cut.rfind(". "))
    return cut[: boundary + 1] if boundary >= max_chars - 500 else cut
