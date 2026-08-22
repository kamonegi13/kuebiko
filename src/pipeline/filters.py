"""記事フィルタ群: dedup / triage / thin-body prefetch / semantic dedup (src.main から分割)。"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from src.config_loader import AppConfig
from src.logging_config import get_logger
from src.pipeline.grok_convert import _is_grok_article
from src.storage.run_history import RunHistoryRepository
from src.tools.article_model import Article
from src.tools.content_extractor import ContentExtractor, check_extracted_identity
from src.tools.embedding_client import EmbeddingClient, EmbeddingError
from src.tools.llm_client import LLMClient
from src.tools.text_utils import strip_html as _strip_html
from src.tools.url_normalizer import url_hash

_log = get_logger(__name__)

# triage の同時実行数 (2026-08-17)。既定 5 = 従来どおり。
#
# triage は「長い入力 → ごく短い出力」= prefill 主体。prefill は 1 リクエストだけで
# GPU 演算器を飽和させるため、束ねても **速くも遅くもならない**。実測 (gemma4:26b、
# 4 件 x 3 往復 ABABAB、OLLAMA_NUM_PARALLEL=4): 逐次 9.84s / 並列 9.89s = 1.00x。
#
# ⚠ 当初「並列で 0.68x に悪化する」と記録したが、これは 1 回ずつの単発測定によるノイズで
# 反復すると再現しなかった。**triage の並列度は速度上どちらでもよい** (既定 5 のまま)。
# 速度に効くのは decode 主体の記事処理側 (ARTICLE_CONCURRENCY、実測 1.85x)。
_TRIAGE_CONCURRENCY_DEFAULT = 5
_TRIAGE_CONCURRENCY_MAX = 8


def _triage_concurrency() -> int:
    """triage の同時実行数を解決する (env override → 既定 5、壊れた値は既定へ)。"""
    raw = os.environ.get("TRIAGE_CONCURRENCY", "").strip()
    if not raw:
        return _TRIAGE_CONCURRENCY_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        return _TRIAGE_CONCURRENCY_DEFAULT
    if value < 1:
        return _TRIAGE_CONCURRENCY_DEFAULT
    return min(value, _TRIAGE_CONCURRENCY_MAX)


def _filter_duplicates(
    articles: list[Article],
    dedup_repo: RunHistoryRepository,
) -> tuple[list[Article], int, list[str]]:
    """URL ハッシュベースで既出記事を除外する (Phase 3a)。

    戻り値: (重複を除いた記事リスト, スキップ件数, スキップした article id リスト)。
    skipped_ids は呼び出し側で dedup 既読化 (次 run の再評価リサイクル防止) に使う。
    """
    # まず article ごとに hash を計算してバルク問い合わせ
    hashes = [url_hash(a.url) for a in articles]
    seen = dedup_repo.filter_unseen_hashes(hashes)
    # Phase B-cal: DB 既出 (seen) に加え、**同一バッチ内の URL 重複** も除外する。
    # 同じ記事が複数 feed 購読 (例: Google blog / Ifri を 2 つの feed 名で購読) から
    # 同一 run に入ると、どちらも DB 未登録のため両方 survive → 重複投稿していた。
    batch_seen: set[str] = set()
    survivors: list[Article] = []
    skipped_ids: list[str] = []
    for article, h in zip(articles, hashes, strict=True):
        if h in seen:
            skipped_ids.append(article.id)
            _log.info("dedup_skipped_url", article_id=article.id, url=article.url)
            continue
        if h in batch_seen:
            skipped_ids.append(article.id)
            _log.info("dedup_skipped_intra_batch", article_id=article.id, url=article.url)
            continue
        batch_seen.add(h)
        survivors.append(article)
    return survivors, len(skipped_ids), skipped_ids


async def _filter_by_triage(
    articles: list[Article],
    llm: LLMClient,
    *,
    keep_importance: set[str],
    max_keep: int,
    think: bool = False,
) -> tuple[list[Article], int, list[str], int, list[Article], list[tuple[Article, str, bool]]]:
    """軽量 LLM で重要度判定し、threshold 以上の記事のみ通す (Phase 3.1)。

    Grok 経路の記事は triage 対象外 (元から重要度を内包しているため)。
    importance ランク順 (high → medium → low) に並び替え、上位 ``max_keep`` 件
    までで打ち切る。

    ``think`` は thinking モード (True で深い推論、False で高速生成)。

    戻り値:
        survivors: triage 通過した記事リスト
        skipped: フィルタで落とした件数 (Grok 記事を除く全記事 - 通過分)
        skipped_ids: フィルタで落とした article id リスト
            (呼び出し側で dedup 既読化に使う)
        triage_error_count: LLM 失敗で medium fail-open した件数 (Phase 5P)
        rejected: **評価の結果 importance 不足で不採用**とした記事 (skipped の部分集合)。
            呼び出し側が URL 既読化する = 判断済みの終端状態 (2026-07-12)。
            max_keep の枠あふれ (評価は通ったが予算切り) は含めない —
            未採用でなく未処理であり、次 run のリトライ権を保持する。
        decisions: triage 対象全記事の (article, importance, error)。ヘッド v0 の
            シャドー記録 (§14.3) が棄却分も含む全判定を必要とするため露出する。
            Grok バイパス記事は含まない。
    """
    from src.tools.article_triage import ArticleTriage  # 遅延インポート (循環回避)

    triage = ArticleTriage(llm, think=think)

    # Grok 記事はバイパス (重要度判定済)、RSS / scraper 由来のみ triage する
    grok_articles: list[Article] = []
    triage_targets: list[Article] = []
    for a in articles:
        if _is_grok_article(a):
            grok_articles.append(a)
        else:
            triage_targets.append(a)

    if not triage_targets:
        return articles, 0, [], 0, [], []

    # 並列 triage (既定 5 — Ollama サーバへの負荷バランス)
    sem = asyncio.Semaphore(_triage_concurrency())

    async def _one(article: Article) -> tuple[Article, str, bool]:
        async with sem:
            decision = await triage.triage(article)
            _log.info(
                "triage_decision",
                article_id=article.id,
                title=(article.title or "")[:80],
                importance=decision.importance,
                reason=decision.reason[:80],
            )
            return article, decision.importance, decision.error

    decisions: list[tuple[Article, str, bool]] = await asyncio.gather(
        *[_one(a) for a in triage_targets],
    )

    # Phase 5P: LLM 失敗 (fail-open) の件数を集計
    triage_error_count = sum(1 for _, _, err in decisions if err)

    # importance ランクで並び替え
    importance_rank = {"high": 0, "medium": 1, "low": 2}
    decisions.sort(key=lambda x: importance_rank.get(x[1], 3))

    kept: list[Article] = []
    rejected: list[Article] = []  # importance 不足 = 評価済み・不採用 (既読化対象)
    for article, importance, _err in decisions:
        if importance not in keep_importance:
            rejected.append(article)
        elif len(kept) < max_keep:
            kept.append(article)
        # else: 枠あふれ (評価は keep 水準) — skipped には数えるが rejected ではない

    kept_ids = {a.id for a in kept}
    skipped_ids = [a.id for a in triage_targets if a.id not in kept_ids]
    survivors = grok_articles + kept
    return survivors, len(skipped_ids), skipped_ids, triage_error_count, rejected, decisions


async def _prefetch_thin_bodies(
    articles: list[Article],
    *,
    min_chars: int,
    max_concurrency: int = 5,
) -> tuple[list[Article], int]:
    """Phase 3 (収集の深掘り): thin feed 記事の本文を triage 前に先行抽出する。

    RSS が title + 短い description しか返さない feed は triage が薄い snippet だけで
    判定し、重要記事 (例: 中国/北朝鮮 APT 関連) を誤って低評価しがち。trafilatura
    (LLM 不要) で本文を取得して ``body_text`` を埋めた Article に差し替え、triage の
    判定材料を厚くする。survivor は本処理 (_process_article) で再抽出しない。

    Grok 記事 (triage 対象外) と既に厚い記事はスキップ。抽出失敗はそのまま通す
    (graceful、body_text=None のまま従来挙動)。戻り値: (enriched, prefetched_count)。
    """
    from src.tools.text_utils import strip_html

    targets_idx = [
        i
        for i, a in enumerate(articles)
        if not _is_grok_article(a)
        and not a.body_text
        and len(strip_html(a.summary_html or "")) < min_chars
    ]
    if not targets_idx:
        return articles, 0

    enriched = list(articles)
    sem = asyncio.Semaphore(max_concurrency)

    async def _one(idx: int) -> None:
        async with sem:
            a = articles[idx]
            try:
                extraction = await extractor.extract(a.url)
            except Exception as e:  # noqa: BLE001
                _log.info("thin_prefetch_failed", article_id=a.id, error=str(e))
                return
            # 取得成功でも **別記事の本文** が入ることがある (2026-08-18 実測 0.4%)。
            # 現時点では棄却せず記録のみ — 閾値を勘で決めて良い記事を捨てないため。
            check_extracted_identity(extraction, a.title, article_id=a.id)
            if extraction.success and extraction.text.strip():
                enriched[idx] = a.model_copy(update={"body_text": extraction.text})

    async with ContentExtractor() as extractor:
        await asyncio.gather(*[_one(i) for i in targets_idx])

    prefetched = sum(1 for i in targets_idx if enriched[i].body_text)
    return enriched, prefetched


async def _filter_semantic_duplicates(
    articles: list[Article],
    dedup_repo: RunHistoryRepository,
    embedder: EmbeddingClient,
    *,
    threshold_hard: float,
    threshold_cluster: float,
    window_hours_hard: int,
    window_hours_cluster: int,
) -> tuple[list[Article], int, dict[str, tuple[str, list[float]]], list[str]]:
    """embedding コサイン類似度で意味的重複をスキップする (Phase 5L-2: 2 段階)。

    Args:
        threshold_hard: 「ほぼ同一記事」を弾く高 threshold (URL 違い再投稿防止)
        threshold_cluster: 「同事象別ソース」を弾く低 threshold (続報の集約)
        window_hours_hard: hard 判定の比較対象時間窓 (時間、0 で全期間)
        window_hours_cluster: cluster 判定の時間窓 (短期、続報取りこぼし回避)

    判定: hard / cluster いずれかにヒットすれば skip 扱い。

    戻り値:
        survivors: 重複でない記事リスト
        skipped: スキップ件数
        embeddings_to_persist: 投稿後に保存する {article_id: (model, vector)} マップ
        skipped_ids: スキップした article id リスト (dedup 既読化対象)
    """
    survivors: list[Article] = []
    skipped_ids: list[str] = []
    embeddings_to_persist: dict[str, tuple[str, list[float]]] = {}
    # R-D (dedup の時間的完全性): 永続ストアは投稿後にしか更新されないため、同一バッチ内
    # の同事象 (同日・別ソース) が互いに照合されず両方生き残る。実行中バッチの生存
    # embedding (正規化済) とも比較してこの穴を塞ぐ。
    import numpy as np

    batch_units: list[Any] = []
    batch_ids: list[str] = []

    for article in articles:
        text = _embedding_input_text(article)
        try:
            response = await embedder.embed(text)
        except EmbeddingError as e:
            # embedding 失敗時は graceful degradation (この記事はスキップせず続行)
            _log.warning(
                "semantic_dedup_embed_failed",
                article_id=article.id,
                url=article.url,
                error=str(e),
            )
            survivors.append(article)
            continue

        # ⚠ **判定の前に記録する** (2026-08-19)。従来は survivor だけを記録していたため、
        # 重複で落とした記事は 14 日 239 件すべて embedding が消え、「別の層なら捕まえ
        # られたか」「閾値変更で新たに落ちた記事は妥当か」を後から検証できなかった。
        # 落とす判断こそ根拠が要る。呼出側 (orchestrator) が skip 経路でも永続化する。
        embeddings_to_persist[article.id] = (response.model, list(response.vector))

        # 1. hard 判定 (再投稿防止 / 長窓 + 高 threshold)
        match_hard = dedup_repo.find_similar_embedding(
            response.vector,
            model=response.model,
            threshold=threshold_hard,
            window_hours=window_hours_hard,
        )
        if match_hard is not None:
            matched_hash, similarity = match_hard
            skipped_ids.append(article.id)
            _log.info(
                "dedup_skipped_semantic_match",
                article_id=article.id,
                url=article.url,
                similar_to=matched_hash,
                similarity=round(similarity, 4),
                tier="hard",
            )
            continue
        # 2. cluster 判定 (同事象別ソース / 短窓 + 低 threshold)
        # Phase 5T-L: Grok report は構造的に類似 (毎日同じテンプレ + 共通 section
        # 見出し) のため、article 単位の cluster tier で誤 skip される (過去 30 日で
        # 7 件全件 skip、月 35-50 incident 失損)。Grok 経路は hard tier のみで判定し
        # cluster tier を bypass する (真の content コピーは hard 0.92 で捕捉)。
        # incident 単位 dedup (案 C) は data 分析で追加価値が薄いと判定済。
        if _is_grok_article(article):
            _log.info(
                "dedup_skipped_semantic_cluster_bypass_grok",
                article_id=article.id,
                url=article.url,
                reason="grok_structural_similarity_bypass",
            )
        else:
            match_cluster = dedup_repo.find_similar_embedding(
                response.vector,
                model=response.model,
                threshold=threshold_cluster,
                window_hours=window_hours_cluster,
            )
            if match_cluster is not None:
                matched_hash, similarity = match_cluster
                skipped_ids.append(article.id)
                _log.info(
                    "dedup_skipped_semantic_match",
                    article_id=article.id,
                    url=article.url,
                    similar_to=matched_hash,
                    similarity=round(similarity, 4),
                    tier="cluster",
                )
                continue

        # 3. intra-batch 判定 (R-D): 実行中バッチで既に生き残った記事との cosine。
        # 永続ストアにまだ無い同バッチ記事同士の同事象漏れを塞ぐ。Grok は hard、
        # それ以外は cluster threshold (永続ストア比較と同じ基準)。
        nvec = np.asarray(response.vector, dtype=np.float32)
        nrm = float(np.linalg.norm(nvec))
        unit = nvec / nrm if nrm > 0.0 else nvec
        if nrm > 0.0 and batch_units:
            sims = np.stack(batch_units) @ unit
            bi = int(np.argmax(sims))
            bsim = float(sims[bi])
            intra_threshold = threshold_hard if _is_grok_article(article) else threshold_cluster
            if bsim >= intra_threshold:
                skipped_ids.append(article.id)
                _log.info(
                    "dedup_skipped_semantic_match",
                    article_id=article.id,
                    url=article.url,
                    similar_to=batch_ids[bi],
                    similarity=round(bsim, 4),
                    tier="intra_batch",
                )
                continue

        survivors.append(article)
        if nrm > 0.0:
            batch_units.append(unit)
            batch_ids.append(article.id)

    return survivors, len(skipped_ids), embeddings_to_persist, skipped_ids


def _embedding_input_text(article: Article) -> str:
    """embedding 入力文字列。タイトル + 本文先頭の HTML 除去テキスト。"""
    body = _strip_html(article.summary_html)[:1500]
    return f"{article.title}\n\n{body}".strip()


def _try_build_embedder(config: AppConfig) -> EmbeddingClient | None:
    """設定があれば EmbeddingClient を組み立てる。失敗時は None で graceful degradation。"""
    from src.tools.model_tiers import resolve_embedding_model

    model = resolve_embedding_model()
    if not model:
        # Phase 0 Q5: silent failure 低減。embedder 無しでは semantic dedup (4 層中 2 層:
        # 0.92 hard + cluster) が無効化されるため、INFO でなく WARNING で可視化する。
        _log.warning(
            "embedding_disabled",
            reason="OLLAMA_EMBED_MODEL not set",
            impact="semantic dedup (embedding cosine) is OFF; only url-hash + Jaccard active",
        )
        return None
    try:
        from src.tools.embedding_client import OllamaEmbeddingClient

        return OllamaEmbeddingClient(
            base_url=config.ollama_base_url,
            model=model,
            query_prefix=config.ollama_embed_query_prefix,
        )
    except Exception as e:  # noqa: BLE001
        _log.warning("embedding_client_init_failed", error=str(e))
        return None
