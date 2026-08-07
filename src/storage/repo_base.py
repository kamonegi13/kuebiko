"""RunHistoryRepository の共有基底 (run_history 分割の一部)。

``__init__`` / ``_connect`` / ``db_path`` / ``_apply_migrations`` を保持し、
各 mixin が ``self._connect`` などを共有する土台。mixin はすべてこの基底を
継承するため、ダイヤモンド MRO でも属性解決は一意 (共通基底)。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from src.storage.records import DEFAULT_DB_PATH
from src.storage.schema_sql import _SCHEMA_SQL


class RunHistoryRepositoryBase:
    """SQLite ベースの run / article / live_log 履歴リポジトリ。"""

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH) -> None:
        from src.storage.db_backend import ensure_pg_schema, is_pg_enabled

        self._db_path = Path(db_path)
        if is_pg_enabled():
            # Phase Y-3: PostgreSQL モード。schema 初期化は pg_schema 経由。
            # _apply_migrations は PG 側 schema で完結しているため skip。
            ensure_pg_schema()
            return
        # SQLite legacy path (default)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA_SQL)
            self._apply_migrations(conn)
            conn.commit()

    @property
    def db_path(self) -> Path:
        """SQLite fallback の DB path (PG 運用では connect が DATABASE_URL を優先)。"""
        return self._db_path

    @contextmanager
    def _connect(self) -> Iterator[Any]:
        # Phase Y-3: DATABASE_URL set なら PG 接続、 未設定なら SQLite
        from src.storage.db_backend import connect as backend_connect
        from src.storage.db_backend import is_pg_enabled

        if is_pg_enabled():
            conn = backend_connect()
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()
            return

        # SQLite legacy path
        conn = sqlite3.connect(
            self._db_path,
            isolation_level=None,  # autocommit
            timeout=10.0,
        )
        conn.row_factory = sqlite3.Row
        # WAL モードで読み取りと書き込みを並行可能に
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()

    def _apply_migrations(self, conn: sqlite3.Connection) -> None:
        """既存 DB に新規列を追加する idempotent なマイグレーション。"""
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
        if "log_line_count" not in existing:
            conn.execute(
                "ALTER TABLE runs ADD COLUMN log_line_count INTEGER NOT NULL DEFAULT 0",
            )
        if "log_truncated" not in existing:
            conn.execute(
                "ALTER TABLE runs ADD COLUMN log_truncated INTEGER NOT NULL DEFAULT 0",
            )
        # Phase 5L-4: articles に dedup_key カラム追加 (idempotent)
        # 古い DB は ALTER TABLE で追加、新しい DB は _SCHEMA_SQL の CREATE TABLE で
        # 既に存在する。INDEX 作成は両ケース共通 (IF NOT EXISTS)。
        existing_articles = {row["name"] for row in conn.execute("PRAGMA table_info(articles)")}
        if "dedup_key" not in existing_articles:
            conn.execute("ALTER TABLE articles ADD COLUMN dedup_key TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_dedup_key ON articles(dedup_key)")
        # Phase 5T-K: Discord post 識別子カラム追加 (idempotent)
        if "discord_message_id" not in existing_articles:
            conn.execute("ALTER TABLE articles ADD COLUMN discord_message_id TEXT")
        if "discord_channel_id" not in existing_articles:
            conn.execute("ALTER TABLE articles ADD COLUMN discord_channel_id TEXT")
        # Phase 5T-P: summary カラム追加 (idempotent、digest 用)
        if "summary" not in existing_articles:
            conn.execute("ALTER TABLE articles ADD COLUMN summary TEXT")
        # Phase Diamond L2: 記事本文 (trafilatura) を永続化。entity 再抽出基盤。
        if "body" not in existing_articles:
            conn.execute("ALTER TABLE articles ADD COLUMN body TEXT")
        if "body_fetched_at" not in existing_articles:
            conn.execute("ALTER TABLE articles ADD COLUMN body_fetched_at TEXT")
        # 本文完全性 (2026-07-27, docs/body_extraction_and_entity_integrity_redesign.md):
        # body の由来 (full_extract / playwright_extract / prefetch / scraper / grok /
        # feed_summary / none) と全文取得失敗理由を永続化。無音 fallback (全文失敗→feed 抜粋)
        # を監査可能にし、切り株の再取得キュー (body_source='feed_summary') の対象抽出に使う。
        # 不変条件: body が非 NULL なら body_source も非 NULL (書込 seam=update_article_body)。
        _body_source_added = "body_source" not in existing_articles
        if _body_source_added:
            conn.execute("ALTER TABLE articles ADD COLUMN body_source TEXT")
        if "extraction_failure_reason" not in existing_articles:
            conn.execute("ALTER TABLE articles ADD COLUMN extraction_failure_reason TEXT")
        # 記事タイプ (breaking/advisory/recap/tutorial/research/press/opinion)。統合判断分類器
        # (judgment_classifier) が分類済みだが未永続だった (2026-07-27 露出)。記事詳細で表示。
        if "article_type" not in existing_articles:
            conn.execute("ALTER TABLE articles ADD COLUMN article_type TEXT")
        # body_source 状態機械化 (2026-07-29, docs/body_source_state_machine_design.md B1):
        # 再取得試行回数。pending_refetch が N 回失敗したら blocked へ昇格させる恒久失敗判定に使う
        # (reason 一致では救えない WAF/timeout 系を試行回数で終端化)。B1 は列追加のみ。
        if "refetch_attempts" not in existing_articles:
            conn.execute(
                "ALTER TABLE articles ADD COLUMN refetch_attempts INTEGER NOT NULL DEFAULT 0"
            )
        if _body_source_added:
            # 既存行の遡及分類 (heuristic): WordPress boilerplate/切詰マーカーor極短=feed_summary、
            # それ以外の本文有 = full_extract 推定。完全ではないが監査の出発点 (§2.2)。
            conn.execute(
                "UPDATE articles SET body_source="
                "  CASE WHEN body IS NULL OR body='' THEN 'none'"
                "       WHEN body LIKE '%appeared first on%' OR body LIKE '%[…]%'"
                "         OR length(body) < 800 THEN 'feed_summary'"
                "       ELSE 'full_extract' END"
                " WHERE body_source IS NULL"
            )
        # Source Identity Decoupling: 安定 source キー feed_url (= ManagedSource.url) を永続化。
        # 表示名 feed_title を改名キーから外し、統計を feed_url で結合するため。
        if "feed_url" not in existing_articles:
            conn.execute("ALTER TABLE articles ADD COLUMN feed_url TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_feed_url ON articles(feed_url)")
        # Phase 5P: 観測性カラム追加 (idempotent)。
        if "triage_error_count" not in existing:
            conn.execute(
                "ALTER TABLE runs ADD COLUMN triage_error_count INTEGER NOT NULL DEFAULT 0",
            )
        if "partial_fetch" not in existing:
            conn.execute(
                "ALTER TABLE runs ADD COLUMN partial_fetch INTEGER NOT NULL DEFAULT 0",
            )
        if "partial_fetch_count" not in existing:
            conn.execute(
                "ALTER TABLE runs ADD COLUMN partial_fetch_count INTEGER NOT NULL DEFAULT 0",
            )

        # Phase H: PMESII-PT 8 軸 + Diamond victim の column 追加 (idempotent)
        pmesii_columns = [
            "pmesii_p",
            "pmesii_m",
            "pmesii_e",
            "pmesii_s",
            "pmesii_i_infra",
            "pmesii_i_cyber",
            "pmesii_p_env",
            "pmesii_t",
        ]
        for col in pmesii_columns:
            if col not in existing_articles:
                conn.execute(
                    f"ALTER TABLE articles ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0",
                )
            # partial index で集計を高速化 (各軸ごと、=1 のみ index 対象)
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_articles_{col} "
                f"ON articles(created_at) WHERE {col}=1",
            )

        # Phase H: Diamond victim (sector/country) の column 追加
        for col in (
            "victim_sector_canonical",
            "victim_sector_raw",
            "victim_country_iso",
            "victim_country_raw",
        ):
            if col not in existing_articles:
                conn.execute(f"ALTER TABLE articles ADD COLUMN {col} TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_articles_victim_sector "
            "ON articles(victim_sector_canonical, created_at) "
            "WHERE victim_sector_canonical IS NOT NULL",
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_articles_victim_country "
            "ON articles(victim_country_iso, created_at) "
            "WHERE victim_country_iso IS NOT NULL",
        )

        # Phase B-R5b 観察: LLM が判定した editorial_stance を永続化。
        # values: factual_report / analytical / opinion / propaganda / unknown
        # UI 観察ページ (/intel-graph/editorial-quality) で集計、誤分類フラグ機能で
        # prompt 改善ループに使う。
        if "editorial_stance" not in existing_articles:
            conn.execute("ALTER TABLE articles ADD COLUMN editorial_stance TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_articles_editorial_stance "
            "ON articles(editorial_stance, created_at) "
            "WHERE editorial_stance IS NOT NULL",
        )

        # 記事の公開時刻 (RSS pubDate)。created_at(取得/処理時刻) と別保持し UI 表示に使う。
        # 既存行は NULL (公開時刻は再取得不能) → 表示側で created_at にフォールバック。
        if "published_at" not in existing_articles:
            conn.execute("ALTER TABLE articles ADD COLUMN published_at TEXT")

        # 時間軸レイヤ b/c (2026-06-27): 事象発生日 / 基準 / 侵害開始日 (SQLite fallback)。
        for col in ("event_date", "event_date_basis", "compromise_date"):
            if col not in existing_articles:
                conn.execute(f"ALTER TABLE articles ADD COLUMN {col} TEXT")

        # Phase Diamond-Axes: Diamond Model の 2 meta-feature 軸を articles 列に追加。
        # socio_political_intent = Adversary⇄Victim の意図 (closed enum)、
        # technical_axis_summary = Capability⇄Infrastructure の技術的結線 (narrative)。
        for col in (
            "socio_political_intent",
            "socio_political_rationale",
            "technical_axis_summary",
            # intent の LLM 自己評価確度 (high/medium/low)。低確度 intent を集計から
            # 除外できるようにする (grounded 再設計 follow-up の永続化)。
            "intent_confidence",
            # P4: 対処 (CoA) の 1 文 (本文明示のみ)
            "remediation",
        ):
            if col not in existing_articles:
                conn.execute(f"ALTER TABLE articles ADD COLUMN {col} TEXT")
        # intent 集計 (将来予測の actor×intent クロス集計) を高速化する partial index。
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_articles_socio_political_intent "
            "ON articles(socio_political_intent, created_at) "
            "WHERE socio_political_intent IS NOT NULL",
        )

        # flow Phase 3: 投稿先決定の監査情報 (記事単位の「なぜこのチャンネルか」)。
        # 集計用途は無く article_id (PK) で引くだけなので index は付けない。
        for col in ("routing_rule_id", "routing_reason"):
            if col not in existing_articles:
                conn.execute(f"ALTER TABLE articles ADD COLUMN {col} TEXT")

        # 主題アクター層 (2026-07-17、docs/subject_actor_attribution_design.md):
        # 言及と分離した「記事の主語」。PIR evaluator の主題ゲートが読む。
        # NULL = 未評価 (legacy 照合)。行 SELECT でのみ読むため index 不要。
        for col in (
            "subject_actor_ids",
            "subject_actor_source",
            "subject_actor_confidence",
        ):
            if col not in existing_articles:
                conn.execute(f"ALTER TABLE articles ADD COLUMN {col} TEXT")

        # アクター辞書 D1 (2026-07-26): 主題判定 LLM 層の生入力 (summarizer 出力) の永続化。
        # 判定の再導出・新アクター承認時の全期間遡及帰属・層の fill-rate 監査に使う。
        # 行 SELECT + LIKE 走査 (再帰属時のみ) のため index 不要。
        for col in ("llm_primary_actor_raw", "llm_primary_confidence"):
            if col not in existing_articles:
                conn.execute(f"ALTER TABLE articles ADD COLUMN {col} TEXT")

        # 記事本文のオンデマンド日本語訳キャッシュ (2026-07-25 記事詳細 UI)。
        # body と同時に 90 日 retention で NULL 化 (purge_article_bodies_older_than)。
        if "body_ja" not in existing_articles:
            conn.execute("ALTER TABLE articles ADD COLUMN body_ja TEXT")

        # 被害国スコープ (監査 2026-08-01 ⑥): ISO2 に解決できない "global"/"EU"/複数国の
        # 受け皿 ("global"|"regional"|"multi")。victim_country_iso は単一国の意味を保つ。
        if "victim_country_scope" not in existing_articles:
            conn.execute("ALTER TABLE articles ADD COLUMN victim_country_scope TEXT")

        # ランサム識別フラグ (category と直交する横断属性)。ransomware.live 由来 / 攻撃者が
        # ransom_group のニュースで true。地図の「ランサム/その他」分割フィルタに使う。
        # category を侵食しない (breach/malware のまま is_ransomware=true で無損失)。
        if "is_ransomware" not in existing_articles:
            conn.execute(
                "ALTER TABLE articles ADD COLUMN is_ransomware INTEGER NOT NULL DEFAULT 0",
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_articles_is_ransomware "
            "ON articles(created_at) WHERE is_ransomware=1",
        )

        # Phase B-R5b 観察: analyst が editorial_stance 誤分類をフラグするための table。
        # 別 table にすることで articles テーブルの schema をクリーンに保つ。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS editorial_stance_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id TEXT NOT NULL,
                original_stance TEXT,
                corrected_stance TEXT NOT NULL,
                reviewer TEXT,
                comment TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(article_id)
            )
            """
        )

        # Phase 2 K6: synthesis / spotlight の Discord 配信済時刻 (再配信 dedup)。
        # NULL=未配信。UPSERT (再生成) では touch しないため、一度配信したら以後
        # 同 period は再投稿されない (brief / watch の spam 防止)。
        existing_syn = {row["name"] for row in conn.execute("PRAGMA table_info(status_synthesis)")}
        if "posted_at" not in existing_syn:
            conn.execute("ALTER TABLE status_synthesis ADD COLUMN posted_at TEXT")
        if "tradecraft" not in existing_syn:  # S2: analytic tradecraft
            conn.execute("ALTER TABLE status_synthesis ADD COLUMN tradecraft TEXT")
        existing_spot = {row["name"] for row in conn.execute("PRAGMA table_info(pir_spotlight)")}
        if "posted_at" not in existing_spot:
            conn.execute("ALTER TABLE pir_spotlight ADD COLUMN posted_at TEXT")

        # 日次ブリーフ構造化 payload (2026-07-12): Web はテキストでなく構造から描画する。
        existing_brief = {row["name"] for row in conn.execute("PRAGMA table_info(daily_briefs)")}
        if "payload" not in existing_brief:
            conn.execute("ALTER TABLE daily_briefs ADD COLUMN payload TEXT")

        # 常設情報要求 standing situations (段A, 2026-07-13): event/standing の種別。
        existing_sit = {row["name"] for row in conn.execute("PRAGMA table_info(situations)")}
        if "kind" not in existing_sit:
            conn.execute("ALTER TABLE situations ADD COLUMN kind TEXT NOT NULL DEFAULT 'event'")

        # 証拠台帳の状態分離 (2026-07-16): 割当 (観測) と ACH 評価 (判断) を別状態にする。
        # read_at=接地 prompt 供給の最終時刻 / assessed_at=ACH 引用の最終時刻 (NULL=未)。
        existing_ev = {row["name"] for row in conn.execute("PRAGMA table_info(situation_evidence)")}
        if "read_at" not in existing_ev:
            conn.execute("ALTER TABLE situation_evidence ADD COLUMN read_at TEXT")
        if "assessed_at" not in existing_ev:
            conn.execute("ALTER TABLE situation_evidence ADD COLUMN assessed_at TEXT")
        # 旧 rich 行 (excerpt あり = ACH 引用済) を評価済みに刻む (冪等: 新規行は
        # record_assessment が常に assessed_at を書くため再 match しない)。
        conn.execute(
            "UPDATE situation_evidence SET assessed_at = added_at, read_at = added_at"
            " WHERE assessed_at IS NULL AND excerpt <> ''"
        )

        # LLM 消費台帳の一本化 (2026-07-26): claudecode も llm_usage に記録するため
        # cache 読取トークンと $ 換算コストを追加 (他 provider は 0)。
        existing_usage = {row["name"] for row in conn.execute("PRAGMA table_info(llm_usage)")}
        if "cache_read_tokens" not in existing_usage:
            conn.execute(
                "ALTER TABLE llm_usage ADD COLUMN cache_read_tokens INTEGER NOT NULL DEFAULT 0",
            )
        if "cost_usd" not in existing_usage:
            conn.execute("ALTER TABLE llm_usage ADD COLUMN cost_usd REAL NOT NULL DEFAULT 0")
