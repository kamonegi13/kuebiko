-- ==========================================================================
-- 拡大バッチ 2 (2026-05-14) 観察 SQL
--
-- バッチ 2 で追加した 5 ソースの効果測定:
--   1. AhnLab ASEC (Korea)
--   2. Dragos Blog (ICS/OT)
--   3. ESET WeLiveSecurity
--   4. Cloudflare Blog
--   5. ZDI (Zero Day Initiative)
--
-- 2026-05-17 (3 日後) 目安で実行。バッチ 1 と並走中なので
-- 期間を 2026-05-14 以降に限定する点に注意。
--
-- 実行: sqlite3 data/run_history.db < scripts/expansion_batch_2_observation.sql
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
    partial_fetch_count
FROM runs
WHERE triggered_by = 'scheduler'
  AND started_at >= '2026-05-14'
ORDER BY started_at DESC;

-- ----- Q2: バッチ 2 新ソース由来の articles -----
SELECT
    DATE(a.created_at) AS day,
    CASE
        WHEN a.url LIKE '%asec.ahnlab.com%'        THEN 'ahnlab_asec'
        WHEN a.url LIKE '%hub.dragos.com%'         THEN 'dragos'
        WHEN a.url LIKE '%dragos.com%'             THEN 'dragos'
        WHEN a.url LIKE '%welivesecurity%'         THEN 'eset'
        WHEN a.url LIKE '%blog.cloudflare%'        THEN 'cloudflare'
        WHEN a.url LIKE '%zerodayinitiative%'      THEN 'zdi'
    END                                         AS source,
    a.posted_channel,
    a.status,
    COUNT(*)                                    AS n
FROM articles a
JOIN runs r ON a.run_id = r.id
WHERE r.started_at >= '2026-05-14'
  AND (
        a.url LIKE '%asec.ahnlab.com%'
    OR  a.url LIKE '%dragos.com%'
    OR  a.url LIKE '%welivesecurity%'
    OR  a.url LIKE '%blog.cloudflare%'
    OR  a.url LIKE '%zerodayinitiative%'
  )
GROUP BY day, source, a.posted_channel, a.status
ORDER BY day, source;

-- ----- Q3: dedup ヒット (新ソース URL が既存と重複扱いされた件数) -----
SELECT
    CASE
        WHEN d.url LIKE '%asec.ahnlab.com%'   THEN 'ahnlab_asec'
        WHEN d.url LIKE '%dragos.com%'        THEN 'dragos'
        WHEN d.url LIKE '%welivesecurity%'    THEN 'eset'
        WHEN d.url LIKE '%blog.cloudflare%'   THEN 'cloudflare'
        WHEN d.url LIKE '%zerodayinitiative%' THEN 'zdi'
    END                                         AS source,
    COUNT(*)                                    AS unique_urls,
    SUM(d.seen_count)                           AS total_sightings,
    SUM(CASE WHEN d.seen_count > 1 THEN 1 ELSE 0 END) AS deduped_urls
FROM dedup_seen_urls d
WHERE d.first_seen >= '2026-05-14'
  AND (
        d.url LIKE '%asec.ahnlab.com%'
    OR  d.url LIKE '%dragos.com%'
    OR  d.url LIKE '%welivesecurity%'
    OR  d.url LIKE '%blog.cloudflare%'
    OR  d.url LIKE '%zerodayinitiative%'
  )
GROUP BY source;

-- ----- Q4: バッチ 1 + バッチ 2 統合での channel 分布変化 -----
-- batch 1 マージ前 (~05-13) と現在 (05-14+) の brief / alert 比較
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

-- ----- Q5: 打ち切り条件チェック (連日 triage_error / partial_fetch / 処理時間 15min 超) -----
SELECT
    DATE(started_at)                            AS day,
    MAX(CAST((julianday(finished_at) - julianday(started_at)) * 24 * 60 AS INT)) AS max_minutes,
    SUM(triage_error_count)                     AS triage_errors,
    SUM(partial_fetch_count)                    AS partial_fetches,
    CASE
        WHEN MAX(CAST((julianday(finished_at) - julianday(started_at)) * 24 * 60 AS INT)) > 15
            THEN 'STOP_EXPANSION'
        WHEN SUM(triage_error_count) > 0  THEN 'INVESTIGATE'
        WHEN SUM(partial_fetch_count) > 0 THEN 'INVESTIGATE'
        ELSE 'OK'
    END                                         AS verdict
FROM runs
WHERE triggered_by = 'scheduler'
  AND started_at >= '2026-05-14'
GROUP BY DATE(started_at)
ORDER BY day DESC;
