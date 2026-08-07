-- ==========================================================================
-- 拡大バッチ 1 (2026-05-14) 観察 SQL
--
-- Phase 5O 観察フェーズ完了を待たず +5 ソースを subscribe した判断の検証用。
-- 2026-05-17 (3 日後) を目安に実行し、以下を確認する:
--   1. 1 run の処理時間が 15 分超に膨らんでいないか
--   2. brief 偏重になっていないか
--   3. 新ソース由来の dedup ヒット率が極端に高くないか (高すぎ = 価値が低い)
--   4. triage_error_count / partial_fetch_count が連日発生していないか
--
-- 実行: sqlite3 data/run_history.db < scripts/expansion_batch_1_observation.sql
-- ==========================================================================

.headers on
.mode column

-- ----- Q1: 拡大前後の 1 run 処理時間と件数 -----
SELECT
    DATE(started_at)                            AS day,
    CAST((julianday(finished_at) - julianday(started_at)) * 24 * 60 AS INT) AS minutes,
    total_fetched,
    summarized,
    posted,
    triage_error_count,
    partial_fetch_count,
    CASE
        WHEN DATE(started_at) < '2026-05-14' THEN 'before'
        ELSE 'after'
    END                                         AS phase
FROM runs
WHERE triggered_by = 'scheduler'
  AND started_at >= '2026-05-10'
ORDER BY started_at DESC;

-- ----- Q2: 拡大後の channel 分布 -----
SELECT
    DATE(r.started_at)                          AS day,
    SUM(CASE WHEN a.posted_channel = 'alert'       THEN 1 ELSE 0 END) AS alert_n,
    SUM(CASE WHEN a.posted_channel = 'brief'       THEN 1 ELSE 0 END) AS brief_n,
    SUM(CASE WHEN a.posted_channel = 'watch'       THEN 1 ELSE 0 END) AS watch_n,
    SUM(CASE WHEN a.posted_channel = 'japan_watch' THEN 1 ELSE 0 END) AS jp_n
FROM articles a
JOIN runs r ON a.run_id = r.id
WHERE r.triggered_by = 'scheduler'
  AND r.started_at >= '2026-05-10'
GROUP BY DATE(r.started_at)
ORDER BY day;

-- ----- Q3: 新ソース 5 件由来の articles (拡大後のみ) -----
SELECT
    DATE(a.created_at) AS day,
    CASE
        WHEN a.url LIKE '%googleprojectzero%' THEN 'project_zero'
        WHEN a.url LIKE '%cisa.gov/cybersecurity-advisories%' THEN 'cisa_advisories'
        WHEN a.url LIKE '%ncsc.gov.uk%'       THEN 'ncsc_uk'
        WHEN a.url LIKE '%msrc.microsoft.com%' THEN 'msrc'
        WHEN a.url LIKE '%volexity.com%'      THEN 'volexity'
    END                                         AS source,
    a.posted_channel,
    a.status,
    COUNT(*)                                    AS n
FROM articles a
JOIN runs r ON a.run_id = r.id
WHERE r.started_at >= '2026-05-14'
  AND (
        a.url LIKE '%googleprojectzero%'
    OR  a.url LIKE '%cisa.gov/cybersecurity-advisories%'
    OR  a.url LIKE '%ncsc.gov.uk%'
    OR  a.url LIKE '%msrc.microsoft.com%'
    OR  a.url LIKE '%volexity.com%'
  )
GROUP BY day, source, a.posted_channel, a.status
ORDER BY day, source;

-- ----- Q4: dedup ヒット (新ソース URL が既存と重複扱いされた件数) -----
SELECT
    CASE
        WHEN d.url LIKE '%googleprojectzero%' THEN 'project_zero'
        WHEN d.url LIKE '%cisa.gov/cybersecurity-advisories%' THEN 'cisa_advisories'
        WHEN d.url LIKE '%ncsc.gov.uk%'       THEN 'ncsc_uk'
        WHEN d.url LIKE '%msrc.microsoft.com%' THEN 'msrc'
        WHEN d.url LIKE '%volexity.com%'      THEN 'volexity'
    END                                         AS source,
    COUNT(*)                                    AS unique_urls,
    SUM(d.seen_count)                           AS total_sightings,
    SUM(CASE WHEN d.seen_count > 1 THEN 1 ELSE 0 END) AS deduped_urls
FROM dedup_seen_urls d
WHERE d.first_seen >= '2026-05-14'
  AND (
        d.url LIKE '%googleprojectzero%'
    OR  d.url LIKE '%cisa.gov/cybersecurity-advisories%'
    OR  d.url LIKE '%ncsc.gov.uk%'
    OR  d.url LIKE '%msrc.microsoft.com%'
    OR  d.url LIKE '%volexity.com%'
  )
GROUP BY source;

-- ----- Q5: 打ち切り条件チェック (連日 triage_error or partial_fetch) -----
SELECT
    DATE(started_at)                            AS day,
    SUM(triage_error_count)                     AS triage_errors,
    SUM(partial_fetch_count)                    AS partial_fetches,
    CASE
        WHEN SUM(triage_error_count) > 0  THEN 'INVESTIGATE'
        WHEN SUM(partial_fetch_count) > 0 THEN 'INVESTIGATE'
        ELSE 'OK'
    END                                         AS verdict
FROM runs
WHERE triggered_by = 'scheduler'
  AND started_at >= '2026-05-10'
GROUP BY DATE(started_at)
ORDER BY day DESC;
