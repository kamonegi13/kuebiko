"""段4/5: Estimate → StatusSynthesisRecord 射影 (報告は estimate の射影に徹する)。

- tradecraft は Estimate からの**決定論投影** (drift ゼロ・traceable)。
- narrative セクションは **Estimate のみを入力**に confidence を超える主張を禁じた制約付き LLM。
設計: docs/synthesis_reliability_redesign.md。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from pydantic import BaseModel, GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema

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
from src.tools.llm_schema import require_all_properties

_log = get_logger(__name__)
_TEMPERATURE = 0.2
#: 出力上限。A 層が増えた weekly/monthly は本文も長くなるため広げる — **JSON の途中閉じを
#: 防ぐのが目的** (max_tokens 到達 = structured 出力の破損)。daily は現行のまま。
_MAX_TOKENS = 6_000
_MAX_TOKENS_BY_PERIOD: dict[str, int] = {"daily": 6_000, "weekly": 10_000, "monthly": 10_000}
#: セクションあたりの文数指示 (period 別)。判定が多い期間ほど総括も長くなる。
_SECTION_SENTENCES: dict[str, tuple[int, int]] = {
    "daily": (2, 5),
    "weekly": (3, 8),
    "monthly": (3, 8),
}

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
    """LLM が返す narrative セクション (本番 schema)。

    既定値は Python 側の構築しやすさのために残し、**LLM へ渡す schema だけ全必須**に
    する (``require_all_properties``)。既定値持ち = required ゼロは、制約デコードで
    省略が文法上許され後発の欄から静かに落ちる条件そのものだった (2026-08-26 実測)。
    """

    model_config = {"extra": "ignore"}
    headline: str = ""
    weight_section: str = ""
    chain_section: str = ""
    cog_section: str = ""
    spillover_section: str = ""
    pir_section: str = ""

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        return require_all_properties(dict(handler(core_schema)))


class _WireSectionsCoT(BaseModel):
    """CoT (analysis_notes) つき schema。``SYNTHESIS_COT_NOTES=1`` のときだけ使う。

    ``analysis_notes`` を **先頭** に置くのが要件 — 制約デコードは schema の properties 順に
    生成するため、思考を本文より後ろに置くと「書いた後の後付け」にしかならない。
    ``_WireSections`` を継承しないのは pydantic が基底のフィールドを先に並べるためで、
    欄の同一性は ``tests/unit/test_synthesis_cot_notes.py`` が固定する。

    既定 OFF の理由: 本番 narrative を担う生徒は CoT を学習していない。教師収穫と、
    CoT を学習した生徒の配備までは本番の出力形を変えない (下流は常に ``_WireSections``)。
    """

    model_config = {"extra": "ignore"}
    analysis_notes: str = ""
    headline: str = ""
    weight_section: str = ""
    chain_section: str = ""
    cog_section: str = ""
    spillover_section: str = ""
    pir_section: str = ""

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        return require_all_properties(dict(handler(core_schema)))


#: 本文に載せる「変化した判定」の上限 (報告の幅)。台帳の更新上限とは**別の要求**なので
#: 別に持つ — 台帳は鮮度 (証拠をどれだけ早く消化するか)、こちらは読み物としての幅。
#: 落とした件数はプロンプトに明記する (no silent caps)。落ちた判定も PIR ロールアップには
#: 残るため、関心領域そのものが消えることはない。
#: daily=12: 実測 (2026-09-15、過去 73 窓) の moved は中央 5 件で、台帳 cap を 12 に上げても
#: 約 10 件。つまり通常は非発動の安全網。weekly/monthly は軌跡射影で中央 92/137 件を載せる
#: 設計 (prompt 40k/61k tok) であり、幅の再設計が済むまで**切らない** (切ると報告が壊れる)。
#: A 層 (本文) の選抜は **重要度基準** — 固定 N ではない。状況総括は「真に重要な事象を総括する」
#: ものなので、重要な事象が多い期間は本文も長くなってよい (2026-09-15 利用者指摘)。
#: salience が最高値の ``_BODY_SALIENCE_RATIO`` 以上の判定を全部 A 層に入れ、下限と上限で挟む。
#: 実測 (最高の 50% 以上): daily 2-4 件 / weekly 19-36 件 (中央 29) / monthly 28-46 件。
_BODY_SALIENCE_RATIO = 0.5
#: 下限 = これ未満には絞らない (daily は moved 中央 5 件なので実質「全件」になる)。
_MOVED_SECTION_MIN: dict[str, int] = {"daily": 12, "weekly": 12, "monthly": 15}
#: 上限 = 病的な期間での暴走を防ぐ安全弁 (40 件 × 1-2 文 ≒ 5,600 字で出力上限内)。
_MOVED_SECTION_MAX: dict[str, int] = {"daily": 12, "weekly": 40, "monthly": 40}

#: 継続中の判定 (standing) も **同じ重要度基準**で選ぶ (2026-09-15)。weekly では軌跡射影の
#: no_change 判定がそのまま載り 22 件 = 3.2k tok を占めていた。daily は上流
#: (``stateful._FALLBACK_STANDING``) で 3 件に絞られるため実質不変。
_STANDING_SECTION_MIN = 3
_STANDING_SECTION_MAX: dict[str, int] = {"daily": 3, "weekly": 12, "monthly": 12}

#: A 層 (本文) に PIR 保証で追加してよい上限。weekly を単純に上位 12 で切ると測定 11 窓
#: **すべて**で PIR が落ちた (pir_general_agency_alert ×9、pir_apt_attribution ×6 ほか) 一方、
#: 保証のコストは毎窓 +1-3 件と安い。daily は 73 窓中 1 窓しか落ちないので保証を持たない
#: (非対称は実測どおり — 要らない機構を足さない)。
_PIR_GUARANTEE_MAX: dict[str, int] = {"weekly": 6, "monthly": 6}

#: B 層 (1 行一覧) に載せる上限。1 行 ≒ 40 tok (A 層は根拠抜粋込みで ≒ 600 tok) なので、
#: 全件を「扱う」まま プロンプトを 40k → 14k tok 帯に収められる。超過分は C 層 = 件数のみ。
#: daily は moved 中央 5 件で一覧が無意味なため持たない (件数行のみの現行挙動を保つ)。
_LIST_MAX: dict[str, int] = {"weekly": 60, "monthly": 90}

#: CoT 欄の有効化 flag (既定 OFF)。収穫スクリプトが 1 を立てて教師の思考を捕獲する。
_COT_NOTES_ENV = "SYNTHESIS_COT_NOTES"


def cot_notes_enabled() -> bool:
    """analysis_notes (CoT) 欄を出力させるか。"""
    return os.environ.get(_COT_NOTES_ENV, "0") == "1"


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


#: B 層 1 行の claim 表示長。判定の claim は実測 100-200 字あり、そのまま並べると 1 行が
#: ~100 tok = A 層 (~450 tok) の 1/4 になってしまう。B 層の役目は「何がどちらへ動いたか」の
#: 提示だけなので、主語と対象が分かる長さで切る (分析は A 層に集中させる設計)。
_LINE_CLAIM_CHARS = 60


def _judgment_line(j: KeyJudgment) -> dict[str, Any]:
    """B 層の 1 行形 (証拠抜粋なし・claim も短縮 = A 層 ~450 tok に対し ~45 tok)。"""
    claim = j.claim.strip()
    return {
        "id": j.id,
        "delta_ja": _DELTA_JA.get(j.delta_type, j.delta_type),
        "claim": claim if len(claim) <= _LINE_CLAIM_CHARS else claim[:_LINE_CLAIM_CHARS] + "…",
        "leading_label": _hyp_label(j.leading_hypothesis),
        "confidence_ja": _CONF_JA.get(j.confidence, j.confidence),
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


@dataclass(frozen=True)
class RenderPlan:
    """render 段の決定論部分 (プロンプトと、コードが指名した headline)。

    LLM を呼ばずにプロンプトだけを再構築できるようにするための seam。凍結評価は
    **全腕を同じ再構築で測る**必要があり (2026-09-08 の時代混在の教訓)、本番と
    オフライン評価がこの 1 箇所を共有することでその不変量を構造で保つ。
    """

    prompt: str
    head: KeyJudgment | None
    mode: str
    ranked: tuple[KeyJudgment, ...]
    #: 報告の幅の内訳 (A 本文 / B 一覧 / C 件数のみ)。実装の検証と監査に使う。
    body_count: int = 0
    listed_count: int = 0
    omitted_count: int = 0


@lru_cache(maxsize=1)
def _standing_seed_ids() -> frozenset[str]:
    """常設情報要求 (``kind='standing'``) の situation id (``STANDING_SEEDS`` が SSoT)。

    これらは「国家 N は日本の重要インフラへの事前配置を進めているか」のような**常設の問い**で、
    台帳では dormant/close の対象外 (静穏期間こそ問いが生きる — 「静か≠安全」)。同じ理由で
    **報告の幅の上限からも除外する** — 静かな週に見えなくなるのでは常設である意味がない。
    """
    from src.assessment.standing import STANDING_SEEDS

    return frozenset(seed.situation_id for seed in STANDING_SEEDS)


def _keep_standing_seeds(
    kept: list[KeyJudgment], rest: list[KeyJudgment]
) -> tuple[list[KeyJudgment], list[KeyJudgment]]:
    """上限で落ちた常設情報要求を掲載側へ戻す (順序は salience 順のまま)。"""
    seeds = _standing_seed_ids()
    forced = [j for j in rest if j.id in seeds]
    if not forced:
        return kept, rest
    return [*kept, *forced], [j for j in rest if j.id not in seeds]


def _important_count(ranked: list[KeyJudgment], *, floor: int, cap: int) -> int:
    """載せる件数 = 重要度基準 (最高 salience の一定割合以上) を下限・上限で挟む。

    固定 N でないのは、状況総括が「真に重要な事象」を扱うものだから — 重要な事象が多い期間は
    本文もそれだけ長くなるのが正しい。下限は静穏期に総括が痩せないための床。
    ``ranked`` は salience 降順であること (先頭が最高値)。
    """
    from src.assessment.salience import salience

    if not ranked:
        return 0
    threshold = salience(ranked[0]) * _BODY_SALIENCE_RATIO
    important = sum(1 for j in ranked if salience(j) >= threshold)
    return max(floor, min(important, cap))


def _split_moved(
    moved_all: list[KeyJudgment], *, head: KeyJudgment | None, period: str
) -> tuple[list[KeyJudgment], list[KeyJudgment], int]:
    """変化した判定を A 本文 / B 一覧 / C 件数の 3 層に分ける (決定論)。

    全件を「扱う」が全件を「書かせない」— weekly は判定 92 件を出力 4,000 字に押し込んで
    列挙へ退化していた (2026-09-15 実測)。A だけが分析対象、B は存在と方向のみ、C は件数。

    A の構成順 (すべて決定論):
    1. salience 上位 ``_MOVED_SECTION_MAX``
    2. headline 指名判定を必ず含める (噂クラスが上位を埋めた日に指名だけ本文から消えるのを防ぐ)
    3. **PIR 保証** — A に無い PIR を持つ判定を salience 順に引き上げる (1 件が複数 PIR を
       満たしてよい)。追加順を PIR config 順でなく候補の salience 順にしているのは、
       PIR 優先度が salience の boost に既に入っており、順序の SSoT を 2 つ持たないため。
    """
    cap = _MOVED_SECTION_MAX.get(period)
    if cap is None:
        return moved_all, [], 0
    body = moved_all[
        : _important_count(moved_all, floor=_MOVED_SECTION_MIN.get(period, cap), cap=cap)
    ]
    if head is not None and head in moved_all and head not in body:
        body = [*body[: max(0, len(body) - 1)], head]

    shown = {j.id for j in body}
    rest = [j for j in moved_all if j.id not in shown]
    body, rest = _keep_standing_seeds(body, rest)
    guarantee = _PIR_GUARANTEE_MAX.get(period, 0)
    if guarantee:
        covered = {p for j in body for p in j.pir_ids}
        added: list[KeyJudgment] = []
        for j in rest:
            if len(added) >= guarantee:
                break
            new_pirs = set(j.pir_ids) - covered
            if new_pirs:
                added.append(j)
                covered |= new_pirs
        if added:
            body = [*body, *added]
            added_ids = {j.id for j in added}
            rest = [j for j in rest if j.id not in added_ids]

    list_max = _LIST_MAX.get(period)
    listed = rest[:list_max] if list_max is not None else []
    return body, listed, len(rest) - len(listed)


def build_render_plan(
    *, est: Estimate, period_label: str, cot_notes: bool | None = None
) -> RenderPlan:
    """Estimate から render プロンプトを組む (LLM 呼出なし)。

    ``cot_notes`` 未指定時は env flag (``SYNTHESIS_COT_NOTES``) に従う。
    """
    from src.assessment.salience import pick_headline, rank_judgments

    ranked = rank_judgments(est.judgments)
    head = pick_headline(est.judgments)
    moved_all = [j for j in ranked if j.delta_type not in ("", "no_change")]
    standing = [j for j in ranked if j.delta_type in ("", "no_change")]
    moved, moved_list, moved_omitted = _split_moved(moved_all, head=head, period=est.period_type)
    standing_cap = _STANDING_SECTION_MAX.get(est.period_type)
    standing_omitted = 0
    if standing_cap is not None:
        keep = _important_count(standing, floor=_STANDING_SECTION_MIN, cap=standing_cap)
        kept, dropped = _keep_standing_seeds(standing[:keep], standing[keep:])
        kept_ids = {j.id for j in kept}
        standing_omitted = len(dropped)
        standing = [j for j in standing if j.id in kept_ids]
    if moved_list or moved_omitted:
        _log.info(
            "synthesis_moved_section_capped",
            period_type=est.period_type,
            moved=len(moved_all),
            body=len(moved),
            listed=len(moved_list),
            omitted=moved_omitted,
            standing=len(standing),
            standing_omitted=standing_omitted,
        )
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
        moved_list=[_judgment_line(j) for j in moved_list],
        moved_omitted=moved_omitted,
        section_min_sentences=_SECTION_SENTENCES.get(est.period_type, (2, 5))[0],
        section_max_sentences=_SECTION_SENTENCES.get(est.period_type, (2, 5))[1],
        standing=[_judgment_view(j) for j in standing],
        standing_omitted=standing_omitted,
        pir_rollup=_pir_rollup(est.judgments),
        relation_lines=relation_lines,
        cot_notes=cot_notes_enabled() if cot_notes is None else cot_notes,
    )
    return RenderPlan(
        prompt=prompt,
        head=head,
        mode=mode,
        ranked=tuple(ranked),
        body_count=len(moved),
        listed_count=len(moved_list),
        omitted_count=moved_omitted,
    )


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
    if not est.judgments:
        return _WireSections(headline="本期間に確度ある主要判定は得られなかった。")
    cot = cot_notes_enabled()
    plan = build_render_plan(est=est, period_label=period_label, cot_notes=cot)
    head, mode, ranked = plan.head, plan.mode, list(plan.ranked)
    schema: type[_WireSections] | type[_WireSectionsCoT] = (
        _WireSectionsCoT if cot else _WireSections
    )
    raw = await llm.generate_structured(
        plan.prompt,
        schema,
        temperature=_TEMPERATURE,
        max_tokens=_MAX_TOKENS_BY_PERIOD.get(est.period_type, _MAX_TOKENS),
        think=False,
    )
    if isinstance(raw, _WireSectionsCoT):
        # 下流 (射影・保存・表示) は CoT 欄を知らない。長さだけ観測に残す
        # (「欄は作ったが空だった」を後から数えられるようにする)。
        _log.info("synthesis_cot_notes", chars=len(raw.analysis_notes.strip()))
        sections = _WireSections.model_validate(raw.model_dump())
    else:
        sections = raw
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
