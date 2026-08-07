"""Grounded synthesis の canonical 状態オブジェクト (Estimate) と決定論ガードレール。

Estimate は「証拠接地 → ACH → 敵対的検証」を経た期間の情勢評価 (state)。narrative はこれの射影。
本モジュールは LLM を呼ばない純粋層: スキーマ + 確度 cap + 過剰帰属ガードレール (テスト容易)。
詳細設計: docs/synthesis_reliability_redesign.md。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Literal

AttributionBasis = Literal[
    "govt_confirmed",
    "vendor_confirmed",
    "victim_disclosed",
    "researcher_assessed",
    "tooling_similarity",
    "claimed_by_actor",
    "state_media_claim",
    "unattributed",
    "speculation",
]
Confidence = Literal["high", "moderate", "low"]
Polarity = Literal["supports", "contradicts", "neutral"]

# 「強い帰属根拠」(これが無ければ特定アクターへの帰属を高確度で断定しない)。
STRONG_ATTRIBUTION: frozenset[str] = frozenset(
    {"govt_confirmed", "vendor_confirmed", "researcher_assessed", "victim_disclosed"}
)
WEAK_ATTRIBUTION: frozenset[str] = frozenset(
    {"tooling_similarity", "claimed_by_actor", "state_media_claim", "unattributed", "speculation"}
)
# 特定アクター/主体を帰属する仮説 (= 帰属証拠を要する)。commodity/accidental/reporting/
# unverified は「誰がやったか」を主張しないので帰属上限の対象外。方向に依らず threat 側
# (organized) も benign-but-attributed 側 (criminal/hacktivism) も等しく cap する (対称)。
ATTRIBUTION_SENSITIVE: frozenset[str] = frozenset(
    {"organized_state_op", "criminal_financial", "hacktivism_influence"}
)

_CONF_RANK: dict[str, int] = {"low": 0, "moderate": 1, "high": 2}
# source_basis.confidence (high/medium/low) → Estimate confidence の決定論的上限。
_SB_CEILING: dict[str, Confidence] = {"high": "high", "medium": "moderate", "low": "low"}
# 強帰属 (govt/vendor/研究/被害公表) が leading を支持するとき tier 上限を一段引上げる。
_STEP_UP: dict[Confidence, Confidence] = {"low": "moderate", "moderate": "high", "high": "high"}


@dataclass(frozen=True)
class EvidenceItem:
    """1 件の証拠 (本文抜粋 + 帰属根拠の型 + 出典 tier)。article_id で本文に traceable。"""

    article_id: str
    source_tier: str
    attribution_basis: AttributionBasis
    excerpt: str
    polarity: Polarity = "neutral"


@dataclass(frozen=True)
class HypothesisScore:
    """ACH の 1 仮説の採点。inconsistent (反整合数) が判定の主軸。"""

    hypothesis: str
    consistent: int
    inconsistent: int
    verdict: Literal["leading", "viable", "refuted", "unscored"]


# ---------- 判定整合の不変量 (2026-07-25 ACH 整合監査) ----------
# leading_hypothesis (スカラ) と hypotheses[].verdict (マトリクス) は同じ事実の二重表現で、
# 片方だけの更新 (旧 apply_adversarial の flip 等) が 388 revision 中 21 件の自己矛盾を生んだ。
# SSoT は「verdict は (採点カウント, leading) からの導出値」— 以下がその唯一の導出実装。


def derive_verdict(
    *, hypothesis: str, consistent: int, inconsistent: int, leading: str
) -> Literal["leading", "viable", "refuted", "unscored"]:
    """verdict を (counts, leading) から導出する (verdict の SSoT)。"""
    if hypothesis == leading:
        return "leading"
    if consistent == 0 and inconsistent == 0:
        return "unscored"  # LLM が当該仮説を実質評価しなかった (viable と区別)
    return "refuted" if inconsistent > consistent else "viable"


def is_counts_refuted(h: HypothesisScore) -> bool:
    """採点カウント上「反証」相当か (反整合 > 整合)。"""
    return h.inconsistent > h.consistent


def reconcile_hypotheses(
    hypotheses: tuple[HypothesisScore, ...], leading: str
) -> tuple[HypothesisScore, ...]:
    """マトリクスの verdict を (counts, leading) から導出し直す (整合不変量の強制)。

    leading がマトリクスに無い場合 (増分 parse-fallback で前回見立てを維持した等) は
    0/0 行を追記する — 「今回未採点のまま見立てを維持」を可視化する (カウントは捏造しない)。
    """
    out = tuple(
        HypothesisScore(
            hypothesis=h.hypothesis,
            consistent=h.consistent,
            inconsistent=h.inconsistent,
            verdict=derive_verdict(
                hypothesis=h.hypothesis,
                consistent=h.consistent,
                inconsistent=h.inconsistent,
                leading=leading,
            ),
        )
        for h in hypotheses
    )
    if leading and all(h.hypothesis != leading for h in out):
        out = (
            *out,
            HypothesisScore(hypothesis=leading, consistent=0, inconsistent=0, verdict="leading"),
        )
    return out


@dataclass(frozen=True)
class KeyJudgment:
    """期間の主要判定 1 件 (ACH + 敵対的検証 + 較正確度 + 接地証拠)。"""

    id: str
    claim: str
    domain: str
    leading_hypothesis: str
    confidence: Confidence
    confidence_basis: str
    hypotheses: tuple[HypothesisScore, ...]
    evidence: tuple[EvidenceItem, ...]
    key_assumptions: tuple[str, ...] = ()
    missing_evidence: tuple[str, ...] = ()
    indicators: tuple[str, ...] = ()
    adversarial_refuted: bool = False
    adversarial_note: str = ""
    # 段B (Situation Ledger): 判定の性質 (継続活動/単発事象/構造変化) と証拠接地された含意。
    # 既定は空 = 旧経路互換 (Estimate embed の後方互換のため optional)。
    claim_type: str = ""
    implication: str = ""
    # 段C: delta を estimate の一級データにする (render は射影に徹する = 設計 §4)。
    # delta_type: opened/strengthened/weakened/hypothesis_flip/escalated/claim_revised/
    #             reopened/no_change (台帳 revision と同値、コードの前後比較で決定論付与)。
    delta_type: str = ""
    delta_note: str = ""
    fired_indicators: tuple[str, ...] = ()
    pir_ids: tuple[str, ...] = ()
    japan_related: bool = False
    # 状態分離 (2026-07-16): evidence は ACH 評価済みのみを載せ、割当だけで未評価の
    # 記事は件数で正直に併記する (「中立の証拠」に化けさせない)。0 = 未評価なし。
    unassessed_count: int = 0


@dataclass(frozen=True)
class Estimate:
    """期間の canonical 情勢評価。narrative/tradecraft はこれの射影。"""

    period_type: str
    period_start: datetime
    period_end: datetime
    judgments: tuple[KeyJudgment, ...]
    model: str = ""
    # ノミネート時に考慮した記事プールの総数 (接地した実記事数=article_count とは別)。
    # 「接地N / 考慮M」を正直に併記し、少数接地を実態以上に貧弱に見せない (母数の透明化)。
    considered_count: int = 0
    # 段D: 判定間の関係エッジ (a_id, b_id, rel_type, basis)。決定論 (共有 anchor) 由来で
    # 因果は主張しない。chain セクションの供給源。
    relations: tuple[tuple[str, str, str, str], ...] = ()


def estimate_to_dict(est: Estimate) -> dict[str, Any]:
    """Estimate を JSON シリアライズ可能な dict に (tradecraft 内 grounded_estimate 永続化用)。"""
    d = asdict(est)
    d["period_start"] = est.period_start.isoformat()
    d["period_end"] = est.period_end.isoformat()
    return d


def cap_confidence(llm_conf: Confidence, source_basis_conf: str) -> tuple[Confidence, str]:
    """LLM が ACH から導いた確度を source_basis 由来の決定論的上限で抑える。

    弱いソース (single social / state_media = low) に高確度を付けさせない (ICD 203 #5)。
    返り値: (capped_confidence, 抑制理由 or '')。
    """
    ceiling = _SB_CEILING.get(source_basis_conf, "moderate")
    if _CONF_RANK[llm_conf] <= _CONF_RANK[ceiling]:
        return llm_conf, ""
    return ceiling, f"source_basis({source_basis_conf})上限により {llm_conf}→{ceiling} に抑制"


def has_strong_attribution(evidence: tuple[EvidenceItem, ...]) -> bool:
    """**leading 仮説を支持する**強い帰属根拠 (公式/ベンダ/研究/被害公表) が最低 1 つあるか。

    polarity は leading 仮説基準で付与される (prompt 修正後)。よって supports + 強 basis =
    「leading の帰属が一次/権威ソースで裏付く」を意味し、帰属系のどの仮説にも同義に効く。
    """
    return any(
        e.attribution_basis in STRONG_ATTRIBUTION and e.polarity == "supports" for e in evidence
    )


def attribution_confidence_cap(
    leading: str, evidence: tuple[EvidenceItem, ...]
) -> Confidence | None:
    """特定アクターを帰属する仮説が強い帰属根拠を欠くときの確度上限を返す (None=上限なし)。

    **方向中立**: threat 側 (organized_state_op) も benign-but-attributed 側
    (criminal_financial / hacktivism_influence) も等しく対象 (ATTRIBUTION_SENSITIVE)。
    「誰がやったか」を主張する以上、見合う帰属証拠 (公式/ベンダ/研究/被害公表) が無ければ
    高確度で断定しない、という evidence-strength 規律 (source_basis cap と同型)。
    **仮説選択は上書きしない** (ACH の証拠駆動判定を尊重)。組織的作戦も禁止せず過確信のみ防ぐ。
    commodity/accidental/reporting/unverified は帰属を主張しないので対象外。
    """
    if leading in ATTRIBUTION_SENSITIVE and not has_strong_attribution(evidence):
        return "low"
    return None


def evidence_ceiling(source_basis_conf: str, evidence: tuple[EvidenceItem, ...]) -> Confidence:
    """確度の上限を「証拠グレード」で決める (方向中立)。

    基本は source_basis (出典 outlet tier) 由来の上限。ただし **leading を支持する強帰属
    (govt/vendor/研究/被害公表) があれば一段引上げる** — news 媒体が政府公告/ベンダ確定帰属を
    *正確に報道* している場合、その事実の証拠グレードは媒体 tier でなく一次/ベンダ相当だから。
    引上げも leading 仮説は変えず確度上限のみ動かす (対称・evidence-strength)。
    """
    tier = _SB_CEILING.get(source_basis_conf, "moderate")
    return _STEP_UP[tier] if has_strong_attribution(evidence) else tier


def final_confidence(
    llm_conf: Confidence,
    source_basis_conf: str,
    leading: str,
    evidence: tuple[EvidenceItem, ...],
) -> tuple[Confidence, str]:
    """LLM の ACH 確度に方向中立な上限を適用する。

    上限 = evidence_ceiling (出典 tier + 強帰属による一段引上げ)。加えて帰属を主張する仮説が
    強帰属を欠くなら low に cap。いずれも「証拠が弱ければ *どの結論でも* 抑える」evidence-strength
    で、leading 仮説は変えない (ACH の証拠駆動判定を尊重)。返り値: (confidence, 理由)。
    """
    ceiling = evidence_ceiling(source_basis_conf, evidence)
    reasons: list[str] = []
    conf = llm_conf
    if _CONF_RANK[llm_conf] > _CONF_RANK[ceiling]:
        boost = "+強帰属" if has_strong_attribution(evidence) else ""
        reasons.append(
            f"証拠上限(source_basis={source_basis_conf}{boost})により{llm_conf}→{ceiling}"
        )
        conf = ceiling
    attr_cap = attribution_confidence_cap(leading, evidence)
    if attr_cap is not None and _CONF_RANK[attr_cap] < _CONF_RANK[conf]:
        reasons.append(f"帰属根拠不足により {conf}→{attr_cap} に抑制")
        conf = attr_cap
    return conf, "; ".join(reasons)
