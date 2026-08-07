# Source / Pipeline / Schedule アーキテクチャ全体見直し

> 2026-05-29、機能継ぎ足しで累積した設計不整合の全体調査と再設計。
> 4 並列調査エージェント (Schedule / Source model / Subscriptions+dead code / 全体俯瞰)
> の結果を統合。すべて実コード根拠 (file:line)。

## 0. 結論 (一文)

**「ソース (何を集めるか)」と「パイプライン (いつ・どう処理するか)」という 2 概念が分離
されないまま、RSS は『1 pipeline が feeds.yaml 全件を読む』、scraper/watcher は
『pipeline 内に member を明示列挙する』という非対称な 2 モデルで実装され、その上に
機能を継ぎ足した**ことが、観測される全不整合の単一の根本原因。

## 1. 根本原因の構造

```
RSS:           direct-rss-fetch pipeline ──reads──> feeds.yaml (enabled が唯一のゲート)
scraper/watcher: web-scraper-watchers pipeline ──lists──> pipelines.yaml cluster member.enabled
                                                              └─実体定義──> scrapers/watchers.yaml (enabled)
```

- RSS の source↔pipeline は「registry 参照」(feeds.yaml が source registry、pipeline は全件読む)
- scraper/watcher の source↔pipeline は「明示メンバーシップ」(pipelines.yaml に名前列挙 + 実体は別 yaml)

→ 同じ「ソース 1 件の on/off」が、RSS は **1 file 1 field**、scraper/watcher は
**2 file 2 field の AND ゲート**。この非対称が下記すべての症状を生む。

## 2. 観測された不整合 (調査で確定、影響度順)

### CRITICAL
1. **wizard 追加 sitemap watcher は永久に実行されない** (`_source_writer.py:90-127`)。
   html_scraper は登録時に cluster へ append するが (`:165-182`)、watcher は watchers.yaml に
   書くだけで cluster に入らない。実行ゲートは cluster membership (`source_router.py:324`) なので
   silent dead source 化。**ユーザが今テストしている「ソース追加」が sitemap で機能しない。**
2. **folder regression (本日 commit 0860009 で混入 → 55eda54 で修正済)**。
   ScraperDef/WatcherDef が `extra=forbid` なのに folder を書き込み、registry load が
   ValidationError → 当該ソース実行停止。**修正済。**

### HIGH
3. **enabled の二重 SSoT + 状態分裂**。scraper/watcher の enabled が「entry.enabled (yaml)」と
   「cluster member.enabled (pipelines.yaml)」に分裂。Schedule ページの toggle は cluster member
   のみ書く (`pipelines_editor.py:206`)、Subscriptions は両方同期 (`_source_manager.py:211-224`)。
   → Schedule で disable → Subscriptions で enable が黙って上書き。表示も別ソースを読むため矛盾しうる
   (Schedule=pipelines.yaml、Subscriptions=scrapers/watchers.yaml)。
4. **同一操作の UI 二重化**。scraper/watcher の enable/disable が Schedule と Subscriptions の
   両方に存在し、別 codepath・別編集範囲。ユーザの問題提起そのもの。
5. **2 段 AND ゲートによる「enabled なのに動かない」**。enabled=False で html_scraper を登録すると
   cluster に入らず (`_source_writer.py:168`)、後で UI で enable しても cluster に追加されない
   (`_source_manager.py:196`) → 永久に動かない second-order バグ。

### MEDIUM
6. **stats join key が feed_title (人間可読文字列)**。`subscription_analytics.py:181` が
   `articles.feed_title` で GROUP BY。lac_watch は `feed_title=f"LAC Watch ({category})"` と
   動的 (`lac_watch.py:271`) なため、list_sources の固定 title と不一致 → stats 全件 undefined →
   quality_score 0 / no-data 誤判定。安定 ID でなく表示名を join key にした脆さ。
7. **SourceType enum が ingest と非 ingest を混在** (`config_loader.py:38-55`)。
   rss/grok/scraper (取得) と digest/synthesis/spotlight/taxonomy/pir_daily_focus (DB から生成、
   取得なし) が同列。synthesis pipeline に `extract_method: trafilatura` 等の無意味な飾りが付く
   (`pipelines.yaml:147-229`)。
8. **概念の命名ドリフト**: source / feed / subscription / watcher / scraper / pipeline が層ごとに割れる。
   feed_id prefix が transport 名と不一致 (`html_scraper`→`scraper:`、`sitemap`→`watcher:`)。
9. **channel routing が 3 系統**: `pipelines.yaml channel_routing.importance_map` + `cti/router.py`
   R1-R8 ハードコード + `pir.yaml target_channel` (R0)。どれが最終決定か追うのに 3 file 横断。

