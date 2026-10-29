"""記事本文の自動翻訳ジョブ (2026-07-25。UI 表示名「本文自動翻訳」、job id は互換維持)。

毎時、未訳の記事 (body あり・body_ja なし) を **新しい順に** 少量ずつローカル LLM
(Step.ARTICLE_TRANSLATE = fast ティア) で全訳し body_ja にキャッシュする。

設計 (on-demand 翻訳の追加層。置き換えではない):
- 新規流入 (~470 件/日、実測 2026-07-25) は 40 件/時 x 24 で当日中に追いつく。
  過去分 (~22k 件) は残余キャパで後ろから漸進消化。
- 実測スループット ~208 字/秒 (warm 26B) → 平均 3.9k 字 ≈ 19 秒/件。
  batch 40 件 + 時間予算 12 分で毎時スロット (:15-:27) に収まり、
  RSS 収集 (:00 台) / scraper (:30) と重ならない。
- resumable (2026-08-06): 記事内はチャンク単位で確定保存 (body_ja_chunks)。
  失敗・予算切れの途中結果は残り、次周期が続きから再開する — 保存本文の
  100k 化 (body_limits.py) に伴う長文の毎時全損リトライを構造的に防ぐ。
  時間予算はチャンク境界でも効く (長文 1 件が予算を大幅超過しない)。
- ジョブ無効化 (UI ジョブ管理) で即 on-demand 専用運用に戻る (rollback 自明)。
"""

from __future__ import annotations

import os
import time

from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository
from src.tools.llm_client import LLMClient, LLMError

_log = get_logger(__name__)

# 1 run の上限件数と時間予算。毎時スロット (:15 開始) が :30 の scraper に
# かからないよう、予算は 12 分に抑える (40 件 x ~19 秒 ≈ 13 分だが予算が先に効く)。
_BATCH_LIMIT = 40
_TIME_BUDGET_SECONDS = 12 * 60

#: 失敗が続く記事を選ばない (P0-24)。コード既定 1、``TRANSLATE_QUEUE_BACKOFF=0`` で従来の選び方
TRANSLATE_QUEUE_BACKOFF_ENV = "TRANSLATE_QUEUE_BACKOFF"
_FLAG_OFF_VALUES = frozenset({"0", "false", "off", "no"})
_REASON_MAX_CHARS = 200


def queue_backoff_enabled() -> bool:
    raw = os.environ.get(TRANSLATE_QUEUE_BACKOFF_ENV, "1").strip().lower()
    return raw not in _FLAG_OFF_VALUES


def _record_failure(repo: RunHistoryRepository, aid: str, error: Exception) -> bool:
    """失敗の印を付ける。印の失敗で本処理を止めず、失敗したら False (呼び手が計数)。"""
    try:
        repo.record_translate_failure(aid, f"{type(error).__name__}: {error}"[:_REASON_MAX_CHARS])
    except Exception as e:  # noqa: BLE001 — 印の記録失敗で翻訳ジョブを止めない
        _log.warning("translate_attempt_record_failed", article_id=aid, error=type(e).__name__)
        return False
    return True


def _clear_attempts(repo: RunHistoryRepository, aid: str) -> None:
    try:
        repo.clear_translate_attempts(aid)
    except Exception as e:  # noqa: BLE001 — 消し損ねても次の成功判定で消える
        _log.warning("translate_attempt_clear_failed", article_id=aid, error=type(e).__name__)


async def run_body_translate_backlog(
    repo: RunHistoryRepository | None = None,
    llm: LLMClient | None = None,
    *,
    batch_limit: int = _BATCH_LIMIT,
    time_budget_seconds: float = _TIME_BUDGET_SECONDS,
) -> dict[str, int]:
    """未訳記事を新しい順に翻訳して body_ja に保存する。統計 dict を返す。"""
    from src.cti.body_translator import (
        TranslationEchoError,
        TranslationGarbledError,
        is_probably_japanese,
        translate_body_resumable,
    )

    repo = repo or RunHistoryRepository()
    if llm is None:
        from src.config_loader import load_app_config
        from src.tools.model_tiers import Step, build_llm_for

        llm = build_llm_for(Step.ARTICLE_TRANSLATE, load_app_config())

    article_ids = repo.list_articles_untranslated(
        limit=batch_limit, backoff=queue_backoff_enabled()
    )
    started = time.monotonic()
    translated = 0
    failed = 0
    partial = 0
    skipped_japanese = 0
    skipped_garbled = 0
    skipped_budget = 0
    echo_failed = 0
    record_errors = 0
    for aid in article_ids:
        remaining = time_budget_seconds - (time.monotonic() - started)
        if remaining <= 0:
            skipped_budget = (
                len(article_ids)
                - translated
                - failed
                - partial
                - skipped_japanese
                - skipped_garbled
            )
            break
        body = repo.get_article_body(aid)
        if not body or not body.strip():
            continue
        if is_probably_japanese(body):
            # 日本語原文は翻訳不要。'' で「処理済」を記録し次周期の対象から外す
            # (訳させると LLM が逆方向 (日→英) に翻訳して壊れる)。
            repo.update_article_body_ja(aid, "")
            skipped_japanese += 1
            continue
        try:
            # 残余予算を deadline として渡す — 長文 1 件でもチャンク境界で必ず
            # 予算内に止まり (:30 の scraper スロットへ食い込まない)、途中結果は
            # body_ja_chunks に確定済みなので次周期が続きから再開する。
            progress = await translate_body_resumable(
                llm, body, article_id=aid, store=repo, deadline_seconds=remaining
            )
        except TranslationGarbledError as e:
            # 化けた訳は保存せず「翻訳なし」(body_ja='') で処理済みにする。同じ位置で化けるため
            # 再試行しない (毎時の無限再試行を防ぐ)。画面は原文を出す。
            repo.update_article_body_ja(aid, "")
            repo.clear_body_ja_chunks(aid)
            skipped_garbled += 1
            _log.info("body_backlog_translate_garbled", article_id=aid, error=str(e))
            continue
        except LLMError as e:
            # 1 件の失敗でジョブを落とさない。訳せた分はチャンク保存済みのため、
            # 次周期の再試行は失敗チャンクから続く (毒薬記事の毎時全損を防ぐ)。
            failed += 1
            echo_failed += isinstance(e, TranslationEchoError)
            record_errors += not _record_failure(repo, aid, e)
            _log.warning("body_backlog_translate_failed", article_id=aid, error=str(e))
            continue
        if progress.text is None:
            partial += 1
            _log.info(
                "body_backlog_translate_partial",
                article_id=aid,
                done_chunks=progress.done_chunks,
                total_chunks=progress.total_chunks,
            )
            continue
        repo.update_article_body_ja(aid, progress.text)
        repo.clear_body_ja_chunks(aid)
        _clear_attempts(repo, aid)
        translated += 1

    stats = {
        "picked": len(article_ids),
        "translated": translated,
        "failed": failed,
        "partial": partial,
        "skipped_japanese": skipped_japanese,
        "skipped_garbled": skipped_garbled,
        "skipped_budget": skipped_budget,
        "echo_failed": echo_failed,
        "attempt_record_errors": record_errors,
        "elapsed_seconds": int(time.monotonic() - started),
    }
    _log.info("body_backlog_done", **stats)
    return stats
