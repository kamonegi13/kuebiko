"""段4/5: Estimate → StatusSynthesisRecord 射影 (報告は estimate の射影に徹する)。

- tradecraft は Estimate からの**決定論投影** (drift ゼロ・traceable)。
- narrative セクションは **Estimate のみを入力**に confidence を超える主張を禁じた制約付き LLM。
設計: docs/synthesis_reliability_redesign.md。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from src.logging_config import get_logger
from src.storage.run_history import StatusSynthesisRecord
from src.synthesis.grounded.estimate import (
    STRONG_ATTRIBUTION,
    Estimate,
    KeyJudgment,
    estimate_to_dict,
)
from src.synthesis.grounded.hypotheses import get_hypothesis
from src.synthesis.grounded.passes import _render
from src.tools.llm_client import LLMClient

_log = get_logger(__name__)
_TEMPERATURE = 0.2
_MAX_TOKENS = 6_000

_CONF_JA: dict[str, str] = {"high": "高確度", "moderate": "中確度", "low": "低確度"}


def _hyp_label(hid: str) -> str:
    h = get_hypothesis(hid)
    return h.label if h else hid


def _dedup(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        s = it.strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _counter(j: KeyJudgment) -> str:
    """judgment の最有力対立仮説 (viable で leading でない) を 1 つ言語化。"""
    for h in j.hypotheses:
        if h.verdict == "viable" and h.hypothesis != j.leading_hypothesis:
            return f"{_hyp_label(h.hypothesis)}の可能性"
    return ""


def _source_caveat(est: Estimate) -> str:
    # 状態分離 (2026-07-16): evidence は ACH 評価済みのみ (割当だけの行は混ざらない)。
    # 未評価の割当分は件数として正直に併記する (接地証拠に水増ししない)。
    evidence = [e for j in est.judgments for e in j.evidence]
    unassessed = sum(j.unassessed_count for j in est.judgments)
    unassessed_note = f"、ほかに未評価の割当記事 {unassessed} 件" if unassessed else ""
    if not evidence:
        return f"本評価は接地証拠が乏しく、確度は保守的に較正した{unassessed_note}。"
    strong_tier = sum(1 for e in evidence if e.source_tier in ("official", "research"))
    weak_tier = sum(1 for e in evidence if e.source_tier in ("social", "state_media", "unknown"))
    # 報道 tier でなく帰属根拠が強い証拠 (確度上限を一段引上げる根拠)。
    strong_attr = sum(
        1
        for e in evidence
        if e.attribution_basis in STRONG_ATTRIBUTION and e.polarity == "supports"
    )
    attr_note = (
        f"うち強帰属(政府/ベンダ/研究/被害公表) {strong_attr} 件"
        if strong_attr
        else "強帰属(政府/ベンダ/研究確認)なし"
    )
    considered = f"考慮 {est.considered_count} 記事中 " if est.considered_count else ""
    return (
        f"{considered}接地証拠 {len(evidence)} 件 (一次/研究 {strong_tier} 件、"
        f"SNS/国営/不明 {weak_tier} 件、{attr_note}{unassessed_note})。"
        "確度は最弱ソースで較正し、強帰属を伴う判定のみ報道 tier を一段引上げた。"
    )


def project_tradecraft(est: Estimate, forecast_ctx: dict[str, Any] | None = None) -> dict[str, Any]:
    """Estimate → tradecraft (決定論投影)。alternatives は ACH の実競合仮説 + 検証反証。

    forecast_ctx (監査 2026-07-05 P4): forecast lifecycle の射影
    (forecasts/scorecard/alignment/freshness)。台帳非稼働時は None = 従来の空。
    """
    leading_lines = [
        f"{j.claim} → {_hyp_label(j.leading_hypothesis)}"
        f"({_CONF_JA.get(j.confidence, j.confidence)})"
        + (
            f" [{_DELTA_JA.get(j.delta_type, j.delta_type)}]"
            if j.delta_type not in ("", "no_change")
            else ""
        )
        for j in est.judgments
    ]
    alts: list[str] = []
    for j in est.judgments:
        c = _counter(j)
        if c:
            alts.append(f"[{j.id}] {_hyp_label(j.leading_hypothesis)}でなく{c}")
        if j.adversarial_refuted and j.adversarial_note:
            alts.append(f"[{j.id} 検証] {j.adversarial_note}")
    return {
        "leading_assessment": " / ".join(leading_lines),
        "alternatives": _dedup(alts)[:6],
        "key_assumptions": _dedup([a for j in est.judgments for a in j.key_assumptions])[:5],
        "indicators": _dedup([i for j in est.judgments for i in j.indicators])[:5],
        "missing_evidence": _dedup([m for j in est.judgments for m in j.missing_evidence])[:5],
        "source_caveat": _source_caveat(est),
        "forecast_alignment": str((forecast_ctx or {}).get("forecast_alignment", "")),
        "freshness_note": str((forecast_ctx or {}).get("freshness_note", "")),
        "forecasts": list((forecast_ctx or {}).get("forecasts", [])),
        "forecast_scorecard": list((forecast_ctx or {}).get("forecast_scorecard", [])),
    }


class _WireSections(BaseModel):
    model_config = {"extra": "ignore"}
    headline: str = ""
    weight_section: str = ""
    chain_section: str = ""
    cog_section: str = ""
    spillover_section: str = ""
    pir_section: str = ""


# 段C: delta の日本語ラベル (射影用の決定論マッピング)。
_DELTA_JA: dict[str, str] = {
    "opened": "新規",
    "hypothesis_flip": "見立て転換",
    "strengthened": "強化",
    "weakened": "後退",
    "escalated": "拡大",
    "reopened": "再燃",
    "claim_revised": "更新",
    "closing": "収束",
    "no_change": "継続",
    "": "",
}

# headline は朝刊の太字先頭行 = 最も読まれる 1 行。claim の言い換え程度 (実測 48-61 字) では
# BLUF (変化 + 要点 + 含意 + 確度) を運べないため、floor 未満は台帳 field から決定論合成する。
_HEADLINE_MIN_CHARS = 70


def _headline_mode(head: KeyJudgment | None) -> str:
    """headline の書式モード (決定論指名): moved=変化あり / quiet=台帳静穏 / plain=delta 未追跡。

    quiet と plain の区別は正直さの問題 — delta 未追跡 (rollback 経路) の期間に
    「変化なし」と主張してはならない (追跡していないだけで、変化が無かった保証はない)。
    """
    if head is None or not head.delta_type:
        return "plain"
    if head.delta_type == "no_change":
        return "quiet"
    return "moved"


def _compose_headline(head: KeyJudgment, mode: str) -> str:
    """floor 未満の headline を台帳 field のみから合成する (新規主張ゼロの決定論 fallback)。"""
    conf = _CONF_JA.get(head.confidence, head.confidence)
    label = _hyp_label(head.leading_hypothesis)
    if mode == "quiet":
        lead = (
            f"本期間、確度をもって報告できる大きな変化はない。"
            f"継続する最重要判定: {head.claim} ({label}、{conf})。"
        )
    else:
        delta = _DELTA_JA.get(head.delta_type, head.delta_type)
        prefix = f"【{delta}】" if mode == "moved" and delta else ""
        note = f" — {head.delta_note}" if mode == "moved" and head.delta_note else ""
        lead = f"{prefix}{head.claim}{note}。見立て: {label} ({conf})。"
    return lead + head.implication if head.implication else lead


def _next_grounded_candidate(ranked: list[KeyJudgment], head: KeyJudgment) -> KeyJudgment | None:
    """headline 筆頭以外で接地ゲートを通る salience 次点 (反復抑制の「次いで注視」用)。"""
    from src.assessment.salience import is_headline_grounded

    for j in ranked:
        if j.id != head.id and is_headline_grounded(j):
            return j
    return None


def _compose_quiet_repeat_headline(head: KeyJudgment, nxt: KeyJudgment | None) -> str:
    """静穏日に同一 standing 判定が連日 headline に立つ場合の決定論合成 (2026-08-07)。

    実測 (07-08/09、07-18/19) で quiet 日の headline が前日とほぼ同文で再掲され、
    読者に「また同じ見出し」の既視感と話題偏りの誤知覚を与えていた。同一判定の
    再掲時は「前日から継続」を明示して短縮し、salience 次点の判定を「次いで注視」
    として立てる — 継続表示の正直さ (最重要判定は変えない) と新規性を両立する。
    """
    conf = _CONF_JA.get(head.confidence, head.confidence)
    lead = (
        f"本期間も確度をもって報告できる大きな変化はない"
        f" (最重要判定は前日から継続: {head.claim} [{conf}])。"
    )
    if nxt is None:
        return lead + head.implication if head.implication else lead
    n_conf = _CONF_JA.get(nxt.confidence, nxt.confidence)
    n_label = _hyp_label(nxt.leading_hypothesis)
    tail = f"次いで注視: {nxt.claim} ({n_label}、{n_conf})。"
    return lead + tail + nxt.implication if nxt.implication else lead + tail


def _guard_headline(sections: _WireSections, head: KeyJudgment | None, mode: str) -> _WireSections:
    """headline の決定論 floor ガード (LLM 自由文への sanity ガードの一環)。"""
    if head is None or len(sections.headline.strip()) >= _HEADLINE_MIN_CHARS:
        return sections
    _log.warning(
        "synthesis_headline_below_floor",
        headline_chars=len(sections.headline.strip()),
        floor=_HEADLINE_MIN_CHARS,
        judgment_id=head.id,
        mode=mode,
    )
    return sections.model_copy(update={"headline": _compose_headline(head, mode)})


def _judgment_view(j: KeyJudgment) -> dict[str, Any]:
    return {
        "id": j.id,
        "claim": j.claim,
        "leading_label": _hyp_label(j.leading_hypothesis),
        "confidence_ja": _CONF_JA.get(j.confidence, j.confidence),
        "adversarial_refuted": j.adversarial_refuted,
        "evidence_excerpts": [e.excerpt for e in j.evidence[:3] if e.excerpt],
        "counter": _counter(j),
        "missing": "; ".join(j.missing_evidence[:2]),
        # 段C: delta (変化の言語) と含意・指標を射影に供給
        "delta_ja": _DELTA_JA.get(j.delta_type, j.delta_type),
        "delta_note": j.delta_note,
        "implication": j.implication,
        "fired_indicators": list(j.fired_indicators),
        "indicators": list(j.indicators[:2]),
    }


def _pir_titles() -> dict[str, str]:
    """pir_id → title (PIR rollup の決定論整形用)。設定不在時は空 dict。"""
    try:
        from src.pir.integration import get_pir_config

        return {p.id: p.title for p in get_pir_config().priorities}
    except Exception:  # noqa: BLE001 — PIR 不在でも render は動かす
        return {}


def _pir_rollup(judgments: tuple[KeyJudgment, ...]) -> list[dict[str, Any]]:
    """PIR 別ロールアップ (決定論): pir_id → 関連判定 (claim/確度/含意)。"""
    titles = _pir_titles()
    by_pir: dict[str, list[KeyJudgment]] = {}
    for j in judgments:
        for pid in j.pir_ids:
            by_pir.setdefault(pid, []).append(j)
    out: list[dict[str, Any]] = []
    for pid, js in sorted(by_pir.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        out.append(
            {
                "pir_title": titles.get(pid, pid),
                # NOTE: key 名は "items" 不可 (Jinja の属性解決が dict.items メソッドに化ける)
                "entries": [
                    {
                        "claim": j.claim,
                        "confidence_ja": _CONF_JA.get(j.confidence, j.confidence),
                        "implication": j.implication,
                    }
                    for j in js[:3]
                ],
            }
        )
    return out[:6]


async def render_sections(
    *,
    llm: LLMClient,
    est: Estimate,
    period_label: str,
    prev_headline_judgment_id: str | None = None,
) -> _WireSections:
    """Estimate のみを入力に、制約付きで narrative セクションを LLM render する。

    段C: 判定は salience 決定論順、headline 対象もコードが指名する (LLM は順位を選ばない)。
    新規/変化/継続のグルーピングも決定論 (delta_type) — LLM は変化の散文化のみ。
    ``prev_headline_judgment_id`` = 前回 (daily は前日) の headline に立った判定 id。
    quiet 日に同一判定が再掲される場合は決定論の継続表記へ置き換える (反復抑制)。
    """
    from src.assessment.salience import pick_headline, rank_judgments

    if not est.judgments:
        return _WireSections(headline="本期間に確度ある主要判定は得られなかった。")
    ranked = rank_judgments(est.judgments)
    head = pick_headline(est.judgments)
    moved = [j for j in ranked if j.delta_type not in ("", "no_change")]
    standing = [j for j in ranked if j.delta_type in ("", "no_change")]
    # 段D: 関係エッジ (決定論・共有 anchor 由来) を chain セクションの事実供給にする
    claim_by_id = {j.id: j.claim for j in est.judgments}
    rel_ja = {
        "same_actor": "同一アクター",
        "same_campaign": "同一作戦",
        "shared_nation": "国家の共有",
    }
    relation_lines = [
        f"「{claim_by_id[a][:40]}」↔「{claim_by_id[b][:40]}」: {rel_ja.get(t, t)} ({basis})"
        for a, b, t, basis in est.relations
        if a in claim_by_id and b in claim_by_id
    ][:8]
    mode = _headline_mode(head)
    prompt = _render(
        "synthesis/render.j2",
        period_label=period_label,
        headline_id=head.id if head else "",
        headline_view=_judgment_view(head) if head else None,
        headline_mode=mode,
        moved=[_judgment_view(j) for j in moved],
        standing=[_judgment_view(j) for j in standing],
        pir_rollup=_pir_rollup(est.judgments),
        relation_lines=relation_lines,
    )
    sections = await llm.generate_structured(
        prompt, _WireSections, temperature=_TEMPERATURE, max_tokens=_MAX_TOKENS, think=False
    )
    sections = _guard_headline(sections, head, mode)
    # 反復抑制 (2026-08-07): daily の quiet 日に前日と同一の standing 判定が headline へ
    # 再掲される場合、決定論の「前日から継続 + 次いで注視」合成に置き換える。
    # moved (実変化) の連日報告は情報価値があるため対象外。
    if (
        est.period_type == "daily"
        and mode == "quiet"
        and head is not None
        and prev_headline_judgment_id is not None
        and head.id == prev_headline_judgment_id
    ):
        sections = sections.model_copy(
            update={
                "headline": _compose_quiet_repeat_headline(
                    head, _next_grounded_candidate(ranked, head)
                )
            }
        )
    return sections


async def render_record(
    *,
    llm: LLMClient,
    est: Estimate,
    period_label: str,
    article_count: int = 0,
    forecast_ctx: dict[str, Any] | None = None,
    prev_headline_judgment_id: str | None = None,
) -> StatusSynthesisRecord:
    """Estimate を StatusSynthesisRecord に射影 (後方互換のため既存スキーマに乗せる)。

    tradecraft=決定論投影、sections=制約付き LLM render。canonical な estimate 本体は
    別途 estimate JSONB に保存する (段5)。axes_evidence は grounded では空 (UI は estimate を見る)。
    """
    from src.assessment.salience import pick_headline as _pick

    sections = await render_sections(
        llm=llm,
        est=est,
        period_label=period_label,
        prev_headline_judgment_id=prev_headline_judgment_id,
    )
    tradecraft = project_tradecraft(est, forecast_ctx=forecast_ctx)
    # canonical estimate を tradecraft に埋め込む (schema 変更回避。段7 UI が ACH/証拠を表示)。
    tradecraft["grounded_estimate"] = estimate_to_dict(est)
    # 反復抑制用のメタ (schema 変更回避で tradecraft に埋める): 次回生成が
    # 「前回どの判定が headline に立ったか」を参照する。
    _head = _pick(est.judgments)
    if _head is not None:
        tradecraft["headline_judgment_id"] = _head.id
    return StatusSynthesisRecord(
        period_type=est.period_type,
        period_start=est.period_start,
        period_end=est.period_end,
        headline=sections.headline.strip() or "(見出しなし)",
        weight_section=sections.weight_section.strip(),
        chain_section=sections.chain_section.strip(),
        cog_section=sections.cog_section.strip(),
        spillover_section=sections.spillover_section.strip(),
        pir_section=sections.pir_section.strip(),
        axes_evidence="{}",
        tradecraft=json.dumps(tradecraft, ensure_ascii=False),
        article_count=article_count,
        llm_model=est.model or None,
        generated_at=datetime.now(UTC),
    )
