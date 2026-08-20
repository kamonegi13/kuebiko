"""PostgreSQL schema definition (Phase Y-2)。

SQLite ``_SCHEMA_SQL`` を PG dialect に翻訳。

主な翻訳ルール:
- ``INTEGER PRIMARY KEY AUTOINCREMENT`` → ``BIGSERIAL PRIMARY KEY``
- ``REAL`` → ``DOUBLE PRECISION``
- ``BLOB`` → ``BYTEA``
- ``TEXT`` はそのまま
- 日時は ``TIMESTAMPTZ`` に翻訳 (SQLite は TEXT に ISO-8601 を入れていた)
- ``CREATE INDEX IF NOT EXISTS`` は PG でも互換
"""

from __future__ import annotations

PG_SCHEMA_SQL = """
-- ===== runs (パイプライン実行履歴) =====
CREATE TABLE IF NOT EXISTS runs (
    id              BIGSERIAL PRIMARY KEY,
    started_at      TIMESTAMPTZ NOT NULL,
    finished_at     TIMESTAMPTZ,
    pipeline        TEXT        NOT NULL,
    -- BOOL 系は SQLite との互換性のため SMALLINT (0/1) で統一
    dry_run         SMALLINT    NOT NULL DEFAULT 0,
    triggered_by    TEXT        NOT NULL DEFAULT 'scheduler',
    status          TEXT        NOT NULL DEFAULT 'running',
    total_fetched   INTEGER     NOT NULL DEFAULT 0,
    summarized      INTEGER     NOT NULL DEFAULT 0,
    posted          INTEGER     NOT NULL DEFAULT 0,
    marked_read     INTEGER     NOT NULL DEFAULT 0,
    error_count     INTEGER     NOT NULL DEFAULT 0,
    note            TEXT,
    log_line_count  INTEGER     NOT NULL DEFAULT 0,
    log_truncated   SMALLINT    NOT NULL DEFAULT 0,
    triage_error_count INTEGER  NOT NULL DEFAULT 0,
    partial_fetch   SMALLINT    NOT NULL DEFAULT 0,
    partial_fetch_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_runs_started_at ON runs(started_at);

-- ===== articles (記事処理結果) =====
CREATE TABLE IF NOT EXISTS articles (
    id               BIGSERIAL PRIMARY KEY,
    run_id           BIGINT      NOT NULL,
    article_id       TEXT        NOT NULL,
    title            TEXT        NOT NULL,
    url              TEXT        NOT NULL,
    feed_title       TEXT,
    -- 安定 source キー (= ManagedSource.url)。表示名 feed_title と結合キーを分離するため永続化。
    feed_url         TEXT,
    importance       TEXT,
    category         TEXT,
    status           TEXT        NOT NULL,
    failure_reason   TEXT,
    posted_channel   TEXT,
    duration_seconds DOUBLE PRECISION,
    dedup_key        TEXT,
    discord_message_id TEXT,
    discord_channel_id TEXT,
    summary          TEXT,
    body             TEXT,
    body_fetched_at  TIMESTAMPTZ,
    victim_sector_canonical TEXT,
    victim_sector_raw TEXT,
    victim_country_iso TEXT,
    victim_country_raw TEXT,
    -- Phase Diamond-Axes: Diamond Model meta-feature 軸
    -- (socio_political = Adversary⇄Victim 意図 enum, technical = Capability⇄Infra narrative)
    socio_political_intent    TEXT,
    socio_political_rationale TEXT,
    technical_axis_summary    TEXT,
    -- Phase H: PMESII-PT 8 軸 (1=該当)
    pmesii_p         SMALLINT NOT NULL DEFAULT 0,
    pmesii_m         SMALLINT NOT NULL DEFAULT 0,
    pmesii_e         SMALLINT NOT NULL DEFAULT 0,
    pmesii_s         SMALLINT NOT NULL DEFAULT 0,
    pmesii_i_infra   SMALLINT NOT NULL DEFAULT 0,
    pmesii_i_cyber   SMALLINT NOT NULL DEFAULT 0,
    pmesii_p_env     SMALLINT NOT NULL DEFAULT 0,
    pmesii_t         SMALLINT NOT NULL DEFAULT 0,
    -- Phase B-R5b: LLM 判定 editorial_stance (factual_report/analytical/opinion/propaganda/unknown)
    editorial_stance TEXT,
    -- 記事の公開時刻 (RSS pubDate)。created_at(取得/処理時刻) と区別して表示に使う。
    published_at     TIMESTAMPTZ,
    -- 時間軸レイヤ b/c (2026-06-27): 事象の「実発生(または検知/公表)日」を published_at
    -- (報道時刻)・created_at(取得時刻) と分離。明示時のみ、推測しない (YYYY-MM-DD)。
    -- event_date_basis = occurred|detected|disclosed|reported|relative (date が何を指すか=正直さ)。
    -- compromise_date = 初期侵害開始日 (明示時のみ)。dwell = event_date - compromise_date。
    event_date       TEXT,
    event_date_basis TEXT,
    compromise_date  TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_articles_pmesii_p ON articles(created_at) WHERE pmesii_p=1;
CREATE INDEX IF NOT EXISTS idx_articles_pmesii_m ON articles(created_at) WHERE pmesii_m=1;
CREATE INDEX IF NOT EXISTS idx_articles_pmesii_e ON articles(created_at) WHERE pmesii_e=1;
CREATE INDEX IF NOT EXISTS idx_articles_pmesii_s ON articles(created_at) WHERE pmesii_s=1;
CREATE INDEX IF NOT EXISTS idx_articles_pmesii_i_infra
    ON articles(created_at) WHERE pmesii_i_infra=1;
CREATE INDEX IF NOT EXISTS idx_articles_pmesii_i_cyber
    ON articles(created_at) WHERE pmesii_i_cyber=1;
CREATE INDEX IF NOT EXISTS idx_articles_pmesii_p_env ON articles(created_at) WHERE pmesii_p_env=1;
CREATE INDEX IF NOT EXISTS idx_articles_pmesii_t ON articles(created_at) WHERE pmesii_t=1;
CREATE INDEX IF NOT EXISTS idx_articles_victim_sector
    ON articles(victim_sector_canonical, created_at)
    WHERE victim_sector_canonical IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_articles_victim_country
    ON articles(victim_country_iso, created_at)
    WHERE victim_country_iso IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_articles_editorial_stance
    ON articles(editorial_stance, created_at)
    WHERE editorial_stance IS NOT NULL;
-- 注: idx_articles_socio_political_intent は新規列のため、既存テーブルでは
-- 末尾の ALTER TABLE ADD COLUMN で列が追加された **後** に作る必要がある。
-- ここ (CREATE TABLE 直後) で作ると既存 DB で列未追加のまま参照して失敗するため、
-- 末尾の migration セクションで CREATE する (Phase Diamond-Axes)。
CREATE INDEX IF NOT EXISTS idx_articles_run_id      ON articles(run_id);
CREATE INDEX IF NOT EXISTS idx_articles_importance  ON articles(importance);
CREATE INDEX IF NOT EXISTS idx_articles_status      ON articles(status);
CREATE INDEX IF NOT EXISTS idx_articles_created_at  ON articles(created_at);
CREATE INDEX IF NOT EXISTS idx_articles_dedup_key   ON articles(dedup_key)
    WHERE dedup_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_articles_url         ON articles(url);

-- ===== run_logs (ライブログ永続化) =====
-- FK 無し: subprocess の同時 INSERT INTO run_logs と UPDATE runs.log_line_count
-- の deadlock を構造的に排除する (Phase Y-4 で実発生、 DEFERRABLE でも解決せず)。
-- orphan row は cleanup job で別途対応。
CREATE TABLE IF NOT EXISTS run_logs (
    run_id   BIGINT  NOT NULL,
    seq      INTEGER NOT NULL,
    ts       TIMESTAMPTZ NOT NULL,
    stream   TEXT    NOT NULL DEFAULT 'stdout',
    line     TEXT    NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_run_logs_ts ON run_logs(ts);

-- ===== article_entities (Diamond Model entities) =====
CREATE TABLE IF NOT EXISTS article_entities (
    id          BIGSERIAL PRIMARY KEY,
    article_id  TEXT        NOT NULL,
    entity_type TEXT        NOT NULL,
    value       TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(article_id, entity_type, value)
);
CREATE INDEX IF NOT EXISTS idx_article_entities_article_id ON article_entities(article_id);
CREATE INDEX IF NOT EXISTS idx_article_entities_type_value ON article_entities(entity_type, value);
CREATE INDEX IF NOT EXISTS idx_article_entities_created_at ON article_entities(created_at);
-- 監査 backlog 2026-07-05: LOWER(value) 照合クエリ用の式索引 (SQLite _SCHEMA と対)
CREATE INDEX IF NOT EXISTS idx_article_entities_type_value_lower
    ON article_entities(entity_type, LOWER(value));

-- ===== dedup_seen_urls (URL 重複排除) =====
CREATE TABLE IF NOT EXISTS dedup_seen_urls (
    url_hash    TEXT        NOT NULL PRIMARY KEY,
    url         TEXT        NOT NULL,
    article_id  TEXT,
    title       TEXT,
    first_seen  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    seen_count  INTEGER     NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_dedup_first_seen ON dedup_seen_urls(first_seen);

-- ===== article_embeddings (意味的重複排除) =====
CREATE TABLE IF NOT EXISTS article_embeddings (
    url_hash   TEXT        NOT NULL PRIMARY KEY,
    url        TEXT        NOT NULL,
    title      TEXT,
    model      TEXT        NOT NULL,
    dim        INTEGER     NOT NULL,
    vector     BYTEA       NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (url_hash) REFERENCES dedup_seen_urls(url_hash) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_article_embeddings_created ON article_embeddings(created_at);
CREATE INDEX IF NOT EXISTS idx_article_embeddings_model   ON article_embeddings(model);

-- ===== ops_notify_log (rate limit 用) =====
CREATE TABLE IF NOT EXISTS ops_notify_log (
    pipeline_name TEXT        NOT NULL PRIMARY KEY,
    last_sent_at  TIMESTAMPTZ NOT NULL,
    last_status   TEXT        NOT NULL
);

-- ===== f1_selections (F1 deep dive 履歴) =====
CREATE TABLE IF NOT EXISTS f1_selections (
    id              BIGSERIAL PRIMARY KEY,
    run_id          BIGINT      NOT NULL,
    article_id      TEXT        NOT NULL,
    dedup_key       TEXT,
    composite_score DOUBLE PRECISION NOT NULL,
    pir             DOUBLE PRECISION NOT NULL,
    roi             DOUBLE PRECISION NOT NULL,
    timeliness      DOUBLE PRECISION NOT NULL,
    novelty         DOUBLE PRECISION NOT NULL,
    selected_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_f1_selections_selected_at ON f1_selections(selected_at);
CREATE INDEX IF NOT EXISTS idx_f1_selections_dedup_key   ON f1_selections(dedup_key);

-- ===== weekly_recaps (段5: F1 recap 本文の永続化 → Retrospect 連携) =====
-- 旧来 recap 本文は Discord のみで蒸発し f1_selections は id+score のみ → 過去 recap を
-- 後から読めなかった。本文を保存し Retrospect の週 window で参照する。
CREATE TABLE IF NOT EXISTS weekly_recaps (
    id              BIGSERIAL PRIMARY KEY,
    run_id          BIGINT,
    period_label    TEXT        NOT NULL,
    recap_text      TEXT        NOT NULL,
    candidate_count INTEGER     NOT NULL DEFAULT 0,
    generated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_weekly_recaps_generated_at ON weekly_recaps(generated_at);

-- ===== daily_briefs (W1 通知再設計: 日次ブリーフ本文の永続化 → Web pull 閲覧) =====
-- 旧来ブリーフ (朝刊/夕刊) 本文は Discord のみで蒸発していた。Web「日次ブリーフ」ビューで
-- 通読できるよう本文 (synthesis + PIR focus) を保存する。sources は JSON 文字列 (TEXT、
-- 中身を SQL で問い合わせないため weekly_recaps 同様 dialect 非依存に保つ)。
CREATE TABLE IF NOT EXISTS daily_briefs (
    id              BIGSERIAL PRIMARY KEY,
    run_id          BIGINT,
    slot            TEXT        NOT NULL,
    period_label    TEXT        NOT NULL,
    title           TEXT        NOT NULL,
    bluf            TEXT        NOT NULL DEFAULT '',
    summary         TEXT        NOT NULL,
    section_count   INTEGER     NOT NULL DEFAULT 0,
    sources         TEXT        NOT NULL DEFAULT '[]',
    payload         TEXT,
    generated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_daily_briefs_generated_at ON daily_briefs(generated_at);

-- ===== status_synthesis (PMESII-PT 状況総括) =====
CREATE TABLE IF NOT EXISTS status_synthesis (
    id              BIGSERIAL PRIMARY KEY,
    period_type     TEXT        NOT NULL,
    period_start    TIMESTAMPTZ NOT NULL,
    period_end      TIMESTAMPTZ NOT NULL,
    headline        TEXT        NOT NULL,
    weight_section  TEXT        NOT NULL,
    chain_section   TEXT        NOT NULL,
    cog_section     TEXT        NOT NULL,
    spillover_section TEXT      NOT NULL,
    pir_section     TEXT        NOT NULL,
    axes_evidence   JSONB       NOT NULL,
    tradecraft      JSONB,
    article_count   INTEGER     NOT NULL,
    llm_model       TEXT,
    generated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(period_type, period_start)
);
CREATE INDEX IF NOT EXISTS idx_synthesis_period
    ON status_synthesis(period_type, period_start DESC);

-- ===== editorial_stance_reviews (Phase B-R5b analyst flag) =====
CREATE TABLE IF NOT EXISTS editorial_stance_reviews (
    id               BIGSERIAL PRIMARY KEY,
    article_id       TEXT        NOT NULL,
    original_stance  TEXT,
    corrected_stance TEXT        NOT NULL,
    reviewer         TEXT,
    comment          TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(article_id)
);

-- ===== taxonomy_review_proposals (Phase H taxonomy review) =====
CREATE TABLE IF NOT EXISTS taxonomy_review_proposals (
    id              BIGSERIAL PRIMARY KEY,
    run_id          BIGINT,
    proposal_type   TEXT NOT NULL,
    tier            TEXT NOT NULL,
    target_yaml     TEXT NOT NULL,
    target_canonical TEXT,
    proposed_change TEXT NOT NULL,
    rationale       TEXT NOT NULL,
    confidence      TEXT NOT NULL,
    evidence_count  INTEGER DEFAULT 0,
    evidence_ids    TEXT,
    status          TEXT DEFAULT 'pending',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decided_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_taxonomy_review_status ON taxonomy_review_proposals(status);

-- ===== actor_update_proposals (Actors Stage 4: MITRE 同期レビュー提案) =====
CREATE TABLE IF NOT EXISTS actor_update_proposals (
    id              BIGSERIAL PRIMARY KEY,
    run_id          BIGINT,
    proposal_type   TEXT NOT NULL,
    mitre_group     TEXT NOT NULL,
    dedup_key       TEXT NOT NULL,
    actor_id        TEXT,
    payload         TEXT NOT NULL,
    rationale       TEXT NOT NULL,
    status          TEXT DEFAULT 'pending',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decided_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_actor_proposals_status ON actor_update_proposals(status);

-- ===== pir_spotlight (PIR Spotlight narrative 永続化) =====
CREATE TABLE IF NOT EXISTS pir_spotlight (
    id              BIGSERIAL PRIMARY KEY,
    pir_id          TEXT        NOT NULL,
    pir_title       TEXT        NOT NULL,
    period_type     TEXT        NOT NULL,
    period_start    TIMESTAMPTZ NOT NULL,
    period_end      TIMESTAMPTZ NOT NULL,
    headline        TEXT        NOT NULL,
    outlook         TEXT        NOT NULL,
    key_events      JSONB       NOT NULL,
    article_count   INTEGER     NOT NULL,
    llm_model       TEXT,
    generated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(pir_id, period_type, period_start)
);
CREATE INDEX IF NOT EXISTS idx_pir_spotlight_period
    ON pir_spotlight(pir_id, period_type, period_start DESC);

-- ===== 既存テーブルへの列追加 (idempotent migration) =====
-- CREATE TABLE IF NOT EXISTS は既存テーブルに列を足さないため、後から増えた列は
-- ADD COLUMN IF NOT EXISTS で明示的に追加する (PG 9.6+ 対応)。
ALTER TABLE articles ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ;
-- Phase Diamond-Axes: Diamond Model meta-feature 軸の列を既存 articles に追加
ALTER TABLE articles ADD COLUMN IF NOT EXISTS socio_political_intent TEXT;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS intent_confidence TEXT;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS remediation TEXT;
-- アナリスト所見 (2026-08-18): Discord のみで永続化されず後から追えなかったため列を持つ。
ALTER TABLE articles ADD COLUMN IF NOT EXISTS analyst_note TEXT;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS socio_political_rationale TEXT;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS technical_axis_summary TEXT;
-- intent 集計 (actor×intent クロス) 高速化 partial index。上の ADD COLUMN の後に作る。
CREATE INDEX IF NOT EXISTS idx_articles_socio_political_intent
    ON articles(socio_political_intent, created_at)
    WHERE socio_political_intent IS NOT NULL;
-- Source Identity Decoupling: 安定 source キー feed_url を既存 articles に追加。
-- 表示名 (feed_title) を改名キーから外し、統計を feed_url で結合するため。
-- index は ADD COLUMN の後に作る (memory pg_schema_index_ordering 厳守)。
ALTER TABLE articles ADD COLUMN IF NOT EXISTS feed_url TEXT;
CREATE INDEX IF NOT EXISTS idx_articles_feed_url
    ON articles(feed_url, created_at)
    WHERE feed_url IS NOT NULL;
-- Phase 2 K6: synthesis / spotlight の Discord 配信済時刻 (再配信 dedup)。
-- NULL=未配信。UPSERT (再生成) では touch しないので一度配信したら再投稿しない。
-- 自然キー (period_type, period_start) / (pir_id, period_type, period_start) は既存
-- UNIQUE 索引があり、posted_at 自体への index は不要。
ALTER TABLE status_synthesis ADD COLUMN IF NOT EXISTS posted_at TIMESTAMPTZ;
-- S2 (analytic tradecraft): 主見立て + 対立仮説 + 前提 + 覆る指標 (ICD 203)。
ALTER TABLE status_synthesis ADD COLUMN IF NOT EXISTS tradecraft JSONB;
ALTER TABLE pir_spotlight    ADD COLUMN IF NOT EXISTS posted_at TIMESTAMPTZ;
-- flow Phase 3: 投稿先決定の監査情報 (記事単位の「なぜこのチャンネルか」)。
-- article_id (PK) で引くだけで集計用途は無いため index は不要。
ALTER TABLE articles ADD COLUMN IF NOT EXISTS routing_rule_id TEXT;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS routing_reason TEXT;

-- ランサム識別フラグ (category と直交)。ransomware.live 由来 / 攻撃者が ransom_group で 1。
-- 地図「ランサム/その他」分割用。index は ADD COLUMN の後 (pg_schema_index_ordering 厳守)。
ALTER TABLE articles ADD COLUMN IF NOT EXISTS is_ransomware SMALLINT NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_articles_is_ransomware
    ON articles(created_at) WHERE is_ransomware=1;

-- 時間軸レイヤ b/c (2026-06-27): 事象発生日 / 基準 / 侵害開始日 (報道時刻と分離)。
-- index は ADD COLUMN の後 (pg_schema_index_ordering 厳守)。
ALTER TABLE articles ADD COLUMN IF NOT EXISTS event_date TEXT;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS event_date_basis TEXT;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS compromise_date TEXT;
CREATE INDEX IF NOT EXISTS idx_articles_event_date
    ON articles(event_date) WHERE event_date IS NOT NULL;

-- 主題アクター層 (2026-07-17、docs/subject_actor_attribution_design.md):
-- 言及 (article_entities actor) と分離した「記事の主語」。PIR evaluator の主題ゲート用。
-- NULL = 未評価 (legacy 照合に fallback)。行 SELECT でのみ読むため index 不要。
ALTER TABLE articles ADD COLUMN IF NOT EXISTS subject_actor_ids TEXT;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS subject_actor_source TEXT;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS subject_actor_confidence TEXT;

-- アクター辞書 D1 (2026-07-26): 主題判定 LLM 層の生入力 (summarizer の
-- routing_flags.primary_actor_id / confidence、辞書解決前)。判定の再導出・
-- 新アクター承認時の全期間遡及帰属・fill-rate 監査に使う。index 不要。
ALTER TABLE articles ADD COLUMN IF NOT EXISTS llm_primary_actor_raw TEXT;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS llm_primary_confidence TEXT;

-- 記事本文のオンデマンド日本語訳キャッシュ (2026-07-25 記事詳細 UI)。翻訳ボタン押下時に
-- LLM 全訳を保存し、以後は DB から返す (バッチ全訳はしない)。body と同時に 90 日
-- retention で NULL 化 (repo_dedup.purge_article_bodies_older_than)。
-- article_id (PK) 引きのみのため index 不要。
ALTER TABLE articles ADD COLUMN IF NOT EXISTS body_ja TEXT;

-- 本文完全性 (2026-07-27, docs/body_extraction_and_entity_integrity_redesign.md):
-- body_source = body の由来 (full_extract / playwright_extract / prefetch / scraper /
-- grok / feed_summary / none)、extraction_failure_reason = 全文取得失敗時の理由。
-- 全文失敗→feed 抜粋への無音 fallback を監査可能にし、切り株の再取得キュー対象を抽出する。
-- 不変条件: body 非 NULL ⇒ body_source 非 NULL (書込 seam=update_article_body)。
-- 行 SELECT + WHERE body_source= のみのため index 不要 (再取得ジョブは限定件数走査)。
ALTER TABLE articles ADD COLUMN IF NOT EXISTS body_source TEXT;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS extraction_failure_reason TEXT;
-- 記事タイプ (breaking/advisory/recap/…)。judgment_classifier が分類済だが未永続だった
-- (2026-07-27 露出)。記事詳細 UI で表示。行 SELECT のみ = index 不要。
ALTER TABLE articles ADD COLUMN IF NOT EXISTS article_type TEXT;
-- body_source 状態機械化 (2026-07-29, docs/body_source_state_machine_design.md B1):
-- 再取得試行回数。pending_refetch が N 回失敗で blocked 昇格させる恒久失敗判定に使う。
-- 行 SELECT/UPDATE のみ = index 不要 (index を張る場合はこの ADD COLUMN より後に CREATE)。
ALTER TABLE articles ADD COLUMN IF NOT EXISTS refetch_attempts INTEGER NOT NULL DEFAULT 0;
-- 既存行の遡及分類 (heuristic、body_source 未設定行のみ = 初回 migration 時)。
UPDATE articles SET body_source =
    CASE WHEN body IS NULL OR body='' THEN 'none'
         WHEN body LIKE '%appeared first on%' OR body LIKE '%[…]%'
           OR length(body) < 800 THEN 'feed_summary'
         ELSE 'full_extract' END
    WHERE body_source IS NULL;

-- ===== forecast_indicators (Phase 4 将来予測 FC2) =====
CREATE TABLE IF NOT EXISTS forecast_indicators (
    id              BIGSERIAL PRIMARY KEY,
    period_type     TEXT        NOT NULL,
    period_start    TIMESTAMPTZ NOT NULL,
    scope           TEXT        NOT NULL,
    target_value    TEXT        NOT NULL,
    direction       TEXT        NOT NULL,
    z_score         DOUBLE PRECISION NOT NULL DEFAULT 0,
    baseline_avg    DOUBLE PRECISION NOT NULL DEFAULT 0,
    latest_count    INTEGER     NOT NULL DEFAULT 0,
    rationale       TEXT        NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    verified_at     TIMESTAMPTZ,
    hit             SMALLINT,
    observed_count  INTEGER     NOT NULL DEFAULT 0,
    UNIQUE(period_type, period_start, scope, target_value)
);
CREATE INDEX IF NOT EXISTS idx_forecast_indicators_period
    ON forecast_indicators(period_type, period_start DESC);
CREATE INDEX IF NOT EXISTS idx_forecast_indicators_open
    ON forecast_indicators(period_type, verified_at);

-- ===== article_notes (Phase 5 学習・記憶: memo/bookmark/tag/judgment) =====
CREATE TABLE IF NOT EXISTS article_notes (
    article_id  TEXT        NOT NULL PRIMARY KEY,
    bookmarked  SMALLINT    NOT NULL DEFAULT 0,
    note        TEXT        NOT NULL DEFAULT '',
    tags        TEXT        NOT NULL DEFAULT '[]',
    judgment    TEXT        NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_article_notes_bookmarked
    ON article_notes(bookmarked, updated_at DESC);

-- ===== Situation Ledger (状況台帳、段A) =====
-- 設計: docs/synthesis_situation_ledger_design.md。時刻列は TEXT (ISO8601, UTC 固定) —
-- store は文字列比較/Python 側パースのみで SQL 日時関数を使わず、SQLite と挙動を揃える。
CREATE TABLE IF NOT EXISTS situations (
    situation_id     TEXT NOT NULL PRIMARY KEY,
    title            TEXT NOT NULL,
    domain           TEXT NOT NULL DEFAULT 'unclassified',
    status           TEXT NOT NULL DEFAULT 'active',
    anchors          TEXT NOT NULL DEFAULT '[]',
    pir_ids          TEXT NOT NULL DEFAULT '[]',
    opened_at        TEXT NOT NULL,
    last_evidence_at TEXT NOT NULL,
    closed_at        TEXT,
    kind             TEXT NOT NULL DEFAULT 'event'
);
CREATE INDEX IF NOT EXISTS idx_situations_status
    ON situations(status, last_evidence_at DESC);

CREATE TABLE IF NOT EXISTS situation_revisions (
    id                 BIGSERIAL PRIMARY KEY,
    situation_id       TEXT    NOT NULL,
    rev                INTEGER NOT NULL,
    run_id             TEXT    NOT NULL DEFAULT '',
    claim              TEXT    NOT NULL,
    claim_type         TEXT    NOT NULL DEFAULT 'ongoing_activity',
    leading_hypothesis TEXT    NOT NULL,
    confidence         TEXT    NOT NULL,
    confidence_basis   TEXT    NOT NULL DEFAULT '',
    hypotheses         TEXT    NOT NULL DEFAULT '[]',
    assumptions        TEXT    NOT NULL DEFAULT '[]',
    missing            TEXT    NOT NULL DEFAULT '[]',
    indicators         TEXT    NOT NULL DEFAULT '[]',
    implication        TEXT    NOT NULL DEFAULT '',
    delta_type         TEXT    NOT NULL,
    delta_note         TEXT    NOT NULL DEFAULT '',
    created_at         TEXT    NOT NULL,
    UNIQUE(situation_id, rev)
);
CREATE INDEX IF NOT EXISTS idx_situation_revisions_sid
    ON situation_revisions(situation_id, rev DESC);

CREATE TABLE IF NOT EXISTS situation_evidence (
    situation_id      TEXT NOT NULL,
    article_id        TEXT NOT NULL,
    polarity          TEXT NOT NULL DEFAULT 'neutral',
    attribution_basis TEXT NOT NULL DEFAULT 'unattributed',
    excerpt           TEXT NOT NULL DEFAULT '',
    source_tier       TEXT NOT NULL DEFAULT 'unknown',
    added_at          TEXT NOT NULL,
    assigned_by       TEXT NOT NULL DEFAULT 'anchor',
    PRIMARY KEY (situation_id, article_id)
);
CREATE INDEX IF NOT EXISTS idx_situation_evidence_article
    ON situation_evidence(article_id);

CREATE TABLE IF NOT EXISTS situation_relations (
    a_id       TEXT NOT NULL,
    b_id       TEXT NOT NULL,
    rel_type   TEXT NOT NULL,
    basis      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (a_id, b_id, rel_type)
);

CREATE TABLE IF NOT EXISTS situation_detection_log (
    id           BIGSERIAL PRIMARY KEY,
    run_at       TEXT NOT NULL,
    article_id   TEXT NOT NULL,
    decision     TEXT NOT NULL,
    reason       TEXT NOT NULL DEFAULT '',
    situation_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_situation_detection_run
    ON situation_detection_log(run_at DESC);

-- P0 収集飢餓修正 (2026-07-05): dedup purge を last_seen 基準に変更したため index を追加。
-- last_seen は既存列 (新列ではない) なのでここで CREATE INDEX してよい。
CREATE INDEX IF NOT EXISTS idx_dedup_last_seen ON dedup_seen_urls(last_seen);

-- 監査 2026-07-05 P6: taxonomy_review_proposals の列 drift 修正。SQLite 側は
-- reviewed_at/reviewed_by を持ち INSERT/UPDATE が両列を書くが、PG には無かった
-- (GROUP_CONCAT 沈黙死で INSERT 自体が走らず隠れていた複合故障)。
-- decided_at は pg_schema 初版の残骸だが live PG に存在するため残置 (未使用)。
ALTER TABLE taxonomy_review_proposals ADD COLUMN IF NOT EXISTS reviewed_at TEXT;
ALTER TABLE taxonomy_review_proposals ADD COLUMN IF NOT EXISTS reviewed_by TEXT DEFAULT 'manual';

-- 監査 2026-07-05 P4 (将来予測): Situation indicators の forecast lifecycle (open→scored)。
CREATE TABLE IF NOT EXISTS situation_forecasts (
    id           BIGSERIAL PRIMARY KEY,
    situation_id TEXT    NOT NULL,
    indicator    TEXT    NOT NULL,
    opened_at    TEXT    NOT NULL,
    horizon_days INTEGER NOT NULL DEFAULT 30,
    status       TEXT    NOT NULL DEFAULT 'open',
    scored_at    TEXT,
    note         TEXT
);
-- 較正 (2026-08-14): この指標を LLM に何回提示したか。0 = 一度も照会していない
-- → 未発火でも外れとは採点できない (unevaluated)。判定 SSoT は forecast.py _terminal_status。
ALTER TABLE situation_forecasts ADD COLUMN IF NOT EXISTS presented_count INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_situation_forecasts_open
    ON situation_forecasts(situation_id, status);
CREATE INDEX IF NOT EXISTS idx_situation_forecasts_scored
    ON situation_forecasts(scored_at);

-- 監査 2026-07-05: article_id 索引 (PK は id serial)。記事引き / orphan 掃除の全 scan 解消。
CREATE INDEX IF NOT EXISTS idx_articles_article_id ON articles(article_id);

-- 監査 2026-07-05 P2: per-feed fetch 結果の永続化 (feed 死活検知)。
-- 「エラーにならない失敗」(恒常無産出) を run 成否と独立に観測する。
CREATE TABLE IF NOT EXISTS source_fetch_health (
    source_key           TEXT    NOT NULL PRIMARY KEY,
    name                 TEXT    NOT NULL,
    last_ok_at           TEXT,
    last_error_at        TEXT,
    last_error           TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_article_count   INTEGER NOT NULL DEFAULT 0,
    updated_at           TEXT    NOT NULL
);

-- 認証の監査証跡 (2026-08-02、SQLite _SCHEMA と対)。Tier1 の成功・失敗を両方残す。
-- stdout ログは deploy とローテーションで消えるため監査には DB 永続が要る。
-- §4 により email は保存しない (識別は subject の SHA-256 先頭 12 桁)。
CREATE TABLE IF NOT EXISTS access_audit (
    id           BIGSERIAL PRIMARY KEY,
    at           TEXT NOT NULL,
    event        TEXT NOT NULL,
    subject_hash TEXT NOT NULL DEFAULT '',
    method       TEXT NOT NULL DEFAULT '',
    path         TEXT NOT NULL DEFAULT '',
    client_ip    TEXT NOT NULL DEFAULT '',
    country      TEXT NOT NULL DEFAULT '',
    detail       TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_access_audit_at ON access_audit(at);

-- 背景ジョブの最終実行 (2026-07-06 統一ジョブ制御、SQLite _SCHEMA と対)
CREATE TABLE IF NOT EXISTS job_last_run (
    job_id      TEXT NOT NULL PRIMARY KEY,
    last_run_at TEXT NOT NULL,
    status      TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT ''
);

-- 背景ジョブの実行履歴 (2026-07-07 実行状況一覧性、SQLite _SCHEMA と対)
CREATE TABLE IF NOT EXISTS job_run_log (
    id       BIGSERIAL PRIMARY KEY,
    job_id   TEXT NOT NULL,
    ran_at   TEXT NOT NULL,
    status   TEXT NOT NULL,
    detail   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_job_run_log_job ON job_run_log(job_id, ran_at DESC);

-- 日次ブリーフ構造化 payload (2026-07-12): Web はテキストでなく構造から描画する。
-- 既存 DB への新列は末尾 ALTER (本体 CREATE に index を足すと既存 DB で crash する既知 gotcha)。
ALTER TABLE daily_briefs ADD COLUMN IF NOT EXISTS payload TEXT;

-- 常設情報要求 standing situations (段A, 2026-07-13): event/standing の種別。
-- 設計: docs/prepositioning_posture_ledger_design.md
ALTER TABLE situations ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'event';

-- 証拠台帳の状態分離 (2026-07-16): 割当 (観測) と ACH 評価 (判断) を別状態にする。
-- read_at = 接地 prompt に本文が供給された最終時刻 / assessed_at = ACH 引用の最終時刻。
-- polarity/excerpt 等の評価フィールドは assessed_at IS NOT NULL の行でのみ有意。
ALTER TABLE situation_evidence ADD COLUMN IF NOT EXISTS read_at TEXT;
ALTER TABLE situation_evidence ADD COLUMN IF NOT EXISTS assessed_at TEXT;
-- 宣言的 backfill (冪等・初回のみ実質作用): 旧 rich 行 (excerpt あり = ACH 引用済) を
-- 評価済みに刻む。新規行は record_assessment が常に assessed_at を書くため再 match しない。
UPDATE situation_evidence SET assessed_at = added_at, read_at = added_at
 WHERE assessed_at IS NULL AND excerpt <> '';

-- 概念 PIR の LLM 主題判定 verdict (2026-07-23、docs/pir_concept_llm_judge_design.md)。
-- 負の verdict も保存 (再判定防止)。pir_rev = PIR description+question ハッシュ (編集で stale)。
CREATE TABLE IF NOT EXISTS pir_llm_judgments (
    article_id TEXT NOT NULL,
    pir_id     TEXT NOT NULL,
    matched    SMALLINT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    pir_rev    TEXT NOT NULL,
    judged_at  TEXT NOT NULL,
    PRIMARY KEY (article_id, pir_id)
);
CREATE INDEX IF NOT EXISTS idx_pir_llm_judgments_pir
    ON pir_llm_judgments(pir_id, judged_at DESC);

-- 外部 LLM 呼出のトークン消費記録 (2026-07-24、接続先レジストリ)。UI の消費表示
-- (5h窓/今日/7日) 用。claudecode は bridge 自己観測が SSoT のためここには記録しない。
CREATE TABLE IF NOT EXISTS llm_usage (
    id            BIGSERIAL PRIMARY KEY,
    ts            TEXT NOT NULL,
    provider      TEXT NOT NULL,
    model         TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    duration_ms   INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd      DOUBLE PRECISION NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_provider_ts
    ON llm_usage(provider, ts DESC);
-- 消費台帳の一本化 (2026-07-26): claudecode (サブスク) も llm_usage に記録する。
-- cache_read_tokens / cost_usd は bridge が CLI から得る値 (他 provider は 0)。
-- 既存 DB 向けの冪等追加。
ALTER TABLE llm_usage ADD COLUMN IF NOT EXISTS cache_read_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE llm_usage ADD COLUMN IF NOT EXISTS cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0;

-- アクター辞書 Phase1 F7 (2026-07-26): アクター行動史の月次期間行 (決定論蒸留)。
-- 詳細コメントは schema_sql.py 側を参照 (両 backend で列名統一、JSON 列は PG=JSONB)。
CREATE TABLE IF NOT EXISTS actor_observed_profile (
    actor_id         TEXT NOT NULL,
    month            TEXT NOT NULL,
    subject_articles INTEGER NOT NULL DEFAULT 0,
    distinct_sources INTEGER NOT NULL DEFAULT 0,
    sectors          JSONB NOT NULL DEFAULT '{}',
    countries        JSONB NOT NULL DEFAULT '{}',
    malware          JSONB NOT NULL DEFAULT '{}',
    ttps             JSONB NOT NULL DEFAULT '{}',
    campaigns        JSONB NOT NULL DEFAULT '{}',
    japan_targeted   INTEGER NOT NULL DEFAULT 0,
    kev_hits         INTEGER NOT NULL DEFAULT 0,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (actor_id, month)
);

-- アクター辞書 Phase2 F5 (2026-07-26): alias 使用統計 (詳細は schema_sql.py 側)。
CREATE TABLE IF NOT EXISTS actor_alias_usage (
    article_id TEXT NOT NULL,
    actor_id   TEXT NOT NULL,
    name       TEXT NOT NULL,
    month      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (article_id, actor_id, name)
);
CREATE INDEX IF NOT EXISTS idx_alias_usage_actor
    ON actor_alias_usage(actor_id, month);

-- 被害国スコープ (監査 2026-08-01 ⑥): ISO2 に解決できない "global"/"EU"/複数国の受け皿。
ALTER TABLE articles ADD COLUMN IF NOT EXISTS victim_country_scope TEXT;

-- 主題判定の根拠文 (2026-08-13 可視化): 特に「主題なし」の理由を記事詳細に表示する。
ALTER TABLE articles ADD COLUMN IF NOT EXISTS subject_actor_rationale TEXT;

-- 本文日本語訳のチャンク単位キャッシュ (2026-08-06 resumable 翻訳)。
-- 詳細コメントは schema_sql.py の同表を参照 (SQLite と対)。
CREATE TABLE IF NOT EXISTS body_ja_chunks (
    article_id TEXT    NOT NULL,
    seq        INTEGER NOT NULL,
    total      INTEGER NOT NULL,
    body_hash  TEXT    NOT NULL,
    text       TEXT    NOT NULL,
    created_at TEXT    NOT NULL,
    PRIMARY KEY (article_id, seq)
);

-- ops 通知の永続化 (2026-08-21、SQLite _SCHEMA と対)。post_ops_message は従来 Discord ops
-- webhook へ送るだけで DB に残さず、webhook 不達・未設定の警告が痕跡なく消えていた
-- (確立教訓「stdout は監査にならない」「成功も記録しないと沈黙の意味を決められない」に
-- 反する)。webhook 送信の成否に関わらず必ず 1 行残す (access_audit と同型の設計)。
CREATE TABLE IF NOT EXISTS ops_notices (
    id         BIGSERIAL PRIMARY KEY,
    created_at TEXT     NOT NULL,
    title      TEXT     NOT NULL,
    body       TEXT     NOT NULL,
    importance TEXT     NOT NULL DEFAULT 'low',
    sent       SMALLINT NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_ops_notices_created_at ON ops_notices(created_at);
"""


def get_schema_sql() -> str:
    """全 schema を 1 つの SQL string で返す (idempotent、CREATE TABLE IF NOT EXISTS のみ)。"""
    return PG_SCHEMA_SQL
