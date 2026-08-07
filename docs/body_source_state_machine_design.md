# body_source 状態機械化 設計案 (本文抽出の完全性)

> **位置づけ**: [body_extraction_and_entity_integrity_redesign.md](body_extraction_and_entity_integrity_redesign.md)
> が導入した `body_source` 列 (2026-07-27) を **状態機械へ拡張**し、
> [entity_pipeline_inventory.md](entity_pipeline_inventory.md) §6 の本文抽出ギャップ 8・9・10 を根治する。
> 3 並列コード実査 + 本番 PG 実データ検証 (2026-07-29) に基づく。**schema 変更を伴うため実装前に合意を取る**。

---

## 1. 問題 (実データで確認)

本番 PG (`postgres`, HEAD=`640a9e6f`) の実測:

```
stump_total (body_source='feed_summary')            = 3203
  └ うち extraction_failure_reason あり             =    1   ← reason がほぼ書かれていない
body_null_total (body IS NULL)                      = 1640   ← 「第3の空状態」
  └ うち failure_reason LIKE 'degenerate_body%'      =  152
```

3 つの構造的欠陥:

### G8. paywall 薄切りが `full_extract` に化ける (二値では表現不能)
`content_extractor.py:252` の `_looks_like_paywall` は **本文長 < 200字の分岐内でしか呼ばれない**。
paywall のリード文 (見出し+リード+ナビ boilerplate) は容易に 200字を超えるため、
`success=True → body_source='full_extract', reason=None` で確定し、切り株集計からも漏れる。
**success/fail の二値では「HTTP 成功したが本文が薄い」第3状態を表現できない**。

### G9. 「第3の空状態」= body_source NULL が不可視
`DegenerateBodyError` (block_page / body_too_short) は `msg=None` → `persistence.py:255` の
`if msg is not None:` ゲートで `update_article_body` を**呼ばず** → body も body_source も
extraction_failure_reason も書かれず **NULL のまま**。**scraper/watcher の抽出失敗
(`summary_html=""`) は全部ここに落ちる**。`feed_summary`(切り株)にも `'none'`(purge)にも
数えられず、`stump_rate` / `subscription_analytics` の source 別ドリルダウンから構造的に不可視
(実測 1640 件)。

### G10. 恒久失敗 blacklist が空回り (試行回数がない)
`list_articles_needing_refetch` (`repo_articles.py:440-482`) の 2 つの非対称性:
- `body IS NULL` 分岐は **reason を一切見ない** → block_page 等の恒久失敗を毎時無限リトライ。
- `permanent_reasons` に `timeout` / `connection_error` / `http_error_403/429/503` (WAF 系) が
  **含まれない** → UA では解決しない恒常 WAF サイト (FDD 等) を無限リトライ。
- `reprocess.py:75` は再取得失敗時に **DB へ何も書かない** (ログのみ) → 行が変化せず翌サイクルで
  同じ URL が再選定される。
**試行回数の列がないため「N 回失敗したら昇格」ができない** = reason 一致だけでは救えない。

---

## 2. 設計原則

1. **真の指標は「切り株率」ではなく source 単位の body-health**。paywall 薄切り (success=True) は
   切り株率で測れない。ソース単位の直近取得率が backlog 遅れと恒久失敗を切り分ける鍵。
2. **二値 (success/fail) を状態機械に置換**。「取れた/取れない」ではなく、取れなかった**理由の種類**で
   処遇 (再取得する/しない、KPI 上の扱い) を分ける。
3. **全経路で状態と reason を記録**。書込は唯一 seam `update_article_body` に集約したまま、
   現在漏れている経路 (msg=None / reprocess 失敗 / scraper 失敗) を塞ぐ。NULL 状態を根絶する。
4. **恒久判定は reason 一致 + 試行回数の二本立て**。reason で明白な恒久失敗を即除外し、
   曖昧なもの (WAF/timeout) は `refetch_attempts >= N` で昇格させる。
