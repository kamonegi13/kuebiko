"""n20 T5 のテスト用の固定入力 (旗 off の同一性・新しい描画の両方で使う)。LLM は呼ばない。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.eventnews.models import MemberArticle
from src.synthesis.grounded.estimate import Estimate, EvidenceItem, HypothesisScore, KeyJudgment

BASE_TS = datetime(2026, 10, 20, 0, 0, tzinfo=UTC)

BODY_1 = (
    "Researchers said the attackers allegedly exploited a flaw in the VPN appliance. "
    "The vendor confirmed that versions up to 9.1 are affected and released a patch. "
    "Around 1,200 devices were exposed according to a scan by the researchers."
)
BODY_2 = (
    "The agency reportedly linked the intrusion to a known group, but the attribution "
    "has not been confirmed. The campaign targeted manufacturing firms in Japan."
)


def member(article_id: str, body: str, *, hours: int = 0) -> MemberArticle:
    return MemberArticle(
        article_id=article_id,
        title=f"title-{article_id}",
        url=f"https://kuebiko.example/{article_id}",
        feed_title="Example Feed",
        feed_url="https://kuebiko.example/feed",
        host="kuebiko.example",
        importance="high",
        category="vuln",
        status="posted",
        anchor_ts=BASE_TS + timedelta(hours=hours),
        summary="summary",
        body=body,
        entities=frozenset(),
    )


def members() -> list[MemberArticle]:
    return [member("a1", BODY_1), member("a2", BODY_2, hours=1)]


ALLOWED = "I1 = CVE-2026-0001 (記事 [1])"


def judgment(
    jid: str,
    *,
    confidence: str = "moderate",
    delta: str = "opened",
    refuted: bool = False,
    viable_counter: bool = True,
) -> KeyJudgment:
    hyps = [HypothesisScore("organized_state_op", 3, 0, "leading")]
    if viable_counter:
        hyps.append(HypothesisScore("opportunistic_commodity", 1, 1, "viable"))
    return KeyJudgment(
        id=jid,
        claim=f"{jid} の主張: 製造業を狙う侵入が続いている",
        domain="cyber_incident",
        leading_hypothesis="organized_state_op",
        confidence=confidence,  # type: ignore[arg-type]
        confidence_basis="ACH",
        hypotheses=tuple(hyps),
        evidence=(
            EvidenceItem("a1", "research", "vendor_confirmed", "侵入が観測された", "supports"),
        ),
        indicators=("同種の侵入の再発",),
        adversarial_refuted=refuted,
        adversarial_note="標的の偏りは偶然の可能性" if refuted else "",
        implication="国内の製造業でも警戒が要る",
        delta_type=delta,
        delta_note="新たな報告があった" if delta not in ("", "no_change") else "",
        fired_indicators=("同種の侵入の再発",) if delta == "escalated" else (),
    )


def estimate() -> Estimate:
    return Estimate(
        period_type="daily",
        period_start=BASE_TS,
        period_end=BASE_TS + timedelta(days=1),
        judgments=(
            judgment("s-aaa", confidence="high", delta="escalated"),
            judgment("s-bbb", confidence="low", delta="opened", refuted=True),
            judgment("s-ccc", confidence="moderate", delta="no_change", viable_counter=False),
        ),
        model="fake",
    )