### LOW (dead/stale、要 cleanup)
- dead endpoint: `POST /api/v1/feeds/bulk` `/feeds/folder` (`bulk_feeds.py:110,157`、sources/* に移行済)。
  ただし `/feeds/discovery` は現役なので file 全体は残す。
- orphan client: `feeds.ts` の probe/preview/validate/add/bulk/setFolder (呼び元ゼロ)。
- orphan func: `subscription_analytics.make_inoreader_deep_link` / `fetch_first_seen_for_all_feeds`。
- dead stub: `cti/router.py:293` `load_routing_rules()` (routing_rules.yaml 不在)、
  `scheduler.attach_watchers` (`scheduler.py:88` 以降、lifespan から未呼出)。
- 削除済 /auto_detect を叩く `scripts/verify_add_source_*.py` (実行すると 404)。
- stale コメント: `feeds.ts:2` (削除済 feeds.py 参照)、`sources_v2.ts:2` (消滅した sources.ts 参照)、
  `SubscriptionsPage.tsx:11` (削除済 3 wizard 参照)。
- `except sqlite3.Error` のみ (`subscription_analytics.py:208`) → PG 例外を捕捉せず /subscriptions 500 リスク。
- CLAUDE.md §5 ディレクトリ表に `watchers/digest/taxonomy/synthesis/pir/spotlight` 未記載、
  §6 フェーズ表 (Phase 2.6a が「現在地」) が実態と大乖離、§11 「HTMX+Jinja」記述は React SPA 移行済で陳腐化。
- `main.py` 2453 行 / `run_pipeline` God 関数 / `pages.py` 747 行 catch-all router。

## 3. 目標設計 (target model)

### 概念境界の再定義
- **Source** = 取得器のみ。`transport: rss | sitemap | html_scraper` を 1 属性に持つ単一抽象。
  **enabled はソース定義だけが SSoT**。feed/watcher/scraper/subscription の語を **source に統一**。
- **Pipeline** = `kind: ingest | digest | synthesis`。
  - `ingest` のみ source を参照。**cluster の明示メンバー列挙を廃止し、enabled な source を自動収集**。
  - `digest`/`synthesis` は source を持たず DB クエリ条件を持つ。SourceType enum から非取得系を排除。
- **Schedule** = pipeline の属性 (cron/interval)。pause/resume も yaml に書き戻し runtime 推測を廃止。
- **Channel routing** = `cti/router.py` に一本化 (PIR R0 override は維持、importance_map と
  routing_rules.yaml stub は撤去)。

### linchpin: web_scraper_cluster の auto-collect 化
現在の「pipelines.yaml に scraper/watcher を名前列挙」を廃し、cluster source が
**scrapers.yaml + watchers.yaml の enabled な全 entry を起動時に自動収集**する設計に変える。
これ 1 つで以下が同時に解消する:
- enabled 二重 SSoT (#3) → entry.enabled のみが真実に
- UI 二重化 (#4) → Schedule の per-member toggle は不要に (entry.enabled を編集する Subscriptions に一本化)
- sitemap watcher never-runs (#1) → enabled な watcher は自動で収集される
- 2 段 AND ゲート (#5) → ゲートが 1 段に

## 4. 段階移行計画 (behavior-preserving、PIR 移行と同じ手法)

| Phase | 内容 | リスク | 効果 |
|---|---|---|---|
| **P0 (即時)** | folder regression 修正 (済) + sitemap watcher を cluster へ append する最小 bug 修正 | 低 | CRITICAL バグ 2 件解消 |
| **P1** | web_scraper_cluster auto-collect 化。pipelines.yaml の member 列挙を「enabled な全 scraper/watcher」に置換。enabled SSoT を entry yaml に一本化。Schedule の per-member toggle を Subscriptions へ委譲 (or 同じ entry を編集) | 中 | #3,#4,#5 構造解消 |
| **P2** | stats join を feed_title → 安定 source 識別子に。articles に source_id 付与 (migration) | 中 | #6 解消、LAC 等の stats 復活 |
| **P3** | Pipeline.kind 導入。SourceType から非 ingest 排除。synthesis pipeline の死フィールド除去 | 中 | #7 解消、概念明確化 |
| **P4 (任意)** | 3 yaml → sources.yaml discriminated union 統合 (envelope + transport 固有 fetch payload)。feed_id prefix を transport 名に揃える | 高 | #8 schema 統一。段階移行必須 |
| **P5** | dead code 一掃 + 命名 source 統一 + CLAUDE.md 同期 | 低 | LOW 群解消、保守性 |
| **P6 (任意)** | main.py 分割 (pipeline kind ごと handler を src/pipelines/) + pages.py router 分割 | 中 | God 関数解消 |

## 5. 推奨

- **P0 は即実施** (実バグ、小)。
- **P1 (auto-collect) が最大の ROI** — ユーザ提起の「Schedule/Subscriptions 重複」を構造的に消す核心。
- P2/P3 は観察しつつ。P4 (schema 統一) は大きいので P1-P3 の効果を見てから判断。
- P5 (dead code/命名/docs) は随時。

非対称を生んだ歴史的経緯 (RSS first → watcher/scraper 後付け → 統合 wizard で入力だけ統合) は
妥当だったが、**管理・実行モデルが取り残された**。P1 がその是正の中心。

## 7. 実施ログ (2026-05-30)

- **P0 済** (55eda54): folder regression 修正 (ScraperDef/WatcherDef extra=forbid)。
- **P1 済** (637d985): web_scraper_cluster auto-collect 化。active 16 件完全一致で behavior-preserving。
- **P5a 済** (712d766): dead custom scraper project-zero/lac-watch 撤去 (RSS 現役、.py/script/health/cluster 全除去)。
  nicter/38north は正当な bespoke として維持。
- **P5b 済** (6e199d9): dead endpoint/client/stub 一掃 (bulk_feeds bulk/folder、feeds.ts orphan、
  inoreader/routing stub、stale コメント)。
- **P5c 済**: CLAUDE.md 現状サマリ追加 (React SPA / PG / source 統合管理)。
- **scheduler.py dead watcher path は意図的に保留**: WATCHER_JOB_PREFIX / ScheduledWatcher /
  attach_watchers / _register_watcher_jobs は scheduler.py 内のみ参照で削除安全だが、daily briefing
  実行基盤 (core) への cosmetic 編集はリスク/便益が悪いため見送り。機能影響ゼロ (attach_watchers 未呼出)。
- **P5a の副次効果**: lac_watch.py (動的 feed_title) 撤去で P2 の主要バグ (動的 title による stats 消失)
  が解消。残る P2 は「feed_title は表示文字列で安定 ID でない」堅牢性改善 (重要度低下)。

## 8. P3 結論 (2026-05-30) — マージは見送り、stagger 採用

着手前検証で **2 ingest pipeline のマージは便益薄・リスク高**と判明:
- dedup 2 層 (URL: _filter_duplicates / 意味的: _filter_semantic_duplicates) は
  **永続ストア (DB の過去投稿、48h/168h 窓) 参照**。pipeline はサブプロセス実行なので、
  別 run でも後発は先発の投稿済記事に対して dedup される = **cross-source dedup は既に機能**。
- マージで改善するのは「両 :00 ジョブ並行実行の in-cycle race」のみ (稀、次サイクルで吸収)。
- マージのリスク: timeout 結合 (RSS 速 + Playwright 遅の直列化で RSS ごと失敗)、障害結合、
  max_articles/triage_max_keep 統合での挙動変化、composite source 新設、観測性低下。
→ **採用: stagger** (web-scraper-watchers を rss の 5 分後に offset)。
  `PipelineSchedule.interval_offset_minutes` (default 0、既存無影響) を追加し、
  scheduler が clock-aligned 起点 + offset で IntervalTrigger を構築。in-cycle race を
  構造リスクゼロで解消。**Pipeline.kind と synthesis 死フィールド除去は見送り**:
  前者は God 関数 dispatch 改変リスクで P6 と一緒が安全、後者は cosmetic + 非対称リスク。

## 9. P6 / P4 結論 (2026-05-30) — 見送り

同じ risk/benefit 基準 (便益薄・リスク高なら止める) で判断:
- **P6 (main.py 2453 行分割)**: 純粋な保守性リファクタで**機能的便益ゼロ**、core
  オーケストレータ全体に触れる高リスク。動作中の mission-critical ツールでは ROI 悪。→ 見送り。
- **P4 (3 yaml → sources.yaml 統合)**: cosmetic/DRY で機能的便益ゼロ、source 全設定 +
  feed_id 体系 + stats の最侵襲 migration。→ 見送り。

機能バグを起こしていた累積不整合 (sitemap-never-runs / 二重 SSoT / Schedule-Subscriptions
重複 / folder regression / dead scraper / PG 500) は P0-P3 で全解消済み。P6/P4 は
「保守性の痛みが実際に顕在化したら着手」とし、今は着手しない。

### 本見直しセッションの最終成果 (2026-05-30)
- 高価値 (バグ修正 + 設計整合): P0 / P1 (auto-collect) / P5a-c / P2-lite / P3 (stagger) = 実施済。
- 検証で却下/見送り: ingest pipeline マージ (dedup は永続窓で既に機能)、P2 migration
  (P5a で主因消滅)、scheduler dead code 除去 (core cosmetic)、synthesis 死フィールド除去
  (cosmetic + 非対称リスク)、P6 / P4 (機能便益ゼロの大規模リファクタ)。