5. **既存の不変条件と seam 集約を維持** (`body≠NULL ⇒ body_source≠NULL`、single-writer、
   `test_article_body_source_seam.py` / `test_schema_parity.py`)。

---

## 3. 状態機械 (`body_source` enum の拡張)

現行 `body_source` は「取得元 (provenance)」と「取れなかった理由」を混在させている。
本設計は body_source を**単一の状態 enum** として完成させる (消費者は既に body_source 単一列で
full/stump を判別しているため、列追加より enum 拡張が整合的)。

| state | 本文の中身 | 再取得 | KPI 分類 | 遷移元 |
|---|---|---|---|---|
| `full_extract` | 全文 (trafilatura) | しない | **full** | 抽出成功 |
| `playwright_extract` | 全文 (JS 突破) | しない | **full** | Playwright 成功 |
| `prefetch` | 全文 (triage 前先行抽出) | しない | **full** | thin-feed 先行抽出 |
| `grok` | tweet 本文 | しない (URL なし) | **full** | Grok 変換 |
| `feed_summary` | RSS 抜粋 (切り株) | **する** (終端化まで) | stump | 抽出失敗+summary あり |
| `paywalled` | paywall のリード文のみ | しない (**終端**・壁は開かない) | thin | paywall 検出 (新: 長さ非依存) |
| `native_short` | 元々短い完結本文 | **しない** (完結=失敗でない) | complete-short | 短いが paywall/block でない |
| `blocked` | なし (WAF/JS/bot 壁) | しない (**終端**・CF 突破非推奨) | failed | block_page / 恒久 4xx/5xx / attempts≥N |
| `pending_refetch` | なし or 切り株 | **する** (transient) | pending | timeout/connection/一時 5xx (attempts<N) |
| `none` | なし (purge 済) | しない | n/a | retention purge |

**廃止**: `unknown` (旧行デフォルト) と **NULL** (第3の空状態) は移行で上記のいずれかへ写像し根絶する。
`scraper` (死んだ enum) は列挙から削除。

### 状態の要点
- **`native_short` は失敗ではない**。シンクタンクの引用 / 動画ページ / 短報は本文が本来短い。
  再取得しても増えない → キューから除外し、KPI では「不完全な取得失敗」ではなく別クラス扱い。
  該当ソースは「RSS 粒度が本文でない」ため**ソース見直し**の対象 (別レイヤ)。
- **`paywalled` / `blocked` は終端**。再取得しない。paywalled のリード文の有用性は残置して判断可能に。
- **`pending_refetch` のみが再取得キューの母集団**。`refetch_attempts >= N` で `blocked` に昇格。

---

## 4. スキーマ変更

### 4.1 新列: `refetch_attempts`
```sql
-- SQLite (repo_base.py _apply_migrations、既存 body_source 追加ブロックの近く)
ALTER TABLE articles ADD COLUMN refetch_attempts INTEGER NOT NULL DEFAULT 0;
-- PG (pg_schema.py 末尾 migration セクション、body_source ALTER 群の直後)
ALTER TABLE articles ADD COLUMN IF NOT EXISTS refetch_attempts INTEGER NOT NULL DEFAULT 0;
```
index は不要 (再取得キューは `body_source='pending_refetch'` で先に絞る限定件数走査)。
必要になったら **ALTER の直後**に CREATE INDEX (index 順序 gotcha 遵守)。

### 4.2 5点セット (既存 body_source 追加パターンを踏襲)
1. SQLite ALTER (`repo_base.py`) 2. PG ALTER (`pg_schema.py`、index はその直後)
3. `ArticleRecord.refetch_attempts: int = 0` (`records.py`)
4. `_row_to_article` に `refetch_attempts=int(row["refetch_attempts"] or 0)` (`row_mappers.py`)
5. `test_schema_parity.py` 実行で SQLite/PG drift なしを確認

### 4.3 body_source は CHECK 制約を付けない (現状踏襲・TEXT)
コード側の代入元 (`_resolve_body` 集約後) と本 doc がSSoT。新 enum 値を消費側フィルタ
(`subscription_analytics.py:208`, `retriever.py:87`, `fill_rate_audit.py:92`) に反映する。

