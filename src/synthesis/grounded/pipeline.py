"""Grounded synthesis オーケストレーション: 段0 → 段1/2 → canonical Estimate。

period から high-importance 集約を取り、claim ノミネート → 各 claim を本文接地 ACH にかけ、
方向中立な確度 cap (source_basis + 帰属) を適用して KeyJudgment を組む。
段3 adversarial 検証 / 段5 narrative 射影 / 永続化 は後続段で追加する。
設計: docs/synthesis_reliability_redesign.md。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from src.synthesis.generator import SynthesisGenerationResult

from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository
from src.synthesis.grounded.adversary import adversarial_review, apply_adversarial
from src.synthesis.grounded.chronology import article_chronology
from src.synthesis.grounded.clustering import anchor_entities, expand_claim_sources
from src.synthesis.grounded.estimate import Estimate, KeyJudgment, final_confidence
from src.synthesis.grounded.passes import ground_and_score, nominate_claims, truncate_body
from src.synthesis.grounded.render import render_record
from src.tools.llm_client import LLMClient

_log = get_logger(__name__)
_DEFAULT_DB = Path("data/run_history.db")
# key judgment 数を期間でスケール (積極的被覆: 1 日の情勢を漏らさない)。
_K_BY_PERIOD: dict[str, int] = {"daily": 6, "weekly": 10, "monthly": 12}
# ノミネート・プールの記事数上限 (high+medium)。high のみだと各事案が single-source になり
# ACH (対立仮説の反証採点) が退化するため medium の続報・裏取りまで届ける (period でスケール)。
_POOL_BY_PERIOD: dict[str, int] = {"daily": 150, "weekly": 250, "monthly": 300}
_POOL_IMPORTANCE: tuple[str, ...] = ("high", "medium")
_MAX_SOURCES_PER_CLAIM = 8  # 1 claim の接地に読む本文の上限 (複数ソースで ACH を機能させる)
# 過去文脈: 対象期間より前の同一 entity 記事を ACH 証拠に引く窓 (period スケール)。
# インテリジェンスは過去 (前例/パターン/新規性) を証拠にする。ノミネートは対象期間のまま。
_CONTEXT_WINDOW_HOURS: dict[str, int] = {
    "daily": 90 * 24,
    "weekly": 180 * 24,
    "monthly": 365 * 24,
}
_MAX_HISTORICAL_PER_CLAIM = 3  # 過去文脈ソースの上限/claim (現在の事象を主に、過去は補助)
_HISTORICAL_BODY_CHARS = 1500  # 過去ソースは短く切る (パターン signal で十分)
_GROUNDED_ENV = "SYNTHESIS_GROUNDED"


def grounded_mode() -> Literal["off", "on", "shadow"]:
    """証拠駆動 synthesis の有効化モード (既定 off = 現行 single-pass)。

    1/true → on (新パイプラインを採用)、shadow → 旧出力は維持しつつ grounded を並走生成して
    log 比較 (品質検証用)。algorithm flag ゆえ env が適切 (ROUTING_RULES_ENGINE と同型)。
    """
    v = os.environ.get(_GROUNDED_ENV, "0").strip().lower()
    if v == "shadow":
        return "shadow"
    return "on" if v in ("1", "true", "yes", "on") else "off"


async def build_estimate(
    *,
    llm: LLMClient,
    period_type: str,
    repo: RunHistoryRepository,
    now: datetime | None = None,
    k: int | None = None,
    db_path: Path = _DEFAULT_DB,
    kev_set: frozenset[str] | None = None,
) -> Estimate:
    """証拠接地 ACH を回して期間の canonical Estimate を組み立てる。"""
    from src.cti.source_basis import classify_source_tier, compute_source_basis
    from src.synthesis.generator import _build_high_importance_cross_axis, _resolve_period

    start, end, label, lookback, _bw = _resolve_period(period_type=period_type, now=now)
    pool_limit = _POOL_BY_PERIOD.get(period_type, 150)
    articles = _build_high_importance_cross_axis(
        lookback_hours=lookback,
        db_path=db_path,
        limit=pool_limit,
        importance_levels=_POOL_IMPORTANCE,
    )
    considered_count = len(articles)
    kk = k if k is not None else _K_BY_PERIOD.get(period_type, 6)
    claims = await nominate_claims(llm=llm, articles=articles, period_label=label, k=kk)

    # nominate は各 claim に 1-4 記事しか紐づけない。プール全記事の entity を一括取得し、
    # 各 claim の裏取りを同一事案/同一トピックの記事で決定論的に拡張する (判定数は増やさない)。
    pool_ids = [str(a.get("article_id", "")) for a in articles if a.get("article_id")]
    entities_by_id = repo.entity_keys_for_articles(pool_ids)

    # 過去文脈の窓 [context_start, period_start)。period_start より前=現在プールと重ならない
    now_dt = now or datetime.now(UTC)
    period_start = now_dt - timedelta(hours=lookback)
    context_start = now_dt - timedelta(hours=_CONTEXT_WINDOW_HOURS.get(period_type, 90 * 24))

    def _build_source(aid: str, *, historical: bool) -> dict[str, str] | None:
        """article_id → ground 用 source dict (過去文脈は本文を短く切る)。無本文は None。"""
        g = repo.get_article_grounding(aid)
        if not g or not g["text"]:
            return None
        chrono = article_chronology(
            report=g.get("created_at"),
            event_date=g.get("event_date"),
            event_date_basis=g.get("event_date_basis"),
        )
        text = (
            truncate_body(g["text"], max_chars=_HISTORICAL_BODY_CHARS)
            if historical
            else truncate_body(g["text"])
        )
        return {
            "article_id": aid,
            "feed_title": g["feed_title"],
            "feed_url": g["feed_url"],
            "text": text,
            # 時系列事実 (発生日時の前後関係) を ACH に供給 (因果は断定しない)。
            "chronology": chrono.label,
            "historical": "1" if historical else "",
        }

    judgments: list[KeyJudgment] = []
    for i, c in enumerate(claims):
        sources: list[dict[str, str]] = []
        tier_by_id: dict[str, str] = {}
        expanded_ids = expand_claim_sources(
            claim_text=c.claim,
            seed_ids=c.article_ids,
            pool=articles,
            entities_by_id=entities_by_id,
            max_sources=_MAX_SOURCES_PER_CLAIM,
        )
        # 過去文脈: 現在ソースの anchor entity (actor/CVE/malware/被害組織) を持つ対象期間より前の
        # 記事を ACH 証拠に引く (前例/パターン/新規性/再報道の判定)。ノミネートは対象期間のまま。
        anchors = anchor_entities(entities_by_id, tuple(expanded_ids))
        hist_ids = (
            repo.articles_for_entity_keys(
                anchors, since=context_start, before=period_start, limit=_MAX_HISTORICAL_PER_CLAIM
            )
            if anchors
            else []
        )
        for aid in expanded_ids:
            src = _build_source(aid, historical=False)
            if src:
                sources.append(src)
                tier_by_id[aid] = classify_source_tier(src["feed_title"], src["feed_url"])
        seen_ids = {s["article_id"] for s in sources}
        for aid in hist_ids:
            if aid in seen_ids:
                continue
            src = _build_source(aid, historical=True)
            if src:
                sources.append(src)
                tier_by_id[aid] = classify_source_tier(src["feed_title"], src["feed_url"])
        if not sources:
            continue
        try:
            analysis = await ground_and_score(
                llm=llm, claim=c.claim, domain=c.domain, sources=sources, tier_by_id=tier_by_id
            )
        except Exception as e:  # noqa: BLE001 — 1 claim の失敗で estimate 全体を止めない
            _log.warning("grounded_ach_failed", claim=c.claim[:60], error=str(e))
            continue
        sb = compute_source_basis(repo, list(c.article_ids), kev_set=kev_set)
        conf, reason = final_confidence(
            analysis.llm_confidence, sb.confidence, analysis.leading_hypothesis, analysis.evidence
        )
        basis = f"ACH={analysis.llm_confidence} / source_basis={sb.confidence}"
        if reason:
            basis = f"{basis} / {reason}"
        judgments.append(
            KeyJudgment(
                id=f"j{i + 1}",
                claim=c.claim,
                domain=c.domain,
                leading_hypothesis=analysis.leading_hypothesis,
                confidence=conf,
                confidence_basis=basis,
                hypotheses=analysis.hypotheses,
                evidence=analysis.evidence,
                key_assumptions=analysis.key_assumptions,
                missing_evidence=analysis.missing_evidence,
                indicators=analysis.indicators,
                claim_type=analysis.claim_type,
                implication=analysis.implication,
            )
        )
    # 段3: 対称 red-team で全 judgment を検証 (反証成立 → 確度 low 降格 / 採点済み仮説なら flip)。
    if judgments:
        try:
            reviews = await adversarial_review(llm=llm, judgments=tuple(judgments))
            judgments = [apply_adversarial(j, reviews.get(j.id)) for j in judgments]
        except Exception as e:  # noqa: BLE001 — 検証失敗で estimate を捨てない
            _log.warning("adversarial_review_failed", error=str(e))

    _log.info(
        "grounded_estimate_built",
        period_type=period_type,
        considered=considered_count,
        nominated=len(claims),
        judgments=len(judgments),
        refuted=sum(1 for j in judgments if j.adversarial_refuted),
    )
    return Estimate(
        period_type=period_type,
        period_start=start,
        period_end=end,
        judgments=tuple(judgments),
        model=getattr(llm, "model", ""),
        considered_count=considered_count,
    )


async def generate_grounded_synthesis(
    *,
    llm: LLMClient,
    period_type: str,
    now: datetime | None = None,
    db_path: Path = _DEFAULT_DB,
    kev_set: frozenset[str] | None = None,
    fast_llm: LLMClient | None = None,
    analysis_llm: LLMClient | None = None,
    projection_only: bool = False,
) -> SynthesisGenerationResult:
    """証拠駆動パイプラインで synthesis を生成し SynthesisGenerationResult を返す (後方互換)。

    generate_synthesis (single-pass) のドロップイン代替。record は estimate の射影、
    canonical estimate は tradecraft.grounded_estimate に埋め込まれる。

    ``analysis_llm``: 構造化分析 (ACH/adversarial/射影) 用の reasoning ティア client。
    None なら ``llm`` を流用 (挙動保存 — think ティア分離 2026-07-24)。

    projection_only=True (backfill 用): 台帳の既存 revision からの射影のみで生成し、
    評価も台帳書込も行わない (過去時刻 now での通常経路は revision 順序を壊すため禁止 —
    監査 2026-07-16 設計 §2)。ledger mode on が前提。
    """
    from src.assessment.ledger import ledger_mode, update_ledger
    from src.synthesis.generator import SynthesisGenerationResult, _resolve_period

    ach_llm = analysis_llm or llm

    repo = RunHistoryRepository()
    mode = ledger_mode()
    projection_store = None  # 台帳稼働時のみ forecast/freshness 射影に使う
    if projection_only:
        if mode != "on":
            return SynthesisGenerationResult(
                record=None, raw_response="", error="projection_only requires SYNTHESIS_STATE=1"
            )
        if now is None:
            return SynthesisGenerationResult(
                record=None, raw_response="", error="projection_only requires explicit now"
            )
        from src.assessment.situation_store import SituationStore
        from src.assessment.stateful import build_projection_estimate

        est = await build_projection_estimate(
            period_type=period_type,
            repo=repo,
            store=SituationStore(db_path=db_path),
            now=now,
        )
    elif mode == "on":
        # 段B: 台帳駆動 (割当→増分ACH→detect-new→adversarial)。Estimate は台帳の射影。
        from src.assessment.situation_store import SituationStore
        from src.assessment.stateful import build_estimate_stateful

        projection_store = SituationStore(db_path=db_path)
        est = await build_estimate_stateful(
            llm=ach_llm,
            fast_llm=fast_llm,
            period_type=period_type,
            repo=repo,
            store=projection_store,
            now=now,
            db_path=db_path,
            kev_set=kev_set,
        )
    else:
        est = await build_estimate(
            llm=ach_llm,
            period_type=period_type,
            repo=repo,
            now=now,
            db_path=db_path,
            kev_set=kev_set,
        )
        # 段A (shadow): 旧経路の estimate から台帳を更新 (出力不変・失敗しても止めない)。
        # 設計: docs/synthesis_situation_ledger_design.md。flag: SYNTHESIS_STATE (0/shadow/1)。
        if mode == "shadow":
            try:
                from src.assessment.situation_store import SituationStore

                update_ledger(
                    est=est,
                    repo=repo,
                    store=SituationStore(db_path=db_path),
                    db_path=db_path,
                    now=now,
                )
            except Exception as e:  # noqa: BLE001 — 台帳は shadow、synthesis 本体を守る
                _log.warning("situation_ledger_failed", error=str(e))
    if not est.judgments:
        return SynthesisGenerationResult(
            record=None, raw_response="", error="grounded: no key judgments"
        )
    _start, _end, label, _lb, _bw = _resolve_period(period_type=period_type, now=now)
    # article_count は「考慮した記事プール」(期間内 high+medium、run 間で意味が揺れない
    # 品質監視指標)。判定が引用した記事数 (cited) は run 依存に振れるので log に分離する。
    cited_count = len({e.article_id for j in est.judgments for e in j.evidence})
    article_count = est.considered_count or cited_count
    _log.info(
        "grounded_record_articles",
        period_type=period_type,
        considered=est.considered_count,
        cited=cited_count,
    )
    # 監査 2026-07-05 P4: forecast/freshness を tradecraft に射影 (台帳稼働時のみ)。
    forecast_ctx = None
    if projection_store is not None:
        try:
            from src.assessment.forecast import build_forecast_context

            forecast_ctx = build_forecast_context(
                store=projection_store,
                period_start=_start,
                period_end=_end,
                now=now or datetime.now(UTC),
            )
        except Exception as e:  # noqa: BLE001 — 射影補助の失敗で報告を止めない
            _log.warning("forecast_context_failed", error=str(e))
    # 反復抑制 (2026-08-07): 前回 (同 period_type、別 period_start) の headline 判定 id を
    # tradecraft から回収し、quiet 日の同一 standing 再掲を継続表記へ置き換える材料にする。
    # 同日 regenerate の自己比較を避けるため period_start 一致行は読み飛ばす。
    prev_headline_judgment_id: str | None = None
    try:
        import json as _json

        for _prev in repo.list_synthesis(period_type=period_type, limit=3):
            if _prev.period_start.date() == _start.date():
                continue
            if _prev.tradecraft:
                _v = _json.loads(_prev.tradecraft).get("headline_judgment_id")
                prev_headline_judgment_id = str(_v) if _v else None
            break
    except Exception as e:  # noqa: BLE001 — 反復抑制の材料欠落で報告を止めない
        _log.warning("prev_headline_lookup_failed", error=str(e))
    # render (台帳→報告書の射影整形) も構造化分析側 — think ティア分離後も挙動保存
    # (narrative 側へ移すかは ACH think A/B の結果で判断)。
    record = await render_record(
        llm=ach_llm,
        est=est,
        period_label=label,
        article_count=article_count,
        forecast_ctx=forecast_ctx,
        prev_headline_judgment_id=prev_headline_judgment_id,
    )
    return SynthesisGenerationResult(record=record, raw_response="grounded", error=None)
