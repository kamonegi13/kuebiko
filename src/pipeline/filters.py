"""記事フィルタ群: dedup / triage / thin-body prefetch / semantic dedup (src.main から分割)。"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlparse

from src.config_loader import AppConfig
from src.cti.ingest_relevance import ingest_relevance_hint
from src.logging_config import get_logger
from src.pipeline.grok_convert import _is_grok_article
from src.pipeline.policy_rescue import policy_rescue_mode, rescue_cyber_policy
from src.storage.repo_triage_rejections import TriageRejectionRow
from src.storage.repo_triage_shadow import TriageShadowRow
from src.storage.run_history import RunHistoryRepository
from src.tools.article_model import Article
from src.tools.content_extractor import ContentExtractor, check_extracted_identity
from src.tools.embedding_client import EmbeddingClient, EmbeddingError
from src.tools.llm_client import LLMClient
from src.tools.text_utils import strip_html as _strip_html
from src.tools.url_normalizer import url_hash

if TYPE_CHECKING:
    from src.tools.article_triage import TriageDecision

_log = get_logger(__name__)

# 意味 dedup の skip tier (§8b: 記録のみ・裏取り算入はしない)
SkipTier = Literal["hard", "cluster", "intra_batch"]
# skip 記録の照合種別: hard/cluster は永続ストアの url_hash、intra_batch はバッチ内 article_id
MatchedKind = Literal["url_hash", "article_id"]


@dataclass(frozen=True)
class SemanticSkip:
    """意味 dedup で破棄した記事の記録 (docs/event_news_design.md §8b)。

    v1 では **記録の蓄積のみ** — cluster tier 破棄を裏取りに算入する判断は分離・後置
    (近重複から独立性を製造する Goodhart を避けるため)。**filters.py は record を
    返すだけで DB 書込・dry_run 判定を持ち込まない** (レビュー A H3、書込は
    orchestrator の既存 ``not dry_run`` ブロックが担う)。
    """

    skipped_url: str
    skipped_title: str
    skipped_host: str
    feed_title: str
    feed_url: str
    tier: SkipTier
    matched_kind: MatchedKind
    matched_key: str


def _semantic_skip(
    article: Article,
    *,
    tier: SkipTier,
    matched_kind: MatchedKind,
    matched_key: str,
) -> SemanticSkip:
    """1 件の意味 dedup skip から ``SemanticSkip`` を組み立てる。

    host は article.url から都度導出する (Article に host フィールドは無いため)。
    feed_title/feed_url は Article に既存のフィールドをそのまま使う。
    """
    host = (urlparse(article.url).hostname or "").lower()
    return SemanticSkip(
        skipped_url=article.url,
        skipped_title=article.title,
        skipped_host=host,
        feed_title=article.feed_title,
        feed_url=article.feed_url,
        tier=tier,
        matched_kind=matched_kind,
        matched_key=matched_key,
    )


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


_INGEST_RULE_V2_ENV = "INGEST_RULE_V2"


def _ingest_rule_v2_enabled() -> bool:
    """M4 の取り込みルール (2026-10-08): ``flat triage >= medium OR 取り込みヒント``。

    既定 OFF = 従来どおり importance (triage の判定) のみで足切り。ON にする意味は
    ``TRIAGE_FLAT=1`` (平たい triage) と組み合わせたときのみ — 関連性を見ない平たい判定が
    日本・SIR・注視国の記事を落とさないための安全網 (docs/importance_relevance_redesign.md
    §6 M4)。triage が旧来の関連性込み判定のままでも hint の OR 自体は害にならないが、
    その場合は二重に関連性を効かせることになるため非推奨。
    """
    return os.environ.get(_INGEST_RULE_V2_ENV, "0").strip() in ("1", "true", "yes", "on")


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
    rescue_llm: LLMClient | None = None,
    relevance_embedder: EmbeddingClient | None = None,
    relevance_cascade_llm: LLMClient | None = None,
) -> tuple[list[Article], int, list[str], int, list[TriageRejectionRow], list[TriageShadowRow]]:
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
            判定の重要度と理由つき (呼び出し側が triage_rejections に記録する、2026-10-02)。
            呼び出し側が URL 既読化する = 判断済みの終端状態 (2026-07-12)。
            max_keep の枠あふれ (評価は通ったが予算切り) は含めない —
            未採用でなく未処理であり、次 run のリトライ権を保持する。
        shadow_rows: M4 の影子記録 (``src.tools.triage_shadow``、2026-10-08)。
            現行判定と「平たい triage + 取り込みヒント」を並べて記録する安全網。
            本番の判定・配信には一切影響しない (呼び出し側が DB に保存するかは任意)。

    ``relevance_embedder`` / ``relevance_cascade_llm`` (M4、2026-10-08): 日本関連性 ML
    カスケードに使う埋込クライアント・LLM。いずれか None なら ML 側は skip (jp_prob 等は
    None で記録)。dedup 用に既に構築済みの embedder を再利用する想定 (同じ生産埋込モデル)。
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

    async def _one(article: Article) -> tuple[Article, str, bool, str]:
        async with sem:
            decision = await triage.triage(article)
            _log.info(
                "triage_decision",
                article_id=article.id,
                title=(article.title or "")[:80],
                importance=decision.importance,
                reason=decision.reason[:80],
            )
            return article, decision.importance, decision.error, decision.reason

    decisions: list[tuple[Article, str, bool, str]] = await asyncio.gather(
        *[_one(a) for a in triage_targets],
    )

    # Phase 5P: LLM 失敗 (fail-open) の件数を集計
    triage_error_count = sum(1 for _, _, err, _ in decisions if err)

    rescued: set[str] = set()
    if rescue_llm is not None and os.environ.get("TRIAGE_GEO_RESCUE", "1") != "0":
        decisions, rescued = await _rescue_geopolitical(
            decisions, rescue_llm, keep_importance=keep_importance, think=think
        )

    # サイバー政策の救済 (2026-10-10)。CYBER_POLICY_RESCUE=1 で適用 / shadow で記録のみ
    policy_mode = policy_rescue_mode()
    if rescue_llm is not None and policy_mode != "off":
        decisions, policy_rescued = await rescue_cyber_policy(
            decisions, rescue_llm, shadow=policy_mode == "shadow"
        )
        rescued = rescued | policy_rescued

    # importance ランクで並び替え。救済した記事は同じ重要度の中で後ろ (枠あふれで先に押し出す)
    importance_rank = {"high": 0, "medium": 1, "low": 2}
    decisions.sort(key=lambda x: (importance_rank.get(x[1], 3), x[0].id in rescued))

    ingest_rule_v2 = _ingest_rule_v2_enabled()
    kept: list[Article] = []
    rejected: list[TriageRejectionRow] = []  # importance 不足 = 評価済み・不採用 (既読化対象)
    for article, importance, _err, reason in decisions:
        if importance not in keep_importance:
            # M4 (2026-10-08): INGEST_RULE_V2=1 なら、importance 不足でも取り込みヒント
            # (日本・注視国・関連性の核 SIR) が発火すれば落とさない。TRIAGE_FLAT=1 と
            # 組み合わせて使う想定 (§6)。hint は決定論・LLM 不使用なので全件に適用して安い。
            hint_fired = False
            if ingest_rule_v2:
                hint = ingest_relevance_hint(
                    feed=(article.feed_title or "").strip(),
                    title=(article.title or "").strip(),
                    summary_preview=triage._triage_content(article),
                )
                hint_fired = hint.fired
            if hint_fired and len(kept) < max_keep:
                kept.append(article)
                continue
            rejected.append(
                TriageRejectionRow(
                    article_id=article.id,
                    url=article.url,
                    title=article.title or "",
                    feed_title=article.feed_title or "",
                    feed_url=article.feed_url or "",
                    importance=importance,
                    reason=reason,
                )
            )
        elif len(kept) < max_keep:
            kept.append(article)
        # else: 枠あふれ (評価は keep 水準) — skipped には数えるが rejected ではない

    kept_ids = {a.id for a in kept}
    skipped_ids = [a.id for a in triage_targets if a.id not in kept_ids]
    survivors = grok_articles + kept

    # M4 影子記録 (2026-10-08): 現行判定 (importance in keep_importance、max_keep の
    # 枠あふれは数えない) と平たい triage + 取り込みヒントを並べて記録する安全網。
    # TRIAGE_SHADOW_PER_RUN=0 または対象無しなら no-op (shadow_rows=[])。
    shadow_rows: list[TriageShadowRow] = []
    try:
        from src.tools.triage_shadow import run_triage_shadow

        shadow_decisions = [
            (article, importance, importance in keep_importance)
            for article, importance, _err, _reason in decisions
        ]
        shadow_rows = await run_triage_shadow(
            shadow_decisions,
            llm=llm,
            keep_importance=keep_importance,
            think=think,
            relevance_embedder=relevance_embedder,
            relevance_cascade_llm=relevance_cascade_llm,
        )
    except Exception as e:  # noqa: BLE001 — 影子記録の失敗は本処理を止めない
        _log.warning("triage_shadow_run_failed", error=str(e)[:200])

    return survivors, len(skipped_ids), skipped_ids, triage_error_count, rejected, shadow_rows


#: 救済の対象 = triage が地政学・軍事・外交を理由に落とした記事 (理由の欄で判定する)
_GEO_REASON = re.compile(r"(地政学|軍事|外交|安全保障|海軍|演習|ミサイル|防衛|国防|核)")
_RESCUE_MARK = "[地政学の救済] "


async def _rescue_geopolitical(
    decisions: list[tuple[Article, str, bool, str]],
    rescue_llm: LLMClient,
    *,
    keep_importance: set[str],
    think: bool,
) -> tuple[list[tuple[Article, str, bool, str]], set[str]]:
    """地政学を理由に落ちた記事を、救済用のモデルで同じ基準のまま判定し直す (2026-10-02)。

    triage の SFT モデル (s21) は「一般地政学 = low」を学習しており、地政学の SIR
    (サイバー以外) を足しても癖が判定基準より強く効く。実測: 落ちた注視国の地政学 60 件で
    medium 以上は s21 21 件 / 素の 26B 42 件、範囲外 30 件の誤取込は 1 / 3 件。
    救った記事は **medium に抑える** (サイバー要素のない軍事は high にしない、09-29 の
    利用者決定)。救済の判定に失敗したら元の判定 (low) のまま。
    """
    from src.tools.article_triage import ArticleTriage  # 遅延インポート (循環回避)

    targets = [
        i
        for i, (_a, imp, err, reason) in enumerate(decisions)
        if imp == "low" and not err and _GEO_REASON.search(reason or "")
    ]
    if not targets:
        return decisions, set()
    triage = ArticleTriage(rescue_llm, think=think)
    sem = asyncio.Semaphore(_triage_concurrency())

    async def _one(i: int) -> tuple[int, TriageDecision]:
        async with sem:
            return i, await triage.triage(decisions[i][0])

    out = list(decisions)
    rescued: set[str] = set()
    for i, d in await asyncio.gather(*(_one(i) for i in targets)):
        if d.error or d.importance not in keep_importance:
            continue
        article = out[i][0]
        out[i] = (article, "medium", False, _RESCUE_MARK + d.reason)
        rescued.add(article.id)
    _log.info("triage_geo_rescue", candidates=len(targets), rescued=len(rescued))
    return out, rescued


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
    cluster_judge: Callable[[Article, str], Awaitable[bool | None]] | None = None,
) -> tuple[list[Article], int, dict[str, tuple[str, list[float]]], list[str], list[SemanticSkip]]:
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
        skip_records: skip 1 件ごとの ``SemanticSkip`` (docs/event_news_design.md §8b)。
            DB 書込は呼び出し側 (orchestrator) が担う — このモジュールは record を
            返すだけ (レビュー A H3)。
    """
    survivors: list[Article] = []
    skipped_ids: list[str] = []
    skip_records: list[SemanticSkip] = []
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
            skip_records.append(
                _semantic_skip(
                    article, tier="hard", matched_kind="url_hash", matched_key=matched_hash
                )
            )
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
                # ⭐ 一方向の救済網 (2026-09-03): cluster 帯の 10.2% が別内容だった
                #    (同一ベンダの別製品など)。duplicate と確答したときだけ skip。
                if cluster_judge is not None:
                    verdict = await cluster_judge(article, matched_hash)
                    if verdict is not True:
                        _log.info(
                            "dedup_cluster_rescued",
                            article_id=article.id,
                            url=article.url,
                            similar_to=matched_hash,
                            similarity=round(similarity, 4),
                            verdict="different" if verdict is False else "unclear",
                        )
                        match_cluster = None
            if match_cluster is not None:
                matched_hash, similarity = match_cluster
                skipped_ids.append(article.id)
                skip_records.append(
                    _semantic_skip(
                        article,
                        tier="cluster",
                        matched_kind="url_hash",
                        matched_key=matched_hash,
                    )
                )
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
                skip_records.append(
                    _semantic_skip(
                        article,
                        tier="intra_batch",
                        matched_kind="article_id",
                        matched_key=batch_ids[bi],
                    )
                )
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

    return survivors, len(skipped_ids), embeddings_to_persist, skipped_ids, skip_records


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
            reason="embedding tier unassigned",
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