---

## 5. コード変更 (漏れている全経路を塞ぐ)

### Fix 1 — paywall/native_short を長さ非依存で分類 (`content_extractor.py`)
`:252` の `if len(text) < 200` 分岐限定を撤去し、**text が取れたら常に**分類する:
- paywall marker (DOM/キーワード) 検出 → `paywalled`
- 本文が短い (< 閾値) が paywall/block でない → `native_short`
- それ以外 → 成功 (`full_extract`)
`_BLOCK_PAGE_RE` (briefing.py) と `PAYWALL_KEYWORDS` (content_extractor.py) は
**別状態 (blocked / paywalled) として残しつつ、両方とも「常時判定」ポリシーに統一**。
⚠ **over-flagging リスク**: 正常な全文を paywalled/native_short に誤分類しないよう、
本番データで閾値・marker を検証してから有効化 (§7 検証)。

### Fix 2 — 全経路で state + reason を書く (NULL 根絶)
- `persistence.py:255` の `if msg is not None:` ゲート: msg=None (DegenerateBodyError) でも
  `body_source` (`blocked` / `native_short`) と reason を書けるパスを追加。
  または `_process_article` が raise 前に軽量 `update_article_body(source=state, failure_reason=e.reason)`
  を呼んでから re-raise。
- `reprocess.py:75-83` の `still_stump` return 前: 本文は上書きしない設計を保つため
  **新メソッド `record_refetch_failure(article_id, reason, *, increment=True)`** を新設
  (`_ALLOWED_BODY_WRITERS` allowlist に追加)。reason を書き `refetch_attempts += 1`。
  attempts≥N で `body_source='blocked'` へ昇格。
- `scripts/backfill_article_bodies.py:82` の silent skip も同経路で reason/attempts を記録。

### Fix 3 — `_resolve_body` の重複を集約 (DRY)
`briefing.py:299-321` と `reprocess.py:86-90` に複製された分類ロジックを**単一の分類関数**へ集約。
新状態機械のロジックを 1 箇所に持ち、再取得経路が追従漏れしない構造にする。

### Fix 4 — 再取得キューを状態 + 試行回数ベースに (`list_articles_needing_refetch`)
現行の `body IS NULL OR (body_source='feed_summary' AND reason NOT IN permanent)` を置換:
```sql
WHERE body_source IN ('feed_summary','pending_refetch')
  AND refetch_attempts < :N
  AND (feed_title IS NULL OR feed_title NOT IN ('Grok','Ransomware.live'))
```
終端状態 (`blocked`/`native_short`/`paywalled`/full 系/`none`) は自動除外。
reason 文字列一致の恒久リストは補助 (明白な恒久失敗を初回で `blocked` にする) に降格。

### Fix 5 — reprocess の `malware_type` 削除漏れ (独立バグ、同時修正)
`_REPLACE_ENTITY_TYPES` (`reprocess.py:32`) に `"malware_type"` を追加
(delete→再抽出の replace 対象に含め、二重登録を防ぐ)。

---

## 6. source 単位 body-health 指標

**分母不整合を先に直す** (Fix 2 で scraper/watcher 失敗が NULL でなく `blocked`/`pending_refetch`/
`native_short` になれば、自動的に body-health の分母に乗る)。そのうえで:

- **既存の feed 別集計を再利用**: `subscription_analytics.fetch_all_feed_stats` の
  `SUM(CASE WHEN body_source ...)` を新 enum に更新 (full 系 / thin / failed の 3 分類率)。
  新規テーブルは作らない (`articles.feed_url` + `body_source` から導出)。
- **source 単位の急落自動検知** (現状ギャップ): `fill_rate_audit.detect_fill_collapse` は
  category 粒度のみ。これを feed_url 粒度へ一般化するか、`fill_rate_audit` の `METRICS` に
  body-health 行を追加して週次 ops 通知に載せる (どちらを採るかは §8 決定事項)。

---

## 7. 移行 (既存 3203 切り株 + 1640 NULL の再写像)

