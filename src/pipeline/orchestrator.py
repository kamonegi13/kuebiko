"""End-to-End パイプライン オーケストレータ (src.main から分割)。

``run_pipeline`` は 780 行超の一枚岩だが、取得→dedup→triage→要約→dedup→投稿→
永続化→通知 という 1 本の凝集した流れであるため 1 関数のまま移送する
(受け入れた例外: このファイルは ~850 行になる)。
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

import jinja2

from src.config_loader import (
    AppConfig,
    ChannelRouting,
    PipelineConfig,
    StixAttachPolicy,
)
from src.cti.identity_dedup import check_pre_post_dedup
from src.logging_config import get_logger
from src.pipeline.briefing import (
    DegenerateBodyError,
    _process_article,
)
from src.pipeline.filters import (
    _filter_by_triage,
    _filter_duplicates,
    _filter_semantic_duplicates,
    _prefetch_thin_bodies,
)
from src.pipeline.grok_convert import (
    _grok_article_to_briefings,
    _grok_subarticle_id,
    _is_grok_article,
)
from src.pipeline.persistence import (
    _persist_article_outcomes,
    _persist_feed_health,
)
from src.pipeline.postprocess import (
    _dedup_briefings_by_source_url,
    _dedup_incidents_semantic,
    _maybe_post_system_notification,
    _sort_briefings_for_posting,
)
from src.pipeline.publish import _print_dry_run, _resolve_channel
from src.pipeline.result import PipelineRunResult
from src.pipeline.summary import DiscordChannel
from src.storage.run_history import RunHistoryRepository
from src.tools.article_model import Article
from src.tools.channel_registry import fallback_map, push_map
from src.tools.content_extractor import ContentExtractor
from src.tools.discord_publisher import (
    BriefingMessage,
    DiscordPublisher,
    Importance,
)
from src.tools.embedding_client import EmbeddingClient
from src.tools.llm_client import (
    LLMClient,
    LLMConnectionError,
    LLMTimeoutError,
)
from src.tools.source_router import ArticleSource
from src.tools.url_normalizer import url_hash

_log = get_logger(__name__)

# 時間予算 (soft deadline、2026-08-01)。親 (PipelineRunner) の wallclock timeout で
# kill されると「投稿 0・既読化 0・成果全損 → 次 run が同じ記事を再処理」の全損ループに
# なる (run 3520: backlog + LLM 低速化で 1800s kill)。子は予算から投稿/永続化/通知に
# 使う予備を差し引いた soft deadline を持ち、記事処理ループを途中で打ち切って処理済み分
# だけ確定させる。打ち切られた記事は既読化しない = 次 run の RSS 窓でリトライ権を保持。
_DEADLINE_RESERVE_SECONDS = 300.0
# 予算が予備より短い場合でも処理時間がゼロにならないための下限比率
_MIN_PROCESSING_BUDGET_RATIO = 0.5

# per-article バックストップ (2026-08-01)。単一の病的記事 (巨大 advisory 等) が run の
# 時間予算を専有するのを防ぐ。超過は一時障害 (transient) 扱いで既読化しない = 次 run で
# リトライ (低速の主因は Ollama cold reload 等のインフラ側で、同記事が恒常的に遅いとは
# 限らないため恒久 seen 化しない)。既知の病理 (CVE 参照リスト) は ioc_llm_verifier の
# CVE cap が除去済で、これは未知の暴走への保険。env PER_ARTICLE_TIMEOUT_SECONDS で
# override (0 以下で無効)。
_PER_ARTICLE_TIMEOUT_SECONDS_DEFAULT = 600.0


# 記事処理の同時実行数 (2026-08-17)。既定 1 = 従来の逐次挙動。
#
# 実測 (gemma4:26b、4 件 x 3 往復 ABABAB、OLLAMA_NUM_PARALLEL=4):
#   - decode 主体 (出力 400 tok): 逐次 33.24s → 並列 17.98s = **1.85x** (効く)
#   - prefill 主体 (出力 16 tok):  逐次  9.84s → 並列  9.89s = 1.00x (変わらない)
# decode は 1 トークンごとにモデル重み全体をメモリから読むため帯域律速で演算器が余る。
# 束ねると重みの読み出しを共有できるので効く。prefill は入力を一括処理する演算律速で
# 1 本で既に飽和しているため、束ねても変わらない。記事処理は decode 主体なのでここを並列化する。
#
# ⚠ 当初 prefill 側を「0.68x に悪化」と記録したが、単発測定のノイズで反復すると
# 再現しなかった (正しくは 1.00x = 中立)。単発の性能測定を結論にしないこと。
#
# **単独では効かない**: Ollama は既定 1 スロットで同時要求を直列化するため、
# ここを上げるだけでは実効並列度が上がらない。ホスト側 OLLAMA_NUM_PARALLEL の
# 引き上げと対で運用する。
_ARTICLE_CONCURRENCY_DEFAULT = 1
_ARTICLE_CONCURRENCY_MAX = 8


def _article_concurrency() -> int:
    """記事処理の同時実行数を解決する (env override → 既定 1、壊れた値は安全側=逐次)。"""
    raw = os.environ.get("ARTICLE_CONCURRENCY", "").strip()
    if not raw:
        return _ARTICLE_CONCURRENCY_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        return _ARTICLE_CONCURRENCY_DEFAULT
    if value < 1:
        return _ARTICLE_CONCURRENCY_DEFAULT
    return min(value, _ARTICLE_CONCURRENCY_MAX)


@dataclass(frozen=True)
class _ArticleWork:
    """記事 1 本の処理が生んだ寄与分。

    共有 list を直接 append すると並列化で順序が非決定になるため、各記事は自分の
    寄与だけを返し、呼び出し側が**入力順に**統合する。
    """

    briefings: tuple[tuple[str, BriefingMessage], ...]
    outcomes: tuple[dict[str, object], ...]
    errors: tuple[str, ...]
    # (sub_id, 親 article) — Grok 展開時のみ非空
    grok_subarticles: tuple[tuple[str, Article], ...]

    @staticmethod
    def empty() -> _ArticleWork:
        return _ArticleWork(briefings=(), outcomes=(), errors=(), grok_subarticles=())


async def _run_articles_bounded[T, R](
    items: Sequence[T],
    handler: Callable[[T], Awaitable[R]],
    *,
    concurrency: int,
    should_stop: Callable[[], bool] | None,
) -> tuple[list[R], int]:
    """``items`` を入力順に取り出し、最大 ``concurrency`` 本を同時に処理する。

    戻り値は ``(着手した分の結果を入力順に並べた list, 繰越件数)``。

    ``should_stop`` は soft deadline 判定。**新規の着手だけを止め、実行中は完走させる**
    (逐次ループの ``break`` と同じ意味 — 処理済み分は投稿/永続化に乗せて成果を確定する)。

    ``handler`` は自分で例外を処理する契約。漏れた例外は握り潰さず伝播させる
    (逐次ループでも run 全体へ伝播していたため、並列化で障害を隠さない)。
    """
    if not items:
        return [], 0

    results: dict[int, R] = {}
    next_index = 0
    deferred = 0

    async def worker() -> None:
        nonlocal next_index, deferred
        while True:
            # await を挟まないので index の取得と更新は不可分 (単一スレッドの asyncio)
            if next_index >= len(items):
                return
            index = next_index
            next_index += 1
            if should_stop is not None and should_stop():
                deferred += 1
                continue
            results[index] = await handler(items[index])

    await asyncio.gather(*[worker() for _ in range(max(1, concurrency))])
    return [results[i] for i in sorted(results)], deferred


def _per_article_timeout_seconds() -> float | None:
    """per-article timeout を解決する (env override → 既定値、0 以下で無効)。"""
    raw = os.environ.get("PER_ARTICLE_TIMEOUT_SECONDS", "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            return _PER_ARTICLE_TIMEOUT_SECONDS_DEFAULT
        return value if value > 0 else None
    return _PER_ARTICLE_TIMEOUT_SECONDS_DEFAULT


def _compute_soft_deadline(time_budget_seconds: float | None) -> float | None:
    """時間予算から記事処理ループの soft deadline (monotonic 秒) を計算する。"""
    if time_budget_seconds is None or time_budget_seconds <= 0:
        return None
    processing_budget = max(
        time_budget_seconds - _DEADLINE_RESERVE_SECONDS,
        time_budget_seconds * _MIN_PROCESSING_BUDGET_RATIO,
    )
    return time.monotonic() + processing_budget


def _mark_skipped_urls_seen(
    skipped_ids: list[str],
    articles_by_id: dict[str, Article],
    dedup_repo: RunHistoryRepository,
    semantic_embeddings: dict[str, tuple[str, list[float]]] | None = None,
) -> int:
    """dedup skip 経路の URL を dedup_seen_urls に登録し、次 run の再 fetch→再評価
    リサイクル (取込履歴の毎時フラッド + 48h/24h 窓後の同一 URL 再投稿) を止める。

    ⚠ **判断根拠 (embedding) も一緒に残す** (2026-08-19)。従来は投稿経路でしか
    embedding を保存しておらず、重複で落とした記事は 14 日 239 件すべて根拠が消えていた。
    そのため「別の層なら捕まえられたか」「閾値変更で新たに落ちた記事は妥当か」を
    **後から検証できなかった**。落とす判断こそ検証材料が要る。

    posting-loop の cross-ch / CVE / content dedup は skipped_duplicate にするが URL を
    既読化しないため、RSS が同記事を再配信するたびに再評価→再 skip をリサイクルしていた。
    semantic/triage 落選と同じ「評価済み・不採用は既読化する終端状態」を全 skip 経路に拡張する。

    ``articles_by_id`` に無い id (semantic/triage/URL-dedup の早期 skip は articles から除外済で
    別経路で既読化済) は素通りする。二重 mark は idempotent UPSERT で無害。既読化件数を返す。
    """
    marked = 0
    for skip_id in dict.fromkeys(skipped_ids):
        article = articles_by_id.get(skip_id)
        if article is None:
            continue
        try:
            h = url_hash(article.url)
            dedup_repo.mark_url_seen(
                url_hash=h,
                url=article.url,
                article_id=skip_id,
                title=article.title,
            )
            emb = (semantic_embeddings or {}).get(skip_id)
            if emb is not None:
                dedup_repo.add_article_embedding(
                    url_hash=h,
                    url=article.url,
                    vector=emb[1],
                    model=emb[0],
                    title=article.title,
                )
            marked += 1
        except Exception as e:  # noqa: BLE001 — 既読化失敗は次 run 再評価で自癒
            _log.debug("dedup_skip_seen_mark_failed", article_id=skip_id, error=str(e))
    return marked


# ---------- 公開 API ----------


async def run_pipeline(
    *,
    config: AppConfig,
    pipeline: PipelineConfig,
    source: ArticleSource,
    extractor: ContentExtractor,
    llm: LLMClient,
    publishers: dict[DiscordChannel, DiscordPublisher],
    template: jinja2.Template,
    dry_run: bool = False,
    max_articles: int | None = None,
    dedup_repo: RunHistoryRepository | None = None,
    embedder: EmbeddingClient | None = None,
    skip_dedup: bool = False,
    extract_llm: LLMClient | None = None,
    run_id: int | None = None,
    channel_routing: ChannelRouting | None = None,
    enrichment: object | None = None,  # LlmEnrichment (Phase 5D)
    stix_policy: StixAttachPolicy | None = None,  # Phase 5F
    time_budget_seconds: float | None = None,  # 親の wallclock timeout 予算 (2026-08-01)
) -> PipelineRunResult:
    """ツールを注入された状態で End-to-End パイプラインを実行する。

    Phase 2 から source を ``ArticleSource`` Protocol で受け取り、複数の情報源
    (RSS / Grok email + Playwright / web scraper) を同じパスで処理する。
    """
    effective_max = max_articles if max_articles is not None else pipeline.source.max_articles
    routing = channel_routing or ChannelRouting()
    importance_map: dict[Importance, DiscordChannel] = dict(routing.importance_map)
    # Phase 5D: LLM 補強設定 (なければ全機能 enabled の既定)
    from src.config_loader import LlmEnrichment as _LlmEnrichment

    enrich = enrichment if isinstance(enrichment, _LlmEnrichment) else _LlmEnrichment()
    # Phase 5F: STIX 添付ポリシー (None なら strict 既定)
    stix_attach: StixAttachPolicy = stix_policy if stix_policy is not None else StixAttachPolicy()

    _log.info(
        "pipeline_start",
        pipeline=pipeline.name,
        source_type=pipeline.source.type,
        dry_run=dry_run,
        max_articles=effective_max,
    )

    # 時間予算 → soft deadline (詳細はモジュール先頭コメント)。予算未指定なら無効。
    soft_deadline = _compute_soft_deadline(time_budget_seconds)
    deferred_count = 0

    # 1. 記事取得 (source 種別に依存しない)
    try:
        articles = await source.fetch(max_count=effective_max)
    except Exception as e:  # noqa: BLE001
        # Phase 5P: 中途エラーでも取得済み件数を回収する (例外が partial_count を
        # 持つ場合のみ。現行 source はいずれも立てないため実質 0)
        partial_count = int(getattr(e, "partial_count", 0) or 0)
        partial_note = (
            f"partial_fetch={partial_count}/{effective_max}" if partial_count > 0 else None
        )
        _log.error(
            "source_fetch_failed",
            error=str(e),
            source_type=pipeline.source.type,
            partial_count=partial_count,
        )
        errors_list = [f"source_fetch: {type(e).__name__}: {e}"]
        if partial_note is not None:
            errors_list.append(partial_note)
        early_result = PipelineRunResult(
            errors=errors_list,
            partial_fetch=partial_count > 0,
            partial_fetch_count=partial_count,
        )
        # 失敗即時通知 (system_notify_enabled 時のみ、dry-run は除外)
        # Phase 5L-1: 失敗時は rate limit 適用外なので is_interval_run=False で常時送信
        if not dry_run and routing.system_notify_enabled:
            notified = await _maybe_post_system_notification(
                publishers,
                pipeline.name,
                early_result,
                run_id,
                is_interval_run=False,
                dedup_repo=dedup_repo,
            )
            if notified:
                early_result = early_result.model_copy(update={"ops_notified": True})
        return early_result
    _log.info("articles_fetched", count=len(articles), source_type=pipeline.source.type)

    # 監査 2026-07-05 P2 / 2026-08-02: per-source fetch 結果を永続化 (死活検知の観測点)。
    # RSS / html_scraper / sitemap が同じ seam (last_fetch_health) で記録する。
    # **新着 0 件でもここに到達することが要件** — 「静かなだけ」と「壊れて無音」の
    # 区別は、成果ではなく取得行為の記録によってのみ可能 (早期 return は例外時のみ)。
    _persist_feed_health(source, dedup_repo)

    # 投稿せず終端した article id を集約 (重複判定 / triage 切り捨て)。
    # dedup_seen_urls に既読登録して次 run の再 fetch→再評価リサイクルを止める。
    # LLM/post 失敗はリトライ余地を残すため対象外。
    skipped_for_mark_read: list[str] = []

    # 1.5. URL ベースの重複排除 (Phase 3a)
    # dry-run でもスキップする (dry-run の意図は LLM 出力プレビューであり、重複判定は揃える)
    # skip_dedup=True なら一切 dedup しない (UI からの再投稿テスト用)
    skipped_dup = 0
    if not skip_dedup and dedup_repo is not None and articles:
        articles, skipped_dup, skipped_dup_ids = _filter_duplicates(articles, dedup_repo)
        skipped_for_mark_read.extend(skipped_dup_ids)
        if skipped_dup > 0:
            _log.info("dedup_skipped", count=skipped_dup, source_type=pipeline.source.type)
    elif skip_dedup:
        _log.info("dedup_skipped_by_flag", source_type=pipeline.source.type)

    # 1.7. 意味的重複排除 (Phase 3b + 5L-2 二段階化)
    # Phase 5L-2: 1 段階 (similarity_threshold) を hard / cluster の 2 段階に分離。
    #   hard: URL 違いの "ほぼ同一記事" 再投稿防止 (高 threshold + 長窓)
    #   cluster: 別ソースで同事象を扱う後発記事の取りこぼし防止 (低 threshold + 短窓)
    # どちらかにヒットすれば skip 扱い。embedder が None or 障害時は graceful degradation。
    skipped_dup_semantic = 0
    semantic_embeddings: dict[str, tuple[str, list[float]]] = {}
    # embedding 生成が走った run か (posted なのに embedding 無しの警告は、生成経路が
    # そもそも無効な run では出さない)
    semantic_dedup_active = False
    if not skip_dedup and dedup_repo is not None and embedder is not None and articles:
        semantic_dedup_active = True
        proc = pipeline.processor
        effective_threshold_hard = getattr(proc, "similarity_threshold_hard", 0.92)
        effective_threshold_cluster = getattr(proc, "similarity_threshold_cluster", 0.78)
        effective_window_hard = getattr(proc, "dedup_window_hours_hard", 168)
        effective_window_cluster = getattr(proc, "dedup_window_hours_cluster", 48)
        pre_semantic_by_id = {a.id: a for a in articles}
        (
            articles,
            skipped_dup_semantic,
            semantic_embeddings,
            skipped_semantic_ids,
        ) = await _filter_semantic_duplicates(
            articles,
            dedup_repo,
            embedder,
            threshold_hard=effective_threshold_hard,
            threshold_cluster=effective_threshold_cluster,
            window_hours_hard=effective_window_hard,
            window_hours_cluster=effective_window_cluster,
        )
        skipped_for_mark_read.extend(skipped_semantic_ids)
        # semantic 重複 = 判断済み (embedding 一致で不採用)。URL が異なるため URL-dedup では
        # 既読にならず、既読化しないと毎 run 再 embedding + 再比較のリサイクルになる
        # (triage 落選と同じ「評価済み・不採用の終端状態」欠落、2026-07-12 根治)。
        if not dry_run and skipped_semantic_ids:
            for sem_id in skipped_semantic_ids:
                sem_a = pre_semantic_by_id.get(sem_id)
                if sem_a is None:
                    continue
                try:
                    sem_h = url_hash(sem_a.url)
                    dedup_repo.mark_url_seen(
                        url_hash=sem_h,
                        url=sem_a.url,
                        article_id=sem_id,
                        title=sem_a.title,
                    )
                    # 判断根拠も残す (2026-08-19)。⚠ 最多の skip 経路 (7 日 670 件) が
                    # ここ。embedding を捨てると「この閾値変更で新たに落ちた記事は
                    # 妥当だったか」を後から一切検証できない。
                    sem_emb = semantic_embeddings.get(sem_id)
                    if sem_emb is not None:
                        dedup_repo.add_article_embedding(
                            url_hash=sem_h,
                            url=sem_a.url,
                            vector=sem_emb[1],
                            model=sem_emb[0],
                            title=sem_a.title,
                        )
                except Exception as e:  # noqa: BLE001 — 既読化失敗は次 run 再評価で自癒
                    _log.debug("semantic_dup_seen_mark_failed", article_id=sem_id, error=str(e))
        if skipped_dup_semantic > 0:
            _log.info(
                "dedup_skipped_semantic",
                count=skipped_dup_semantic,
                source_type=pipeline.source.type,
            )

    # 1.8. Phase 3.1: 軽量 LLM による事前 triage (重要度判定 → フィルタ)
    # 1 run 100 件規模を高速に絞り込む。本要約 (gemma4:31b 15s/記事) の
    # 前に title+概要のみで 1-3s/記事の triage を回し、threshold 以上のみ通す。
    # Grok 経路は元から重要度を内包するので triage 対象外 (フィルタしない)。
    triage_error_count = 0
    if pipeline.processor.triage_enabled and articles:
        # Phase 3: thin feed は本文を先行抽出して triage 精度を上げる (recall 重視)
        if pipeline.processor.thin_feed_triage_enabled:
            articles, prefetched_thin = await _prefetch_thin_bodies(
                articles,
                min_chars=pipeline.processor.thin_feed_min_chars,
            )
            if prefetched_thin > 0:
                _log.info(
                    "thin_feed_prefetched",
                    count=prefetched_thin,
                    source_type=pipeline.source.type,
                )
        triage_llm = extract_llm if extract_llm is not None else llm
        (
            articles,
            skipped_triage,
            skipped_triage_ids,
            triage_error_count,
            rejected_triage,
            triage_decisions,
        ) = await _filter_by_triage(
            articles,
            triage_llm,
            keep_importance=set(pipeline.processor.triage_keep_importance),
            max_keep=pipeline.processor.triage_max_keep,
            think=pipeline.processor.think_enabled,
        )
        skipped_for_mark_read.extend(skipped_triage_ids)
        # 評価済み・不採用 (importance 不足) は URL 既読化する — 「既読」の状態機械に
        # 欠けていた終端状態 (2026-07-12 根治)。旧経路の「skip→既読化」が Phase X-1
        # 撤去で受け皿を失い、落選記事が毎時 再選抜→再triage→再落選 をリサイクルしていた
        # (実測: 未見プール ~650 が恒常滞留、毎時 ~87 件を再評価 = 26B 呼出 ~2000/日の浪費、
        # 低頻度 feed は落選常連が公平枠を塞ぎ続け記事が一生出ない)。
        # max_keep の枠あふれは rejected に含まれない (リトライ権保持)。dry-run は状態不変。
        if not dry_run and dedup_repo is not None and rejected_triage:
            for rej in rejected_triage:
                try:
                    dedup_repo.mark_url_seen(
                        url_hash=url_hash(rej.url),
                        url=rej.url,
                        article_id=rej.id,
                        title=rej.title,
                    )
                except Exception as e:  # noqa: BLE001 — 既読化失敗は次 run 再評価で自癒
                    _log.debug("triage_reject_seen_mark_failed", article_id=rej.id, error=str(e))
            _log.info(
                "triage_rejected_marked_seen",
                count=len(rejected_triage),
                source_type=pipeline.source.type,
            )
        if skipped_triage > 0:
            _log.info(
                "triage_filtered",
                kept=len(articles),
                skipped=skipped_triage,
                source_type=pipeline.source.type,
            )
        # Phase 5P: triage の LLM 失敗 (fail-open) を表面化
        if triage_error_count > 0:
            _log.warning(
                "triage_errors_detected",
                count=triage_error_count,
                source_type=pipeline.source.type,
            )
    # 2. 各記事を処理 (1 件失敗しても続行)
    briefings: list[tuple[str, BriefingMessage]] = []  # (article_id, message)
    # Grok 展開時の per-tweet sub-article_id → 親 Article (persist の articles_by_id 補完用)。
    grok_subarticles: dict[str, Article] = {}
    errors: list[str] = []
    # 監査残項目① (2026-07-05): Grok セッション失効は「🟢 取得 0」に化けていた。
    # errors に載せて run を可視失敗にし、ops 通知 + UI (死活監視の Grok カード) へ誘導する。
    _grok_expired_raw = getattr(source, "last_session_expired_count", 0)
    _grok_expired = _grok_expired_raw if isinstance(_grok_expired_raw, int) else 0
    if _grok_expired > 0:
        errors.append(
            f"grok: セッション失効で {_grok_expired} 件のレポート取得不可 — "
            "Web UI 死活監視の Grok セッションカードから再取得してください"
        )
    # Phase 5A fix: ダッシュボード / 履歴 (articles テーブル) 用の per-briefing 結果。
    # 1 ブリーフィング = 1 ArticleRecord (Grok の section 展開も独立行になる)。
    # 完全失敗した article (briefings 0 件) は失敗ステータスで 1 行残す。
    article_outcomes: list[dict[str, object]] = []
    # title / url / feed_title は ArticleRecord 生成で使うため、id → article で引けるようにする
    articles_by_id_local = {a.id: a for a in articles}
    # Phase 5T-V: brief 24h cap 判定用の snapshot を run 先頭で 1 回取得。
    # 同 run 内で cap が動的に変わるのを避け、cap 判定の一貫性を保つ。
    # int キャストで Mock 等の非数値戻り値も安全に 0 へフォールバック。
    brief_count_24h_snapshot = 0
    if dedup_repo is not None:
        try:
            raw_count = dedup_repo.count_brief_in_window(hours=24)
            brief_count_24h_snapshot = int(raw_count)
            _log.info(
                "brief_cap_snapshot_taken",
                brief_count_24h=brief_count_24h_snapshot,
            )
        except (TypeError, ValueError, Exception) as e:  # noqa: BLE001
            _log.warning("brief_cap_snapshot_failed", error=str(e))

    # Phase 1 K5: pipeline 開始時に CISA KEV catalog を最新化 (TTL gated。stale な時だけ fetch)。
    # 以降 routing_signals._cve_on_kev は cache を読むだけ (hot-path で network を踏まない)。
    try:
        from src.tools.kev_client import refresh_kev_catalog

        refresh_kev_catalog()
    except Exception as e:  # noqa: BLE001
        _log.warning("kev_refresh_failed", error=str(e))

    article_timeout = _per_article_timeout_seconds()

    async def _handle_article(article: Article) -> _ArticleWork:
        """記事 1 本を処理し、**共有 list を触らずに**寄与分だけを返す。

        並列化 (ARTICLE_CONCURRENCY) しても下流の突合が壊れないよう、結果は
        呼び出し側が入力順に統合する。例外は従来どおりここで拾い切る。
        """
        briefings: list[tuple[str, BriefingMessage]] = []
        article_outcomes: list[dict[str, object]] = []
        errors: list[str] = []
        grok_subarticles: list[tuple[str, Article]] = []
        try:
            if _is_grok_article(article):
                # Grok レポート (JSONL output) を tweet 単位の briefing に展開する。
                # 以降は通常記事と同様に enrichment / routing される。
                expanded = await _grok_article_to_briefings(
                    article,
                    enrichment=enrich,
                    llm=llm,
                    template=template,
                    brief_count_24h=brief_count_24h_snapshot,
                )
                if not expanded:
                    # Grok 報告 (親) は briefing ゼロなら記事 row を一切作らない (2026-08-15)。
                    # 報告の実体はチャットページであり、記事テーブル = 検索面に残骸
                    # (「No X posts collected - Grok」等) を置く場所ではない。
                    # 障害の可視性は run_logs (下記 warning + heartbeat) と枯渇監視が担保する。
                    from src.pipeline.grok_convert import grok_report_is_quiet

                    if grok_report_is_quiet(article):
                        _log.info(
                            "grok_article_quiet_no_events",
                            article_id=article.id,
                            url=article.url,
                        )
                    else:
                        # ハートビート無しの空/散文応答 = プロンプト違反 or 生成不全の疑い
                        _log.warning(
                            "grok_article_yielded_no_briefings",
                            article_id=article.id,
                            url=article.url,
                        )
                    return _ArticleWork.empty()
                for idx, msg in enumerate(expanded):
                    # per-tweet 一意 id (親共有による body/entity 混載バグの修正)
                    sub_id = _grok_subarticle_id(article.id, msg, idx)
                    # 2026-07-05 退行修正: persist 用 map にも登録する。65c5dc4 が
                    # ここを登録し忘れ、_persist_article_outcomes が sub_id を
                    # 「記事不明」で skip → Grok tweet が articles テーブル
                    # (web UI/検索/entity 層) から 6/17 以降消えていた。
                    # (統合側で grok_subarticles / articles_by_id_local 双方へ入れる)
                    grok_subarticles.append((sub_id, article))
                    briefings.append((sub_id, msg))
                    article_outcomes.append(
                        {
                            "article_id": sub_id,
                            "msg": msg,
                            "status": "summarized",
                            "failure_reason": None,
                        },
                    )
            else:
                # per-article バックストップ: 1 記事の暴走を run 全体に波及させない。
                # Grok 経路は 1 記事 = N tweet 展開で正当に長いため対象外。
                process_coro = _process_article(
                    article,
                    extractor,
                    llm,
                    template,
                    think=pipeline.processor.think_enabled,
                    enrichment=enrich,
                    brief_count_24h=brief_count_24h_snapshot,
                )
                if article_timeout is not None:
                    briefing = await asyncio.wait_for(process_coro, timeout=article_timeout)
                else:
                    briefing = await process_coro
                briefings.append((article.id, briefing))
                article_outcomes.append(
                    {
                        "article_id": article.id,
                        "msg": briefing,
                        "status": "summarized",
                        "failure_reason": None,
                    },
                )
        except DegenerateBodyError as e:
            # 監査 #10: ブロック画面/極短本文は要約せず extract_failed で記録。
            # 日常的事象 (bot 壁/paywall) のため errors には計上しない (run を赤くしない)。
            # content 起因なので Phase B の恒久 seen 登録の対象 (再取得ループ防止)。
            _log.info(
                "article_body_degenerate",
                article_id=article.id,
                url=article.url,
                reason=e.reason,
            )
            article_outcomes.append(
                {
                    "article_id": article.id,
                    "msg": None,
                    "status": "extract_failed",
                    "failure_reason": f"degenerate_body: {e.reason}",
                    # 取得側の理由 (http_error_403/js_challenge/timeout 等)。persistence が
                    # extraction_failure_reason 列へ優先記録する (3 切り分けの観測基盤)。
                    "extraction_failure_reason": e.extraction_reason,
                },
            )
        except Exception as e:  # noqa: BLE001
            err = f"{article.id}: {type(e).__name__}: {e}"
            errors.append(err)
            # 監査 2026-07-05 P1: LLM の一時障害 (timeout/接続断) は content 起因でなく
            # インフラ起因。恒久 seen 化せず次 run の RSS 窓で再取得させる (Recall 保護)。
            # per-article timeout (TimeoutError) も同様に transient 扱い (2026-08-01)。
            is_transient = isinstance(e, (LLMTimeoutError, LLMConnectionError, TimeoutError))
            _log.warning(
                "article_processing_failed",
                article_id=article.id,
                url=article.url,
                error_type=type(e).__name__,
                error=str(e),
                transient=is_transient,
            )
            article_outcomes.append(
                {
                    "article_id": article.id,
                    "msg": None,
                    "status": "summarize_failed",
                    "failure_reason": f"{type(e).__name__}: {e}"[:500],
                    "transient_failure": is_transient,
                },
            )
        return _ArticleWork(
            briefings=tuple(briefings),
            outcomes=tuple(article_outcomes),
            errors=tuple(errors),
            grok_subarticles=tuple(grok_subarticles),
        )

    # soft deadline 超過 → 残記事を次 run へ繰越 (既読化しない = リトライ権保持)。
    # 着手済みは完走させ、投稿/既読化/永続化フローに乗せて成果を確定する。
    _works, deferred_count = await _run_articles_bounded(
        articles,
        _handle_article,
        concurrency=_article_concurrency(),
        should_stop=(None if soft_deadline is None else lambda: time.monotonic() >= soft_deadline),
    )
    if deferred_count:
        _log.warning(
            "pipeline_soft_deadline_reached",
            processed=len(articles) - deferred_count,
            deferred=deferred_count,
            time_budget_seconds=time_budget_seconds,
        )
    # 入力順に統合する (並列でも下流の突合と投稿順が変わらない)
    for _work in _works:
        briefings.extend(_work.briefings)
        article_outcomes.extend(_work.outcomes)
        errors.extend(_work.errors)
        for _sub_id, _parent in _work.grok_subarticles:
            grok_subarticles[_sub_id] = _parent
            articles_by_id_local[_sub_id] = _parent

    # 3. 投稿 (dry-run 時は stdout に整形プレビュー)
    if dry_run:
        for art_id, msg in briefings:
            _print_dry_run(art_id, msg, importance_map)
        _persist_article_outcomes(
            outcomes=article_outcomes,
            articles_by_id=articles_by_id_local,
            dedup_repo=dedup_repo,
            run_id=run_id,
        )
        return PipelineRunResult(
            total_fetched=len(articles) + skipped_dup + skipped_dup_semantic,
            skipped_dup=skipped_dup + skipped_dup_semantic,
            summarized=len(briefings),
            posted=0,
            marked_read=0,
            errors=errors,
            dry_run=True,
            deferred_count=deferred_count,
        )

    # article_id → 元 Article のマップ (mark_url_seen / persist 用)。
    # Grok の per-tweet sub-id は親 Article を指す (feed/url/published は親由来で共有)。
    articles_by_id = {a.id: a for a in articles}
    articles_by_id.update(grok_subarticles)

    # Phase 4.5: STIX bundle 添付用に actor registry を 1 度だけロード
    from src.cti.actor_normalizer import load_actor_aliases as _load_actors
    from src.cti.stix_from_briefing import (
        briefing_to_stix_bytes,
        make_attachment_filename,
    )

    try:
        _stix_actor_registry = _load_actors()
    except Exception as e:  # noqa: BLE001
        _log.warning("stix_actor_registry_load_failed", error=str(e))
        _stix_actor_registry = None

    # Phase 5D / H: cross-task の source_post_url 一致による機械 dedup
    briefings, dropped_dup_url = _dedup_briefings_by_source_url(briefings)
    # Phase 5D / L5: incident 本文の embedding コサインで意味的 dedup
    # Phase 5L-2: cross-briefing は再投稿防止系 (hard threshold) を使う
    if enrich.enabled and enrich.semantic_dedup_across_briefings and embedder is not None:
        briefings, dropped_dup_semantic = await _dedup_incidents_semantic(
            briefings,
            embedder=embedder,
            threshold=getattr(pipeline.processor, "similarity_threshold_hard", 0.92),
        )
    else:
        dropped_dup_semantic = 0
    if dropped_dup_url or dropped_dup_semantic:
        _log.info(
            "cross_task_dedup_applied",
            dropped_url=dropped_dup_url,
            dropped_semantic=dropped_dup_semantic,
        )

    # Phase 5D / E: 投稿順序を (priority → daily → research) に固定。
    # 同一チャンネル内では関心度 score 降順 + grok_task_id でセカンダリソート。
    briefings = _sort_briefings_for_posting(briefings, importance_map)

    posted_ids: list[str] = []
    web_only_ids: list[str] = []  # R1: push 抑止し DB 保存のみにした記事 (status='posted')
    # Phase 5J-1: briefings は _sort_briefings_for_posting で並び替えされるため、
    # 元順の article_outcomes の idx で引くと別の briefing の record に書き込まれる
    # (run 85〜95 で 15 件の posted_channel 不整合を観測)。
    # msg オブジェクトの id() をキーとした dict で一意特定する。
    # Grok 経路の 1 article → N section も msg ごとに別 outcome なので衝突しない。
    outcome_by_msg_id: dict[int, dict[str, object]] = {
        id(o["msg"]): o for o in article_outcomes if o["status"] == "summarized" and o.get("msg")
    }
    # Phase 5L-4: ch 横断 dedup の準備。同 dedup_key で複数 ch にまたがる briefing
    # があれば、最も上位の ch (alert > japan_watch > brief > watch > ops) のみ投稿。
    # 履歴 (直近 48h) に同 key が成功投稿されていたらこの run でも skip。
    cross_channel_seen_keys: set[str] = set()
    # Phase 5T-V-2: 正規化 CVE-ID ベースの同 run 内 dedup (LLM dedup_key 不安定対策)。
    cross_channel_seen_cves: set[str] = set()
    # C1: webhook 未設定 ch の fallback 定義 (レジストリ SSoT、run 中は固定)
    channel_fallbacks = fallback_map()
    # 通知再設計: web-only disposition は channel レジストリの push 属性で決める (情報フロー編集)。
    # push=False の tier は Discord push せず DB 保存のみ (run 中は固定)。
    channel_push = push_map()

    def _register_seen(art_id: str) -> None:
        """投稿/web-only 確定後に URL seen + embedding を登録する (再 surface 防止)。

        Phase 3a (URL SHA-256) + Phase 3b (embedding) の seen 登録を post 成功経路と
        web-only 経路で共有する。dedup_repo 不在や記事不明なら no-op。
        """
        if dedup_repo is None:
            return
        seen_article = articles_by_id.get(art_id)
        if seen_article is None:
            return
        h = url_hash(seen_article.url)
        dedup_repo.mark_url_seen(
            url_hash=h,
            url=seen_article.url,
            article_id=art_id,
            title=seen_article.title,
        )
        if art_id in semantic_embeddings:
            emb_model, emb_vec = semantic_embeddings[art_id]
            dedup_repo.add_article_embedding(
                url_hash=h,
                url=seen_article.url,
                vector=emb_vec,
                model=emb_model,
                title=seen_article.title,
            )
        elif semantic_dedup_active:
            # posted なのに embedding 無しを検知できる唯一の地点 (監査 2026-08-01:
            # embed 失敗の無音 no-op がここで月 200+ 件漸増し、ベクトル類似 dedup の
            # 盲点 → クロスソース再投稿の温床になっていた)。生成は dedup 判定時のみ
            # なので、ここで欠けた記事は backfill (scripts/backfill_article_embeddings)
            # が拾う。
            _log.warning(
                "embedding_persist_skipped",
                article_id=art_id,
                url=seen_article.url,
            )

    for art_id, msg in briefings:
        outcome = outcome_by_msg_id.get(id(msg))
        channel = _resolve_channel(msg, importance_map)
        publisher = publishers.get(channel)
        # Phase 5K fallback: webhook 未設定の新規 ch は fallback ch に流す
        if publisher is None and channel in channel_fallbacks:
            fallback = channel_fallbacks[channel]
            _log.info(
                "channel_fallback",
                article_id=art_id,
                requested=channel,
                fallback=fallback,
            )
            channel = fallback
            publisher = publishers.get(channel)
        if publisher is None:
            errors.append(f"{art_id}: no publisher for channel {channel}")
            _log.error("no_publisher_for_channel", channel=channel, article_id=art_id)
            if outcome is not None:
                outcome["status"] = "post_failed"
                outcome["failure_reason"] = f"no publisher for channel {channel}"
            continue
        # 投稿直前 dedup 4 層 (dedup_key 完全一致 → CVE 正規化 → content 署名 →
        # victim_org、2026-08-19 統合) を 1 ゲートに集約 (src/cti/identity_dedup.py)。
        # Phase 5L-8: skip された article id も既読化対象に加える
        # (同一 URL の再 fetch→再評価リサイクル防止)。
        gate_result = check_pre_post_dedup(
            msg=msg,
            art_id=art_id,
            channel=channel,
            dedup_repo=dedup_repo,
            article=articles_by_id.get(art_id),
            cross_channel_seen_keys=cross_channel_seen_keys,
            cross_channel_seen_cves=cross_channel_seen_cves,
        )
        if gate_result is not None:
            if outcome is not None:
                outcome["status"] = "skipped_duplicate"
                outcome["failure_reason"] = gate_result.failure_reason
            skipped_for_mark_read.append(art_id)
            continue

        # 通知再設計: web-only disposition。channel レジストリで push=False の tier (情報フローで
        # 設定。既定は全 tier push=True) は Discord push をスキップし DB 保存のみ (status='posted'
        # 維持で web/分析サーフェスには出る)。dedup 後・STIX/post 前に判定。
        if not channel_push.get(channel, True):
            if outcome is not None:
                outcome["status"] = "posted"
                outcome["posted_channel"] = channel
                if msg.summary:
                    outcome["summary"] = msg.summary
                from src.cti.llm_routing_flags import parse_routing_flags as _prf

                _flags = _prf(msg.metadata.get("routing_flags"))
                if _flags.editorial_stance and _flags.editorial_stance != "unknown":
                    outcome["editorial_stance"] = _flags.editorial_stance
            _register_seen(art_id)
            web_only_ids.append(art_id)
            _log.info("web_only_suppressed", article_id=art_id, channel=channel)
            continue

        # Phase 4.5: IOC / actor / technique がある briefing には STIX bundle を添付
        attachments: list[tuple[str, bytes]] | None = None
        try:
            stix_bytes = briefing_to_stix_bytes(
                msg,
                registry=_stix_actor_registry,
                policy=stix_attach,
            )
            if stix_bytes is not None:
                filename = make_attachment_filename(
                    importance=msg.importance,
                    article_id=art_id,
                )
                attachments = [(filename, stix_bytes)]
                _log.info(
                    "stix_bundle_attached",
                    article_id=art_id,
                    filename=filename,
                    size_bytes=len(stix_bytes),
                )
        except Exception as e:  # noqa: BLE001
            # STIX 構築失敗は本処理 (Discord 投稿) を止めない (graceful degradation)
            _log.warning(
                "stix_bundle_build_failed",
                article_id=art_id,
                error=str(e),
            )
        # Phase B-续报: Discord 投稿前に 168h 内の同 dedup_key 過去 post を検索し、
        # あれば BriefingMessage の metadata に followup_info を注入 (タイトル prefix +
        # embed footer に "続報" 表記)。検出失敗・例外は swallow して post を続行。
        if dedup_repo is not None:
            try:
                from src.cti.followup_detection import annotate_followup

                # Phase B-cal: dedup_key 一致に加え embedding 類似でも続報検出する
                # (dedup_key は LLM slug 変動で完全一致が稀なため semantic を fallback に)。
                _emb = semantic_embeddings.get(art_id)
                msg = annotate_followup(
                    msg,
                    repo=dedup_repo,
                    embedding=_emb[1] if _emb else None,
                    embedding_model=_emb[0] if _emb else "",
                )
            except Exception as e:  # noqa: BLE001
                _log.warning(
                    "followup_annotate_failed",
                    article_id=art_id,
                    error=f"{type(e).__name__}: {e}",
                )
        try:
            post_meta = await publisher.post(msg, attachments=attachments)
            posted_ids.append(art_id)
            if outcome is not None:
                outcome["status"] = "posted"
                outcome["posted_channel"] = channel
                # Phase 5T-K: digest pipeline 用に message_id / channel_id を保存
                if post_meta.message_id:
                    outcome["discord_message_id"] = post_meta.message_id
                if post_meta.channel_id:
                    outcome["discord_channel_id"] = post_meta.channel_id
                # Phase 5T-P: digest 生成用に summary を永続化
                if msg.summary:
                    outcome["summary"] = msg.summary
                # Phase B-R5b 観察: LLM の editorial_stance 判定を DB に永続化
                # (UI 観察ページで集計、prompt 改善ループに使う)
                from src.cti.llm_routing_flags import parse_routing_flags as _prf

                _flags = _prf(msg.metadata.get("routing_flags"))
                if _flags.editorial_stance and _flags.editorial_stance != "unknown":
                    outcome["editorial_stance"] = _flags.editorial_stance
            # 投稿成功した記事の URL を seen 登録 (Phase 3a) + embedding (Phase 3b)
            _register_seen(art_id)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{art_id}: post failed: {type(e).__name__}: {e}")
            _log.error(
                "discord_post_failed",
                article_id=art_id,
                channel=channel,
                error=str(e),
            )
            if outcome is not None:
                outcome["status"] = "post_failed"
                outcome["failure_reason"] = f"{type(e).__name__}: {e}"[:500]

    # Phase X-1 で外部サービス側の既読化 API を撤去。dedup は URL 正規化
    # SHA-256 ハッシュ (dedup_seen_urls) で担保される。
    marked = 0

    # 2026-07-30: dedup skip 経路の URL 既読化 (再 fetch→再評価リサイクルの根治)。
    # skip 経路 (cross-ch / CVE / content dedup) は全て skipped_for_mark_read に集約済のため
    # ここで一括担保する。将来の新 skip 層も append 先が同じなら自動でカバーされる。詳細は
    # _mark_skipped_urls_seen の docstring 参照。
    if not dry_run and dedup_repo is not None:
        _mark_skipped_urls_seen(
            skipped_for_mark_read, articles_by_id, dedup_repo, semantic_embeddings
        )

    # 投稿結果が確定したので articles テーブルに永続化 (Phase 5A: dashboard 用)
    _persist_article_outcomes(
        outcomes=article_outcomes,
        articles_by_id=articles_by_id_local,
        dedup_repo=dedup_repo,
        run_id=run_id,
    )

    result = PipelineRunResult(
        total_fetched=len(articles) + skipped_dup + skipped_dup_semantic,
        skipped_dup=skipped_dup + skipped_dup_semantic,
        summarized=len(briefings),
        posted=len(posted_ids),
        marked_read=marked,
        errors=errors,
        dry_run=False,
        triage_error_count=triage_error_count,
        deferred_count=deferred_count,
    )

    _log.info(
        "pipeline_complete",
        total=result.total_fetched,
        skipped_dup=result.skipped_dup,
        summarized=result.summarized,
        posted=result.posted,
        web_only=len(web_only_ids),  # R1: push 抑止し DB 保存のみにした件数
        marked_read=result.marked_read,
        errors=len(result.errors),
        deferred=result.deferred_count,
    )

    # Phase 5C: system チャンネルへの稼働ステータス通知 (成功 1 行 / 失敗 @here)
    # Phase 5L-1: interval 経路では成功通知を 24h で抑制 (毎時のノイズ削減)
    # dry-run は通知しない (誤投稿を避ける)
    if not dry_run and routing.system_notify_enabled:
        is_interval = pipeline.schedule is not None and pipeline.schedule.is_interval
        notified = await _maybe_post_system_notification(
            publishers,
            pipeline.name,
            result,
            run_id,
            is_interval_run=is_interval,
            dedup_repo=dedup_repo,
        )
        if notified:
            result = result.model_copy(update={"ops_notified": True})

    # Phase 3 (Synthesis): pipeline 完了後の staleness check で daily synthesis を
    # 自動 trigger。article 流入 spike にも追随する near-realtime 更新を実現。
    # 非 dry-run、posted > 0、dedup_repo 注入時のみ。失敗しても run_pipeline を壊さない。
    #
    # **narrative は reasoning ティア (Dense 31b) を使う** (per-article fast=26b を流用しない)。
    # 旧実装は main llm を渡しており、定時 31b 総括を毎時 RSS 完了時に 26b で上書きして
    # 設計意図 (narrative reasoning は Dense 31b) と不一致だった。detect-new は fast ティア
    # (26B)。発火頻度は auto_trigger の debounce (6h) で抑え、毎時 cron との衝突を緩和する。
    if not dry_run and result.posted > 0 and dedup_repo is not None:
        try:
            from src.synthesis.auto_trigger import maybe_trigger_daily_synthesis
            from src.tools.model_tiers import Step, build_llm_for

            synthesis_llm = build_llm_for(Step.SYNTHESIS_NARRATIVE, config)
            analysis_llm = build_llm_for(Step.SYNTHESIS_ANALYSIS, config)
            fast_llm = build_llm_for(Step.SYNTHESIS_DETECT, config)
            await maybe_trigger_daily_synthesis(
                llm=synthesis_llm,
                repo=dedup_repo,
                fast_llm=fast_llm,
                analysis_llm=analysis_llm,
            )
        except Exception as e:  # noqa: BLE001
            _log.warning(
                "synthesis_auto_trigger_unexpected_error",
                error=f"{type(e).__name__}: {e}",
            )

    return result
