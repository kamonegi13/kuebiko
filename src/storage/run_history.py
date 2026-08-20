"""SQLite/PostgreSQL ベースの実行履歴・記事履歴・ライブログ永続化 (Phase 1.5)。

**このモジュールは公開 seam (facade) である。** 実装は 800 行上限のため以下に分割済:

- ``records.py``      — Pydantic レコードモデル + 定数 (DEFAULT_DB_PATH 等)
- ``schema_sql.py``   — ``_SCHEMA_SQL`` (idempotent スキーマ SQL)
- ``row_mappers.py``  — 行 → レコード変換ヘルパ (``_row_to_*`` / ``_to_iso`` 等)
- ``repo_base.py``    — ``RunHistoryRepositoryBase`` (__init__ / _connect / _apply_migrations)
- ``repo_runs.py``    — runs / live_log / ops_notify_log
- ``repo_articles.py``— articles / article_entities / editorial_stance
- ``repo_dedup.py``   — dedup_seen_urls / article_embeddings / CVE dedup / brief cap
- ``repo_synthesis.py``— status synthesis / spotlight / forecast / article_notes
- ``repo_knowledge.py``— taxonomy / MITRE / actor provisional / F1 / maintenance / aggregations
- ``repo_ops_notices.py``— ops_notices (ops 通知の永続化、2026-08-21)

``RunHistoryRepository`` は上記 mixin を合成した具象クラス。全 mixin は共通基底
``RunHistoryRepositoryBase`` を継承するため、ダイヤモンド MRO でも ``self._connect``
等の属性解決は一意。従来 ``src.storage.run_history`` から import できた全シンボル
(レコード / 定数 / ヘルパ / ``_SCHEMA_SQL``) はここで re-export され import 互換を保つ。

設計原則:
- スキーマは migration なしで idempotent (CREATE TABLE IF NOT EXISTS)
- 既存 DB への列追加は ``_apply_migrations`` で都度判定
- 接続は使い回さず、各メソッドで ``with self._connect()`` を開閉
- 書き込みは WAL モードで読み取りをブロックしない
"""

from __future__ import annotations

from src.storage.records import (
    DEFAULT_DB_PATH,
    DEFAULT_LOG_RETENTION_DAYS,
    LOG_TRUNCATE_SUFFIX,
    MAX_LOG_LINE_BYTES,
    MAX_LOG_LINES_PER_RUN,
    ActorUpdateProposalRecord,
    ArticleNoteRecord,
    ArticleRecord,
    ArticleStatus,
    F1SelectionRecord,
    LogLine,
    LogStream,
    RunRecord,
    RunStatus,
    SourceFetchHealth,
    StatusSynthesisRecord,
    TaxonomyProposalRecord,
)
from src.storage.repo_actor_profile import ActorProfileMixin
from src.storage.repo_articles import ArticlesMixin
from src.storage.repo_base import RunHistoryRepositoryBase
from src.storage.repo_dedup import DedupMixin
from src.storage.repo_knowledge import KnowledgeMixin
from src.storage.repo_llm_usage import LlmUsageMixin
from src.storage.repo_ops_notices import OpsNoticesMixin
from src.storage.repo_pir import PirJudgmentsMixin
from src.storage.repo_runs import RunsMixin
from src.storage.repo_synthesis import SynthesisMixin
from src.storage.repo_translation import TranslationChunksMixin
from src.storage.row_mappers import (
    _from_iso,
    _normalize_jsonb,
    _parse_brief_sources,
    _row_to_actor_proposal,
    _row_to_article,
    _row_to_article_note,
    _row_to_daily_brief,
    _row_to_forecast_indicator,
    _row_to_log,
    _row_to_run,
    _row_to_spotlight,
    _row_to_synthesis,
    _row_to_taxonomy_proposal,
    _to_iso,
    _truncate_line,
)
from src.storage.schema_sql import _SCHEMA_SQL


class RunHistoryRepository(
    RunsMixin,
    ArticlesMixin,
    DedupMixin,
    SynthesisMixin,
    KnowledgeMixin,
    PirJudgmentsMixin,
    LlmUsageMixin,
    ActorProfileMixin,
    TranslationChunksMixin,
    OpsNoticesMixin,
    RunHistoryRepositoryBase,
):
    """SQLite/PostgreSQL ベースの run / article / live_log 履歴リポジトリ (mixin 合成)。"""


__all__ = [
    "DEFAULT_DB_PATH",
    "DEFAULT_LOG_RETENTION_DAYS",
    "LOG_TRUNCATE_SUFFIX",
    "MAX_LOG_LINE_BYTES",
    "MAX_LOG_LINES_PER_RUN",
    "ActorUpdateProposalRecord",
    "ArticleNoteRecord",
    "ArticleRecord",
    "ArticleStatus",
    "F1SelectionRecord",
    "LogLine",
    "LogStream",
    "RunHistoryRepository",
    "RunHistoryRepositoryBase",
    "RunRecord",
    "RunStatus",
    "SourceFetchHealth",
    "StatusSynthesisRecord",
    "TaxonomyProposalRecord",
    "_SCHEMA_SQL",
    "_from_iso",
    "_normalize_jsonb",
    "_parse_brief_sources",
    "_row_to_actor_proposal",
    "_row_to_article",
    "_row_to_article_note",
    "_row_to_daily_brief",
    "_row_to_forecast_indicator",
    "_row_to_log",
    "_row_to_run",
    "_row_to_spotlight",
    "_row_to_synthesis",
    "_row_to_taxonomy_proposal",
    "_to_iso",
    "_truncate_line",
]