`repo_base.py:120-130` (SQLite) / `pg_schema.py:404-410` (PG) の一回限り heuristic 分類を拡張:
- `body IS NULL` かつ `failure_reason LIKE 'degenerate_body:block_page%'` → `blocked`
- `body IS NULL` かつ `body_too_short` 系 → `native_short` or `pending_refetch` (要判断)
- `body_source='feed_summary'` かつ reason が既存 permanent → 対応する終端状態へ
- `body_source IN ('unknown', NULL)` → 長さ heuristic で full/native_short/pending へ
⚠ **PG 側は毎プロセス起動で再実行される** (`ensure_pg_schema` は process-level once-guard、
コンテナ再起動毎に走る)。新 UPDATE は `WHERE body_source IS NULL OR body_source='unknown'` 等の
**冪等ガード**を必ず付け、確定済み行を上書きしないこと。

---

## 8. 決定が必要な事項 (実装前の合意ポイント)

- **D1. enum 拡張 vs 別列**: body_source を状態 enum に拡張する (本案) か、provenance と state を
  別列に分離するか。→ **推奨: enum 拡張** (消費者が既に body_source 単一列で判別・変更範囲が小)。
- **D2. `refetch_attempts` 昇格閾値 N**: 何回失敗で `blocked` へ。→ **推奨: N=3** (毎時ジョブなので
  3 時間で終端化)。
- **D3. paywall/native_short 検出の積極度**: over-flagging を避ける閾値・marker。
  → 有効化前に本番データで誤分類率を検証 (§7 検証必須)。段階 flag `BODY_STATE_MACHINE` で切替。
- **D4. source body-health の範囲**: 分母修正 + 既存表示のみ (小) か、source 単位の急落自動通知
  (中) まで作るか。
- **D5. rollback flag**: `BODY_STATE_MACHINE=0` で旧 `_resolve_body` 二値挙動に戻す seam を用意するか。

---

## 9. 段階実装計画

| Phase | 内容 | 挙動変化 | リスク |
|---|---|---|---|
| **B1** | schema (refetch_attempts + 新 enum 値) + 移行 + records/mappers + parity test | なし (additive) | 低 |
| **B2** | 分類の長さ非依存化 + `_resolve_body` 集約 (Fix 1/3) | paywalled/native_short が新規発生 | **中** (over-flag 要検証) |
| **B3** | 全経路 state+reason 記録 + refetch_attempts + キュー書換 (Fix 2/4) | NULL 根絶・無限リトライ停止 | 中 |
| **B4** | source body-health 指標 + 分母修正 + 急落検知 (§6) | 監査系の可視化追加 | 低 |
| **B5** | reprocess malware_type 修正 (Fix 5) | 二重登録解消 | 低 |

各 Phase は独立にデプロイ可能。B2 は本番データ検証を挟んでから有効化する。

---

## 10. 主要変更ファイル早見

| ファイル | 変更 |
|---|---|
| `src/storage/repo_base.py` / `pg_schema.py` | refetch_attempts ALTER + 移行 CASE 拡張 |
| `src/storage/records.py` / `row_mappers.py` | refetch_attempts フィールド |
| `src/storage/repo_articles.py` | `update_article_body` 拡張 / `record_refetch_failure` 新設 / `list_articles_needing_refetch` 書換 |
| `src/tools/content_extractor.py` | 長さ非依存 paywall/native_short 分類 (Fix 1) |
| `src/pipeline/briefing.py` | `_resolve_body` を分類関数へ集約 (Fix 3) |
| `src/pipeline/persistence.py` | msg=None でも state+reason 書込 (Fix 2) |
| `src/pipeline/reprocess.py` | 失敗時 record_refetch_failure + malware_type 追加 (Fix 2/5) |
| `src/ui/services/subscription_analytics.py` / `fill_rate_audit.py` | 新 enum 反映 + body-health (§6) |
| `tests/unit/test_schema_parity.py` / `test_article_body_source_seam.py` | 新列・新 writer の検証 |
