"""SQLite スキーマ SQL テキスト (run_history 分割の一部)。

``_SCHEMA_SQL`` は ``src.storage.run_history`` から re-export され、
``situation_store`` 等が参照する idempotent なスキーマ定義。
"""

from __future__ import annotations

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT    NOT NULL,
    finished_at     TEXT,
    pipeline        TEXT    NOT NULL,
    dry_run         INTEGER NOT NULL DEFAULT 0,
    triggered_by    TEXT    NOT NULL DEFAULT 'scheduler',
    status          TEXT    NOT NULL DEFAULT 'running',
    total_fetched   INTEGER NOT NULL DEFAULT 0,
    summarized      INTEGER NOT NULL DEFAULT 0,
    posted          INTEGER NOT NULL DEFAULT 0,
    marked_read     INTEGER NOT NULL DEFAULT 0,
    error_count     INTEGER NOT NULL DEFAULT 0,
    note            TEXT
    -- Phase 5P: log_line_count / log_truncated / triage_error_count /
    -- partial_fetch / partial_fetch_count は _apply_migrations で付与する
    -- (旧 DB との互換のため CREATE TABLE には含めない)
);

CREATE INDEX IF NOT EXISTS idx_runs_started_at ON runs(started_at);

CREATE TABLE IF NOT EXISTS articles (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           INTEGER NOT NULL,
    article_id       TEXT    NOT NULL,
    title            TEXT    NOT NULL,
    url              TEXT    NOT NULL,
    feed_title       TEXT,
    -- 安定 source キー (= ManagedSource.url)。表示名 feed_title と結合キーを分離するため永続化。
    feed_url         TEXT,
    importance       TEXT,
    category         TEXT,
    status           TEXT    NOT NULL,
    failure_reason   TEXT,
    posted_channel   TEXT,
    duration_seconds REAL,
    -- Phase 5L-4: 同事象クラスタリング用キー (LLM 提供 dedup_key or CVE-ID)。
    -- 同 key の article は同事象として扱い ch 横断で重複抑制できる。
    dedup_key        TEXT,
    -- Phase 5T-K: Discord 投稿時 (wait=true) に取得した message_id/channel_id。
    -- digest pipeline が「digest 項目 → 元 Discord 投稿への 1-click 遷移」を生成。
    -- 投稿失敗 / 取得失敗時は NULL (digest 側で元記事 URL に fallback)。
    discord_message_id TEXT,
    discord_channel_id TEXT,
    -- Phase 5T-P: BriefingMessage.summary を永続化 (E1/F1 digest 生成で活用)。
    -- 過去レコード (5T-P 以前) は NULL、digest 側で title fallback。
    summary          TEXT,
    -- Phase Diamond L2: trafilatura で取得した記事本文 (~3-5KB/件)。
    -- 主用途: IoC / TTP / malware 再抽出 (article_entities backfill)。
    -- 90 日 retention (post 経路で取得時刻記録)。
    body             TEXT,
    body_fetched_at  TEXT,
    created_at       TEXT    NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_articles_run_id ON articles(run_id);
CREATE INDEX IF NOT EXISTS idx_articles_importance ON articles(importance);
CREATE INDEX IF NOT EXISTS idx_articles_status ON articles(status);
CREATE INDEX IF NOT EXISTS idx_articles_created_at ON articles(created_at);
-- 監査 2026-07-05: PK は id (serial)。article_id での記事引き / orphan 掃除 anti-join /
-- get_articles_by_ids が最大テーブルを全 scan していたため索引を追加。
CREATE INDEX IF NOT EXISTS idx_articles_article_id ON articles(article_id);
-- Phase 5L-4: idx_articles_dedup_key は _apply_migrations で作る
-- (古い DB では dedup_key カラムが先に追加される必要があるため _SCHEMA_SQL では作らない)

CREATE TABLE IF NOT EXISTS run_logs (
    run_id   INTEGER NOT NULL,
    seq      INTEGER NOT NULL,
    ts       TEXT    NOT NULL,
    stream   TEXT    NOT NULL DEFAULT 'stdout',
    line     TEXT    NOT NULL,
    PRIMARY KEY (run_id, seq),
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_run_logs_ts ON run_logs(ts);

-- Phase Diamond: 1 article から抽出された Diamond Model 4 軸の entities
-- (Capability: cve / ttp / malware_family / tool, Infrastructure: ioc_ip / ioc_domain /
--  ioc_sha256 / ioc_sha1 / ioc_md5 / ioc_url)。Adversary / Victim は articles 表に直接列
--  として持つ (actor_aliases.yaml で正規化、victim_sector_canonical / victim_country_iso)。
-- UNIQUE 制約で同一 (article, type, value) を 1 行に集約 (post 経路の冪等性確保)。
CREATE TABLE IF NOT EXISTS article_entities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id  TEXT    NOT NULL,
    entity_type TEXT    NOT NULL,
    value       TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    UNIQUE(article_id, entity_type, value)
);

CREATE INDEX IF NOT EXISTS idx_article_entities_article_id ON article_entities(article_id);
CREATE INDEX IF NOT EXISTS idx_article_entities_type_value ON article_entities(entity_type, value);
CREATE INDEX IF NOT EXISTS idx_article_entities_created_at ON article_entities(created_at);
-- 監査 backlog 2026-07-05: LOWER(value) 照合クエリ (situation board / threat ops /
-- entity 検索の 8 箇所) が全件 scan だった。式索引で解消 (両 backend 対応)。
CREATE INDEX IF NOT EXISTS idx_article_entities_type_value_lower
    ON article_entities(entity_type, LOWER(value));

-- Phase 3a: 投稿済み記事 URL の重複排除キャッシュ
-- url_hash で完全一致を判定する。
CREATE TABLE IF NOT EXISTS dedup_seen_urls (
    url_hash    TEXT    NOT NULL PRIMARY KEY,
    url         TEXT    NOT NULL,
    article_id  TEXT,
    title       TEXT,
    first_seen  TEXT    NOT NULL,
    last_seen   TEXT    NOT NULL,
    seen_count  INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_dedup_first_seen ON dedup_seen_urls(first_seen);
CREATE INDEX IF NOT EXISTS idx_dedup_last_seen ON dedup_seen_urls(last_seen);

-- 監査 2026-07-05 P4 (将来予測): Situation indicators の forecast lifecycle (open→scored)。
-- indicators は「観測されれば判定が変わる」反証可能な予測 — 発火 (hit) / 期限切れ (expired)
-- を採点して的中率 (較正) を実測可能にする (設計 doc §3.6 の実装)。
CREATE TABLE IF NOT EXISTS situation_forecasts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    situation_id TEXT    NOT NULL,
    indicator    TEXT    NOT NULL,
    opened_at    TEXT    NOT NULL,
    horizon_days INTEGER NOT NULL DEFAULT 30,
    status       TEXT    NOT NULL DEFAULT 'open',
    scored_at    TEXT,
    note         TEXT,
    -- 較正 (2026-08-14): この指標を LLM に何回提示したか。0 = 一度も照会していない
    -- → 未発火でも外れとは採点できない (unevaluated)。判定 SSoT は forecast.py
    -- _terminal_status。revision の有無から推測せず「提示した事実」を直接記録する。
    presented_count INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_situation_forecasts_open
    ON situation_forecasts(situation_id, status);
CREATE INDEX IF NOT EXISTS idx_situation_forecasts_scored
    ON situation_forecasts(scored_at);

-- 監査 2026-07-05 P2: per-feed fetch 結果の永続化 (feed 死活検知)。
-- 「エラーにならない失敗」(恒常無産出) を run 成否と独立に観測する。
CREATE TABLE IF NOT EXISTS source_fetch_health (
    source_key           TEXT    NOT NULL PRIMARY KEY,  -- feed URL (名前より安定)
    name                 TEXT    NOT NULL,
    last_ok_at           TEXT,
    last_error_at        TEXT,
    last_error           TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_article_count   INTEGER NOT NULL DEFAULT 0,
    updated_at           TEXT    NOT NULL
);

-- 認証の監査証跡 (2026-08-02)。公開 instance の Tier1 (Cloudflare Access) について
-- **成功・失敗の両方**を残す。stdout ログだけでは deploy (コンテナ再作成) や docker の
-- ログローテーションで消えるため、監査には DB 永続が要る (実測 2026-08-02: 再作成で
-- 40 時間分のログが失われ、ログイン実績の有無を判定できなくなった)。
-- §4 の機密規約により **email は保存しない** — 識別は subject の SHA-256 先頭 12 桁。
CREATE TABLE IF NOT EXISTS access_audit (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    at           TEXT NOT NULL,               -- ISO8601 UTC
    event        TEXT NOT NULL,               -- authenticated / rejected / tier1_write
    subject_hash TEXT NOT NULL DEFAULT '',    -- 認証済みのみ (email は保存しない)
    method       TEXT NOT NULL DEFAULT '',
    path         TEXT NOT NULL DEFAULT '',
    client_ip    TEXT NOT NULL DEFAULT '',
    country      TEXT NOT NULL DEFAULT '',    -- Cf-Ipcountry (異常な所在地の検知用)
    detail       TEXT NOT NULL DEFAULT ''     -- 拒否理由の型名など
);

CREATE INDEX IF NOT EXISTS idx_access_audit_at ON access_audit(at);

-- Phase 3b: 投稿済み記事の embedding (意味的重複排除用)
-- 個人運用規模 (年間数千件) では SQLite の BLOB + numpy で十分高速。
-- Phase 5 でスケールが必要になったら ChromaDB / pgvector に移行する。
CREATE TABLE IF NOT EXISTS article_embeddings (
    url_hash   TEXT    NOT NULL PRIMARY KEY,
    url        TEXT    NOT NULL,
    title      TEXT,
    model      TEXT    NOT NULL,
    dim        INTEGER NOT NULL,
    vector     BLOB    NOT NULL,        -- float32 little-endian
    created_at TEXT    NOT NULL,
    FOREIGN KEY (url_hash) REFERENCES dedup_seen_urls(url_hash) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_article_embeddings_created ON article_embeddings(created_at);
CREATE INDEX IF NOT EXISTS idx_article_embeddings_model ON article_embeddings(model);

-- Phase 5L-1: ops 通知の rate limit 用の最終送信時刻記録
-- pipeline 名ごとに「直近の成功 ops 通知」を記録し、interval 経路では
-- 24h 以内の連続通知を抑制する (失敗時は別経路で常時通知)。
CREATE TABLE IF NOT EXISTS ops_notify_log (
    pipeline_name TEXT NOT NULL PRIMARY KEY,
    last_sent_at  TEXT NOT NULL,
    last_status   TEXT NOT NULL  -- 'success' | 'failure'
);

-- Phase 5T-T1: F1 (weekly deep dive) 選定履歴。
-- novelty 判定で過去 N 時間以内の同 dedup_key を検索し、
-- 5T-T6 で composite score 重みの post-hoc 回帰にも使う。
CREATE TABLE IF NOT EXISTS f1_selections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL,
    article_id      TEXT    NOT NULL,
    dedup_key       TEXT,
    composite_score REAL    NOT NULL,
    pir             REAL    NOT NULL,
    roi             REAL    NOT NULL,
    timeliness      REAL    NOT NULL,
    novelty         REAL    NOT NULL,
    selected_at     TEXT    NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_f1_selections_selected_at
    ON f1_selections(selected_at);
CREATE INDEX IF NOT EXISTS idx_f1_selections_dedup_key
    ON f1_selections(dedup_key);

-- 段5: F1 weekly recap 本文の永続化 (Retrospect で「あの週の深掘り」を読めるように)。
-- generated_at は ISO 文字列で保存し、Retrospect の週 window と文字列比較する (dialect 非依存)。
CREATE TABLE IF NOT EXISTS weekly_recaps (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER,
    period_label    TEXT    NOT NULL,
    recap_text      TEXT    NOT NULL,
    candidate_count INTEGER NOT NULL DEFAULT 0,
    generated_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_weekly_recaps_generated_at
    ON weekly_recaps(generated_at);

-- W1 (通知再設計): 日次ブリーフ (朝刊/夕刊) 合成本文の永続化。
-- 旧来ブリーフ本文は Discord のみで蒸発していた。Web「日次ブリーフ」ビューで pull 閲覧
-- できるよう本文 (synthesis + PIR focus) を保存する。generated_at は ISO 文字列で保存。
CREATE TABLE IF NOT EXISTS daily_briefs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER,
    slot            TEXT    NOT NULL,         -- 'morning' (朝刊) / 'evening' (夕刊)
    period_label    TEXT    NOT NULL,         -- 'YYYY-MM-DD' (JST)
    title           TEXT    NOT NULL,
    bluf            TEXT    NOT NULL DEFAULT '',
    summary         TEXT    NOT NULL,
    section_count   INTEGER NOT NULL DEFAULT 0,
    sources         TEXT    NOT NULL DEFAULT '[]',  -- JSON: [{title,url}]
    payload         TEXT,                           -- JSON: 構造化 payload (Web 構造描画用)
    generated_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_daily_briefs_generated_at
    ON daily_briefs(generated_at);

-- Phase 3 (Phase H 後継): 状況総括 synthesis の永続化。
-- 週次 / 月次 で生成され、PMESII-PT 軸を関係性で総括した narrative + 軸別 evidence
-- を保存。当該期間 (period_type + period_start) ごとに 1 行 (再生成は上書き)。
CREATE TABLE IF NOT EXISTS status_synthesis (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    period_type     TEXT    NOT NULL,  -- 'weekly' / 'monthly'
    period_start    TEXT    NOT NULL,  -- ISO date (UTC)
    period_end      TEXT    NOT NULL,
    headline        TEXT    NOT NULL,
    weight_section  TEXT    NOT NULL,  -- 軸別 weight 分類 (markdown)
    chain_section   TEXT    NOT NULL,  -- 軸間連鎖 narrative
    cog_section     TEXT    NOT NULL,  -- 重心 (CoG)
    spillover_section TEXT  NOT NULL,  -- 波及解釈
    pir_section     TEXT    NOT NULL,  -- PIR 達成度
    axes_evidence   TEXT    NOT NULL,  -- JSON: 軸別 evidence (drill-down 用)
    tradecraft      TEXT,              -- S2: JSON 主見立て+対立仮説+前提+覆る指標 (ICD 203)
    article_count   INTEGER NOT NULL,
    llm_model       TEXT,
    generated_at    TEXT    NOT NULL,
    UNIQUE(period_type, period_start)
);

CREATE INDEX IF NOT EXISTS idx_synthesis_period
    ON status_synthesis(period_type, period_start DESC);

-- Phase Diamond verify-spotlight: PIR Spotlight (PIR 縦断 narrative) の永続化。
-- weekly pir-spotlight pipeline が config/pir.yaml の spotlight.enabled=true な
-- PIR each に対して narrative 生成 → UPSERT。global synthesis との補完関係。
CREATE TABLE IF NOT EXISTS pir_spotlight (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pir_id          TEXT    NOT NULL,
    pir_title       TEXT    NOT NULL,
    period_type     TEXT    NOT NULL,  -- 'daily' / 'weekly' / 'monthly'
    period_start    TEXT    NOT NULL,  -- ISO datetime (UTC)
    period_end      TEXT    NOT NULL,
    headline        TEXT    NOT NULL,
    outlook         TEXT    NOT NULL,
    key_events      TEXT    NOT NULL,  -- JSON: list[KeyEvent] (PG は JSONB、列名は両 backend 統一)
    article_count   INTEGER NOT NULL,
    llm_model       TEXT    NOT NULL,
    generated_at    TEXT    NOT NULL,
    UNIQUE(pir_id, period_type, period_start)
);

CREATE INDEX IF NOT EXISTS idx_spotlight_pir_period
    ON pir_spotlight(pir_id, period_type, period_start DESC);

-- Phase H: taxonomy review LLM 提案の永続化。
-- weekly-taxonomy-review pipeline (月曜 10:00 JST) が 8 パターン分の
-- 提案を生成、user が UI で review (Tier 1: 1-click / Tier 2/3: 個別)。
CREATE TABLE IF NOT EXISTS taxonomy_review_proposals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER,
    proposal_type   TEXT    NOT NULL,
    tier            TEXT    NOT NULL,
    target_yaml     TEXT    NOT NULL,
    target_canonical TEXT,
    proposed_change TEXT    NOT NULL,
    rationale       TEXT    NOT NULL,
    confidence      TEXT    NOT NULL,
    evidence_count  INTEGER DEFAULT 0,
    evidence_ids    TEXT,
    status          TEXT    DEFAULT 'pending',
    created_at      TEXT    NOT NULL,
    reviewed_at     TEXT,
    reviewed_by     TEXT    DEFAULT 'manual',
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_taxonomy_status_tier
    ON taxonomy_review_proposals(status, tier);
CREATE INDEX IF NOT EXISTS idx_taxonomy_created_at
    ON taxonomy_review_proposals(created_at);

-- Actors Stage 4: MITRE 同期のレビュー提案 (新規 actor 追加 / alias 衝突)。
-- 安全な追加系差分は自動適用されるため、ここには判断が必要なものだけが入る。
-- dedup_key で同一提案の再挿入を防ぐ (rejected も含む → 却下が記憶される)。
CREATE TABLE IF NOT EXISTS actor_update_proposals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER,
    proposal_type   TEXT    NOT NULL,  -- 'mitre_new_actor' / 'mitre_alias_conflict'
    mitre_group     TEXT    NOT NULL,
    dedup_key       TEXT    NOT NULL,
    actor_id        TEXT,              -- alias_conflict: MITRE 側の帰属先 actor
    payload         TEXT    NOT NULL,  -- JSON: 提案内容 (新規 actor dict / alias 付替え情報)
    rationale       TEXT    NOT NULL,
    status          TEXT    DEFAULT 'pending',  -- 'pending' / 'accepted' / 'rejected'
    created_at      TEXT    NOT NULL,
    decided_at      TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_actor_proposals_status
    ON actor_update_proposals(status);

-- Phase 4 (将来予測 FC2): spike から導いた監視指標 + 翌期間の的中検証。
CREATE TABLE IF NOT EXISTS forecast_indicators (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    period_type     TEXT    NOT NULL,  -- 'weekly' / 'monthly'
    period_start    TEXT    NOT NULL,  -- 予測を立てた期間の開始 (ISO)
    scope           TEXT    NOT NULL,  -- 'actor' / 'intent' / 'cve' / 'malware_family'
    target_value    TEXT    NOT NULL,
    direction       TEXT    NOT NULL,  -- 'rising' / 'watch'
    z_score         REAL    NOT NULL DEFAULT 0,
    baseline_avg    REAL    NOT NULL DEFAULT 0,
    latest_count    INTEGER NOT NULL DEFAULT 0,
    rationale       TEXT    NOT NULL DEFAULT '',
    created_at      TEXT    NOT NULL,
    verified_at     TEXT,               -- NULL=未検証
    hit             INTEGER,            -- NULL/0/1
    observed_count  INTEGER NOT NULL DEFAULT 0,
    UNIQUE(period_type, period_start, scope, target_value)
);
CREATE INDEX IF NOT EXISTS idx_forecast_indicators_period
    ON forecast_indicators(period_type, period_start DESC);
CREATE INDEX IF NOT EXISTS idx_forecast_indicators_open
    ON forecast_indicators(period_type, verified_at);

-- Phase 5 (学習・記憶): 記事への個人 memo / bookmark / 自由 tag / judgment log。
-- 1 article = 1 行 (article_id PK で upsert)。tags は JSON array 文字列 (TEXT)。
CREATE TABLE IF NOT EXISTS article_notes (
    article_id  TEXT    NOT NULL PRIMARY KEY,
    bookmarked  INTEGER NOT NULL DEFAULT 0,
    note        TEXT    NOT NULL DEFAULT '',
    tags        TEXT    NOT NULL DEFAULT '[]',
    judgment    TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_article_notes_bookmarked
    ON article_notes(bookmarked, updated_at DESC);

-- Situation Ledger (状況台帳): 持続する情勢ライン + 判定 revision + 証拠 + 関係 + 選定台帳。
-- 設計: docs/synthesis_situation_ledger_design.md (段A)。SYNTHESIS_STATE flag 裏。
CREATE TABLE IF NOT EXISTS situations (
    situation_id     TEXT    NOT NULL PRIMARY KEY,  -- 's-'+sha1(title) 短縮。standing は明示 id
    title            TEXT    NOT NULL,
    domain           TEXT    NOT NULL DEFAULT 'unclassified',
    status           TEXT    NOT NULL DEFAULT 'active',  -- active / dormant / closed
    anchors          TEXT    NOT NULL DEFAULT '[]',  -- JSON ["type:value", ...] (identity/割当キー)
    pir_ids          TEXT    NOT NULL DEFAULT '[]',  -- JSON (証拠記事の pir entity 由来)
    opened_at        TEXT    NOT NULL,
    last_evidence_at TEXT    NOT NULL,
    closed_at        TEXT,
    kind             TEXT    NOT NULL DEFAULT 'event'  -- event / standing (常設情報要求、段A)
);
CREATE INDEX IF NOT EXISTS idx_situations_status
    ON situations(status, last_evidence_at DESC);

CREATE TABLE IF NOT EXISTS situation_revisions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    situation_id       TEXT    NOT NULL,
    rev                INTEGER NOT NULL,
    run_id             TEXT    NOT NULL DEFAULT '',
    claim              TEXT    NOT NULL,
    claim_type         TEXT    NOT NULL DEFAULT 'ongoing_activity',
    leading_hypothesis TEXT    NOT NULL,
    confidence         TEXT    NOT NULL,
    confidence_basis   TEXT    NOT NULL DEFAULT '',
    hypotheses         TEXT    NOT NULL DEFAULT '[]',  -- JSON ACH 行列
    assumptions        TEXT    NOT NULL DEFAULT '[]',
    missing            TEXT    NOT NULL DEFAULT '[]',
    indicators         TEXT    NOT NULL DEFAULT '[]',
    implication        TEXT    NOT NULL DEFAULT '',
    delta_type         TEXT    NOT NULL,  -- opened/strengthened/weakened/hypothesis_flip/... (§2.1)
    delta_note         TEXT    NOT NULL DEFAULT '',
    created_at         TEXT    NOT NULL,
    UNIQUE(situation_id, rev)
);
CREATE INDEX IF NOT EXISTS idx_situation_revisions_sid
    ON situation_revisions(situation_id, rev DESC);

-- 観測と判断は別状態 (2026-07-16 状態分離): read_at = 接地 prompt に本文が供給された
-- 最終時刻 (NULL=未読)、assessed_at = ACH が証拠として引用した最終時刻 (NULL=未評価)。
-- polarity/attribution_basis/excerpt/source_tier は assessed_at IS NOT NULL の行でのみ有意
-- (割当だけの行を「中立の証拠」と混同しない)。
CREATE TABLE IF NOT EXISTS situation_evidence (
    situation_id      TEXT NOT NULL,
    article_id        TEXT NOT NULL,
    polarity          TEXT NOT NULL DEFAULT 'neutral',
    attribution_basis TEXT NOT NULL DEFAULT 'unattributed',
    excerpt           TEXT NOT NULL DEFAULT '',
    source_tier       TEXT NOT NULL DEFAULT 'unknown',
    added_at          TEXT NOT NULL,
    assigned_by       TEXT NOT NULL DEFAULT 'anchor',  -- seed / anchor / nation / token / llm
    read_at           TEXT,
    assessed_at       TEXT,
    PRIMARY KEY (situation_id, article_id)
);
CREATE INDEX IF NOT EXISTS idx_situation_evidence_article
    ON situation_evidence(article_id);

CREATE TABLE IF NOT EXISTS situation_relations (
    a_id       TEXT NOT NULL,
    b_id       TEXT NOT NULL,
    rel_type   TEXT NOT NULL,  -- same_actor / same_campaign / shared_nation / temporal_sequence
    basis      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (a_id, b_id, rel_type)
);

CREATE TABLE IF NOT EXISTS situation_detection_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at       TEXT NOT NULL,
    article_id   TEXT NOT NULL,
    decision     TEXT NOT NULL,  -- assigned / opened / rejected / unassigned
    reason       TEXT NOT NULL DEFAULT '',
    situation_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_situation_detection_run
    ON situation_detection_log(run_at DESC);

-- 背景ジョブの最終実行 (2026-07-06 統一ジョブ制御)。bespoke/reactive ジョブの
-- 「いつ最後に走り成否は」を 1 行/job で記録 (K1 pipeline は runs テーブルが持つ)。
CREATE TABLE IF NOT EXISTS job_last_run (
    job_id      TEXT NOT NULL PRIMARY KEY,
    last_run_at TEXT NOT NULL,
    status      TEXT NOT NULL,  -- succeeded / failed
    detail      TEXT NOT NULL DEFAULT ''
);

-- 背景ジョブの実行履歴 (2026-07-07 実行状況一覧性)。bespoke/reactive ジョブの
-- 複数実行記録を append で残す (K1 pipeline は runs テーブルが履歴を持つ)。
-- 詳細パネルの実行履歴表示用。30 日超は起動時 purge。
CREATE TABLE IF NOT EXISTS job_run_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id   TEXT NOT NULL,
    ran_at   TEXT NOT NULL,
    status   TEXT NOT NULL,  -- succeeded / failed
    detail   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_job_run_log_job ON job_run_log(job_id, ran_at DESC);

-- 概念 PIR の LLM 主題判定 verdict (2026-07-23、docs/pir_concept_llm_judge_design.md)。
-- 負の verdict も保存する (不適合候補を毎日再判定しないための要)。
-- pir_rev = PIR title+description+question のハッシュ — PIR 編集で自動 stale になり
-- 次回バッチが再判定する。verdict は category と同格の「永続化された意味的事実」で、
-- 消費 (rebuild/KPI/preview) は決定論のまま。
CREATE TABLE IF NOT EXISTS pir_llm_judgments (
    article_id TEXT NOT NULL,
    pir_id     TEXT NOT NULL,
    matched    INTEGER NOT NULL,           -- 1/0
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
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    provider      TEXT NOT NULL,          -- "anthropic" / "claudecode" / 接続先 slug
    model         TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    duration_ms   INTEGER NOT NULL DEFAULT 0,
    -- 消費台帳の一本化 (2026-07-26): claudecode (サブスク) も本テーブルへ記録。
    -- cache_read_tokens / cost_usd は bridge が CLI から得る自己観測値 (他 provider は 0)。
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_provider_ts
    ON llm_usage(provider, ts DESC);

-- アクター辞書 Phase1 F7 (2026-07-26): アクター行動史の月次期間行 (決定論蒸留)。
-- subject 記事 (articles.subject_actor_ids、主題判定層 2026-07-17 稼働) の月次集計を
-- 週次 actor-history-distill ジョブが当月+前月のみ UPSERT (それ以前の月は不干渉)。
-- 行は判断記録ではなく決定論射影 — 上流訂正時の窓内再蒸留は正当。再計算不能域
-- (body purge 後) では「当時の identity モデル下の観測記録」として立つ (観測≠世界)。
-- actor_id は書込時 canonical のまま不改変、merge 後の合算は表示時 resolve_actor_id()。
CREATE TABLE IF NOT EXISTS actor_observed_profile (
    actor_id         TEXT NOT NULL,
    month            TEXT NOT NULL,               -- 'YYYY-MM' (JST 境界)
    subject_articles INTEGER NOT NULL DEFAULT 0,  -- distinct article_id (run 横断重複排除)
    distinct_sources INTEGER NOT NULL DEFAULT 0,  -- distinct feed_url (量的支配の区別用)
    sectors          TEXT NOT NULL DEFAULT '{}',  -- JSON: {sector: count} (PG は JSONB)
    countries        TEXT NOT NULL DEFAULT '{}',  -- JSON: {iso2: count}
    malware          TEXT NOT NULL DEFAULT '{}',  -- JSON: {family: count}
    ttps             TEXT NOT NULL DEFAULT '{}',  -- JSON: {ttp_id: count}
    campaigns        TEXT NOT NULL DEFAULT '{}',  -- JSON: {name: count}
    japan_targeted   INTEGER NOT NULL DEFAULT 0,  -- 日本標的 subject 記事数 (japan_relevance SSoT)
    kev_hits         INTEGER NOT NULL DEFAULT 0,  -- KEV 掲載 CVE を含む subject 記事数
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (actor_id, month)
);

-- アクター辞書 Phase2 F5 (2026-07-26): alias 使用統計。取込時の本文照合で実際に
-- ヒットした名前 (canonical/alias) を記事単位で記録し、「どの別名が実際に発火して
-- いるか」を辞書 UI に開示する (死に alias の整理判断の材料)。記事単位 PK により
-- run 横断の重複行でも二重計上しない (INSERT OR IGNORE)。purge 対象外。
CREATE TABLE IF NOT EXISTS actor_alias_usage (
    article_id TEXT NOT NULL,
    actor_id   TEXT NOT NULL,
    name       TEXT NOT NULL,   -- ヒットした名前 (canonical or alias、辞書表記)
    month      TEXT NOT NULL,   -- 'YYYY-MM' (JST)
    created_at TEXT NOT NULL,
    PRIMARY KEY (article_id, actor_id, name)
);
CREATE INDEX IF NOT EXISTS idx_alias_usage_actor
    ON actor_alias_usage(actor_id, month);

-- 本文日本語訳のチャンク単位キャッシュ (2026-08-06 resumable 翻訳)。
-- 翻訳器はチャンク 1 つ訳すごとにここへ確定保存し、失敗・時間切れ後は
-- 未訳チャンクだけを続きから処理する。全チャンク完了で articles.body_ja へ
-- 連結保存し本表の行は削除する (= 平常時は空。行が残る = 翻訳が途中)。
-- body_hash は分割元本文の指紋で、reprocess の本文差し替えを検知して
-- 古い部分訳を無効化する。
CREATE TABLE IF NOT EXISTS body_ja_chunks (
    article_id TEXT    NOT NULL,
    seq        INTEGER NOT NULL,   -- 0 始まりのチャンク番号
    total      INTEGER NOT NULL,   -- 分割時のチャンク総数 (進捗表示用)
    body_hash  TEXT    NOT NULL,   -- 分割元本文の SHA-256 先頭 16 桁
    text       TEXT    NOT NULL,
    created_at TEXT    NOT NULL,
    PRIMARY KEY (article_id, seq)
);
"""
