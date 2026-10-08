"""M4 の triage 影子記録オーケストレーション (2026-10-08)。

``src.pipeline.filters._filter_by_triage`` が本番の triage 判定を終えたあとに呼ぶ。
最大 ``TRIAGE_SHADOW_PER_RUN`` 件 (既定 20、0 で無効) を対象に、平たい triage
(``ArticleTriage.triage_flat``) + 取り込み時ヒント (``src.cti.ingest_relevance``) で
判定し直し、現行判定と並べて ``triage_shadow`` に記録する。**本番の取り込み判定・
配信は一切変えない** (記録のみ)。

1 件の失敗は飲んで次へ進む (run 全体を止めない)。``TRIAGE_SHADOW_BUDGET_SECONDS``
(既定 180 秒) を超えたら残りの対象をスキップする (本処理の遅延を防ぐ時間予算)。
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Sequence

from src.cti.ingest_relevance import ingest_relevance_hint
from src.cti.relevance_ml import (
    RelevanceJpModel,
    cascade_japan_involved,
    in_uncertain_band,
    jp_probability,
    load_relevance_jp_model,
    ml_fired,
)
from src.logging_config import get_logger
from src.storage.repo_triage_shadow import TriageShadowRow
from src.tools.article_model import Article
from src.tools.article_triage import ArticleTriage
from src.tools.embedding_client import EmbeddingClient
from src.tools.llm_client import LLMClient
from src.tools.text_utils import strip_html as _strip_html

_log = get_logger(__name__)

_PER_RUN_ENV = "TRIAGE_SHADOW_PER_RUN"
_BUDGET_ENV = "TRIAGE_SHADOW_BUDGET_SECONDS"
#: 既定 20 件/run・常時 ON (M4 の安全網、2026-10-08)。0 で無効化。
_DEFAULT_PER_RUN = 20
_DEFAULT_BUDGET_SECONDS = 180.0
#: triage 本体の既定同時実行数 (5) より絞る — 影子記録は本処理の余力で回す補助経路
_CONCURRENCY = 3
_BODY_PREVIEW_CHARS = 1500
#: flat_importance の medium 以上 (new_kept_v2 の構成要素)
_MEDIUM_PLUS = frozenset({"medium", "high"})


def shadow_sample_size() -> int:
    """1 run あたりの影子記録対象件数 (env override → 既定 20、0 以下は無効)。"""
    raw = os.environ.get(_PER_RUN_ENV, "").strip()
    if not raw:
        return _DEFAULT_PER_RUN
    try:
        n = int(raw)
    except ValueError:
        return _DEFAULT_PER_RUN
    return max(0, n)


def _budget_seconds() -> float:
    raw = os.environ.get(_BUDGET_ENV, "").strip()
    if not raw:
        return _DEFAULT_BUDGET_SECONDS
    try:
        v = float(raw)
    except ValueError:
        return _DEFAULT_BUDGET_SECONDS
    return v if v > 0 else _DEFAULT_BUDGET_SECONDS


def _summary_preview(article: Article) -> str:
    raw = article.body_text or article.summary_html or ""
    return _strip_html(raw)[:_BODY_PREVIEW_CHARS]


async def _relevance_jp_cascade(
    article: Article,
    *,
    model: RelevanceJpModel,
    embedder: EmbeddingClient,
    cascade_llm: LLMClient | None,
) -> tuple[float | None, bool | None, bool | None]:
    """日本関連性 ML カスケード (案 D) を 1 件分実行する。

    戻り値: ``(jp_prob, jp_ml_fired, jp_cascade)``。帯外 (確信度が高い/低い) は
    ``jp_cascade=None`` (LLM 未呼出)。embedding 失敗等で確率が取れないときは全て None。
    """
    title = (article.title or "").strip()
    preview = _summary_preview(article)
    prob = await jp_probability(
        feed=(article.feed_title or "").strip(),
        title=title,
        preview=preview,
        embedder=embedder,
        model=model,
    )
    if prob is None:
        return None, None, None
    fired = ml_fired(prob, model)
    if not in_uncertain_band(prob, model) or cascade_llm is None:
        return prob, fired, None
    judgement = await cascade_japan_involved(title=title, preview=preview, llm=cascade_llm)
    # カスケード失敗時は帯内なので ml_fired (= True、99%-recall 側) へ fail-open する
    # (再現率優先の方針、src.cti.ingest_relevance と同じ倒し方)。
    cascade_result = judgement.japan_involved if judgement is not None else fired
    return prob, fired, cascade_result


def _jp_final(jp_prob: float | None, jp_ml_fired: bool | None, jp_cascade: bool | None) -> bool:
    """帯外なら ML 発火そのもの、帯内なら cascade 結果を最終判定にする。"""
    if jp_prob is None or jp_ml_fired is None:
        return False
    if jp_cascade is not None:
        return jp_cascade
    return jp_ml_fired


def _has_non_jp_hint(reasons: tuple[str, ...]) -> bool:
    """決定論ヒントのうち日本関連 (``jp``/``jp:*``) 以外の発火 (注視国/アクター/SIR)。"""
    return any(not r.startswith("jp") for r in reasons)


async def run_triage_shadow(
    decisions: Sequence[tuple[Article, str, bool]],
    *,
    llm: LLMClient,
    keep_importance: set[str],
    think: bool = False,
    relevance_embedder: EmbeddingClient | None = None,
    relevance_cascade_llm: LLMClient | None = None,
) -> list[TriageShadowRow]:
    """最大 N 件を平たい triage + 取り込みヒントで判定し直し、影子記録を組み立てる。

    ``decisions``: (article, current_importance, current_kept) のタプル列
    (呼び出し側が本番の triage 判定から作る)。戻り値は DB 未保存の行オブジェクト —
    永続化は呼び出し側 (``RunHistoryRepository.record_triage_shadow``) が行う。

    ``relevance_embedder`` / ``relevance_cascade_llm`` (M4、2026-10-08): 指定時のみ
    日本関連性 ML カスケード (案 D) を併走し ``jp_prob`` / ``jp_ml_fired`` / ``jp_cascade`` /
    ``new_kept_v2`` を記録する。未指定・モデル不在ならこれらは ``None`` (従来挙動と同じ)。
    """
    n = shadow_sample_size()
    if n <= 0 or not decisions:
        return []

    targets = list(decisions)[:n]
    triage = ArticleTriage(llm, think=think)
    budget = _budget_seconds()
    start = time.monotonic()
    sem = asyncio.Semaphore(_CONCURRENCY)
    jp_model = load_relevance_jp_model() if relevance_embedder is not None else None

    async def _one(
        article: Article, current_importance: str, current_kept: bool
    ) -> TriageShadowRow | None:
        if time.monotonic() - start > budget:
            return None
        async with sem:
            if time.monotonic() - start > budget:
                return None
            try:
                flat = await triage.triage_flat(article)
                hint = ingest_relevance_hint(
                    feed=(article.feed_title or "").strip(),
                    title=(article.title or "").strip(),
                    summary_preview=_summary_preview(article),
                )
            except Exception as e:  # noqa: BLE001 — 1 件の失敗で run 全体を止めない
                _log.warning("triage_shadow_sample_failed", article_id=article.id, error=str(e))
                return None
            new_kept = (flat.importance in keep_importance) or hint.fired

            jp_prob = jp_ml_fired = jp_cascade = None
            if jp_model is not None and relevance_embedder is not None:
                try:
                    jp_prob, jp_ml_fired, jp_cascade = await _relevance_jp_cascade(
                        article,
                        model=jp_model,
                        embedder=relevance_embedder,
                        cascade_llm=relevance_cascade_llm,
                    )
                except Exception as e:  # noqa: BLE001 — ML 側の失敗は影子記録全体を止めない
                    _log.warning(
                        "triage_shadow_relevance_ml_failed", article_id=article.id, error=str(e)
                    )
            new_kept_v2 = (
                flat.importance in _MEDIUM_PLUS
                or _jp_final(jp_prob, jp_ml_fired, jp_cascade)
                or _has_non_jp_hint(hint.reasons)
            )
            return TriageShadowRow(
                article_id=article.id,
                url=article.url,
                title=article.title or "",
                feed_title=article.feed_title or "",
                feed_url=article.feed_url or "",
                current_importance=current_importance,
                current_kept=current_kept,
                flat_importance=flat.importance,
                hint_fired=hint.fired,
                hint_reasons=hint.reasons,
                new_kept=new_kept,
                jp_prob=jp_prob,
                jp_ml_fired=jp_ml_fired,
                jp_cascade=jp_cascade,
                new_kept_v2=new_kept_v2,
            )

    results = await asyncio.gather(*[_one(a, imp, kept) for a, imp, kept in targets])
    rows = [r for r in results if r is not None]
    _log.info("triage_shadow_sampled", requested=len(targets), recorded=len(rows))
    return rows


__all__ = ["run_triage_shadow", "shadow_sample_size"]
