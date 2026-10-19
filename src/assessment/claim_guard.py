"""台帳の claim に対する決定論の関門と、反転確定時の claim 書き直し。

2 つの不具合の再発防止 (2026-10):

1. 見立て (hypothesis) が反転確定しても claim が前回のまま残る — 増分 ACH は「答えが変わらない
   なら前回をそのまま返してよい」と指示されており、見立てだけ動いて結論文が追従しなかった
   (delta_note に「結論文は未改訂」と注記されるだけだった)。反転が確定し claim が同一なら、
   新しい見立てのラベルを渡して claim だけを書き直す LLM 呼出しを 1 回足す。
2. 問いの主語 (例: 日本) を落とし、他国の事例を答えにする — 問いが対象国を持つのに新 claim が
   その国に言及しないなら、新 claim を採らず前回 claim を保持する。

国の判定は既存の SSoT を使う (日本 = ``japan_claim_relevance``、国 = ``nations_in_text``
= config/cti/countries.yaml)。複製辞書は持たない。LLM 失敗は例外にせず従来どおり保存させる。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from pydantic import BaseModel

from src.logging_config import get_logger

if TYPE_CHECKING:
    from src.assessment.situation_store import RevisionRow
    from src.synthesis.grounded.estimate import KeyJudgment
    from src.tools.llm_client import LLMClient

_log = get_logger(__name__)

REWRITE_FLAG_ENV = "LEDGER_CLAIM_REWRITE_ON_FLIP"
SUBJECT_GATE_ENV = "LEDGER_CLAIM_SUBJECT_GATE"
_TEMPERATURE = 0.2
_MAX_TOKENS = 600
_MAX_EVIDENCE_SHOWN = 4
_EXCERPT_CHARS = 120
_JAPAN = "JP"


# claim に混ざる台帳内部の文 (新着・期間・記事・ソースへの言及)。答えは問いの主語について
# 述べるもので、評価の根拠は implication / missing_evidence に書く。「直接証拠は観測されて
# いない」等の正当な表現は含まない語だけで組む (広い「証拠は」は使わない)。
_INTERNAL_CLAIM_RE = re.compile(
    r"今回の新着|新着ソース|新着の|新着で|新着証拠|新着記事|本期間|今期|今回の観測|記事は|ソースは|経験談"
)


def internal_claim_text(claim: str) -> str:
    """claim に台帳内部の文の語が含まれれば最初の一致語、無ければ空文字。"""
    m = _INTERNAL_CLAIM_RE.search(claim)
    return m.group(0) if m else ""


CLAIM_RESET_PLACEHOLDER = "(前回の答えは破棄済み。問いへの答えを新たに 1 文で書く)"
RESET_FLAG_ENV = "LEDGER_CLAIM_INTERNAL_RESET"


def claim_reset_enabled() -> bool:
    """常設の問いの claim に内部の文が混ざっていたら前回の claim を渡さない (既定 on、0 で無効)。"""
    return os.environ.get(RESET_FLAG_ENV, "1").strip() != "0"


def log_internal_claim(j: KeyJudgment) -> None:
    """記録だけの検査 (shadow)。保存は止めない。混入率を見て関門化を判断する。"""
    term = internal_claim_text(j.claim)
    if term:
        _log.warning("claim_internal_text", situation=j.id, term=term)


class _WireClaim(BaseModel):
    model_config = {"extra": "ignore"}
    claim: str = ""


@dataclass(frozen=True)
class GuardResult:
    """関門・書き直しを通した判定と、delta_note に足す注記 (無ければ空)。"""

    judgment: KeyJudgment
    notes: tuple[str, ...] = ()


def rewrite_enabled() -> bool:
    """flag ``LEDGER_CLAIM_REWRITE_ON_FLIP`` (既定 on、0 で従来どおり)。"""
    return os.environ.get(REWRITE_FLAG_ENV, "1").strip() != "0"


def subject_gate_mode() -> str:
    """``LEDGER_CLAIM_SUBJECT_GATE`` (既定 shadow)。未知の値は shadow として扱う。

    shadow = 発火を注記とログに残すだけで新 claim を採る。既定を shadow にした理由: 導入時
    (2026-10-12) の過去 30 日で、昇格した閾値型の問いは改訂の 6〜8 割が日本を明記していなかった
    (事象用のプロンプトで評価されていた期間)。問い用のプロンプトに切り替えた後の発火率を
    見てから on にする — いきなり on だと結論の文が長期間固まる。
    """
    mode = os.environ.get(SUBJECT_GATE_ENV, "shadow").strip().lower()
    return mode if mode in ("on", "shadow", "off") else "shadow"


def question_subject_nations(question: str) -> frozenset[str]:
    """問いが明示する対象国 (ISO2)。日本が出てくる問いは日本を対象とする (運用の守る側)。

    対象国を持たない問いは空集合 = 関門は何もしない。
    """
    from src.cti.nation_gazetteer import nations_in_text

    nations = nations_in_text(question)
    return frozenset({_JAPAN}) if _JAPAN in nations else nations


def claim_mentions_subject(claim: str, subjects: frozenset[str]) -> bool:
    """claim が対象国のいずれかに言及するか (日本は japan_relevance の SSoT)。"""
    from src.cti.japan_relevance import japan_claim_relevance
    from src.cti.nation_gazetteer import nations_in_text

    if _JAPAN in subjects and japan_claim_relevance(claim, ()) == "party":
        return True
    return bool(subjects - {_JAPAN} & nations_in_text(claim))


def apply_subject_gate(
    j: KeyJudgment, prev: RevisionRow | None, question: str
) -> tuple[KeyJudgment, str]:
    """問いの主語を落とした新 claim は採らず前回 claim を保持する。返り値 = (判定, 注記)。"""
    mode = subject_gate_mode()
    subjects = question_subject_nations(question) if question and mode != "off" else frozenset()
    if not subjects or claim_mentions_subject(j.claim, subjects):
        return j, ""
    if prev is None:
        return j, "claim が問いの対象国に言及しない (初回のため採用)"
    if j.claim.strip() == prev.claim.strip():
        return j, ""
    if mode == "shadow":
        _log.warning("claim_subject_gate_shadow", situation=j.id, subjects=sorted(subjects))
        return j, "claim が問いの対象国に言及しない (記録のみ)"
    _log.warning("claim_subject_gate_held", situation=j.id, subjects=sorted(subjects))
    return replace(j, claim=prev.claim), "claim が問いの対象国に言及せず前回を保持"


def _label(hypothesis_id: str) -> str:
    from src.synthesis.grounded.hypotheses import get_hypothesis

    hyp = get_hypothesis(hypothesis_id)
    return hyp.label if hyp else hypothesis_id


def _render_prompt(j: KeyJudgment, prev: RevisionRow, title: str, question: str) -> str:
    from src.synthesis.grounded.passes import _render

    return _render(
        "synthesis/claim_rewrite.j2",
        situation_title=title,
        question=question,
        prev_label=_label(prev.leading_hypothesis),
        new_label=_label(j.leading_hypothesis),
        confidence=j.confidence,
        prev_claim=prev.claim,
        evidence=[
            e.excerpt[:_EXCERPT_CHARS] for e in j.evidence if e.polarity == "supports" and e.excerpt
        ][:_MAX_EVIDENCE_SHOWN],
    )


async def rewrite_claim_on_flip(
    *,
    llm: LLMClient,
    j: KeyJudgment,
    prev: RevisionRow,
    title: str,
    question: str,
) -> str:
    """反転確定後の claim を書き直す。失敗・空・不正・無変化は空文字 (= 従来どおり保存)。"""
    from src.synthesis.grounded.incremental import is_sane_japanese_claim

    try:
        wire = await llm.generate_structured(
            _render_prompt(j, prev, title, question),
            _WireClaim,
            temperature=_TEMPERATURE,
            max_tokens=_MAX_TOKENS,
            think=False,
            max_attempts=1,
        )
    except Exception as e:  # noqa: BLE001 — 台帳更新を止めない
        _log.warning("claim_rewrite_failed", situation=j.id, error=type(e).__name__)
        return ""
    revised = wire.claim.strip()
    if not revised or revised == prev.claim.strip() or not is_sane_japanese_claim(revised):
        _log.warning("claim_rewrite_rejected", situation=j.id)
        return ""
    return revised


async def guard_claim(
    *,
    llm: LLMClient,
    j: KeyJudgment,
    prev: RevisionRow | None,
    question: str,
    title: str,
    flip_confirmed: bool,
) -> GuardResult:
    """主語の関門 → (反転確定かつ claim 同一なら) 書き直し → 書き直し結果にも関門。"""
    notes: list[str] = []
    j, gate_note = apply_subject_gate(j, prev, question)
    if gate_note:
        notes.append(gate_note)
    log_internal_claim(j)
    if (
        prev is None
        or not flip_confirmed
        or not rewrite_enabled()
        or j.claim.strip() != prev.claim.strip()
    ):
        return GuardResult(j, tuple(notes))
    revised = await rewrite_claim_on_flip(llm=llm, j=j, prev=prev, title=title, question=question)
    if not revised:
        return GuardResult(j, tuple(notes))
    candidate, rewrite_gate = apply_subject_gate(replace(j, claim=revised), prev, question)
    if rewrite_gate:
        notes.append(rewrite_gate)
    if candidate.claim != revised:  # 関門 (on) が書き直しを退けた
        return GuardResult(j, tuple(notes))
    return GuardResult(candidate, (*notes, "結論文を見立て反転に合わせて書き直し"))
