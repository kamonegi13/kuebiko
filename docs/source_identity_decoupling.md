# Source Identity Decoupling — 設計 (可変表示名をキーから外す)

## 1. 問題

`articles` テーブルは source への参照を **`feed_title`(= 購読一覧の可変な表示名)1 本**に依存している。
`feed_title` が「表示名」と「統計の結合キー」を兼ねているため:

- 表示名を改名すると過去記事全行の `feed_title` を移行しないと統計が孤立する
  (2026-06-09 の scraper 6 件改名で実際に articles 144 行を手動移行した)
- scraper が生成タイトルを少し変える / typo るだけで silently に別ソース扱いになりドリフトする
- UI の「✏️ 名称変更」(`rename_source`) は yaml しか変えず DB を移行しないため、**現状は改名で統計が壊れる**

**可変かつユーザ可視の文字列を de-facto 外部キーにしている**のが根本欠陥。

## 2. 監査結果 (同種パターン)

`articles` の列: `id / run_id / article_id / url / feed_title / ...`(安定 source キー無し)。

### 2.1 PRIMARY: `feed_title` をキーにしている箇所 (要修正)

| 箇所 | 用途 |
|---|---|
| `src/ui/services/subscription_analytics.py:181,188,197` | `GROUP BY feed_title` + `JOIN ON r.feed_title=f.feed_title`(購読統計の本体) |
| `src/storage/run_history.py:1205-1212` (`count_editorial_stance_by_feed`) | `GROUP BY feed_title, stance`(論調クロス集計) |
| `src/pir/evaluator.py:112,247` | `feed_counts[feed_title]`(PIR top_feeds) |
| `src/digest/trend_aggregator.py:129` | `sources.add(feed_title)`(distinct source 数) |

### 2.2 SECONDARY: 類似だがリスク低 (今回は対象外、記録のみ)

| パターン | 評価 |
|---|---|
| `article_entities` の actor/malware **canonical 名** (actor_aliases.yaml で正規化) | canonical 改名で entity 行が孤立し得るが、脅威アクター名は実務上ほぼ不変 + 正規化層あり。リスク中低 |
| `pir_spotlight.pir_id` | config(pir.yaml)由来で UI から軽々に改名しない。リスク低 |
| `victim_sector_canonical` 等 | 統制語彙。リスク低 |
| `folder` | articles に非正規化されず source 設定側のみ。改名で article データは壊れない。**設計上正しい(キーにしていない)** |

→ 今回は **PRIMARY (`feed_title`) のみ**を根本修正。SECONDARY は本 doc に記録し、必要時に同方式で対処。

## 3. 設計

### 3.1 安定キー = 既存の `feed_url`(取り込み側ゼロ変更)

**新フィールドは作らない。** `Article` モデルには既に **必須フィールド `feed_url`**(article_model.py:27)が
あり、全 source が設定済み・かつ `ManagedSource.url`(レジストリ)と全 transport で一致する:

| transport | Article.feed_url | ManagedSource.url | 安定性 |
|---|---|---|---|
| RSS/Atom | `feed.url` | feeds.yaml url | 安定 |
| html_scraper | `listing_url` | scrapers.yaml listing_url | 安定 |
| sitemap watcher | `sitemap_urls[0]` | watchers.yaml sitemap[0] | 安定 |
| grok | `"https://grok.com/"`(固定) | (レジストリ外) | 安定 |

→ **`feed_url` を「永続化するだけ」で安定 source キーになる。** 取り込み経路(7+ の Article 構築点)は
**一切変更しない**(競合・取りこぼしリスク最小)。`articles.feed_url` ↔ `ManagedSource.url` で join し、
表示名(`feed_title`)はレジストリから都度解決 → 表示専用に降格。

> なぜ feed_id 規約(`scraper:name`)でなく URL か: feed_id 方式は全構築点に key を流す必要があり
> 取り込み改変が増える。feed_url は既に全構築点で正しく設定済みのため**永続化のみで済む**(本タスクの
> 「取り込みを壊さない」要件に最適)。URL 編集時のドリフトは稀(通常は delete+re-add)で許容。

### 3.2 取り込み経路 (source → article → DB)

- 各 source の Article 構築は**変更なし**(feed_url は既に設定済み)。
- `ArticleRecord` に `feed_url: str | None` を追加、`add_article` の INSERT に列追加。
- `main.py` の ArticleRecord 構築で `feed_url=article.feed_url` を渡す(1 箇所)。

### 3.3 スキーマ (非破壊・PG index 順序ルール遵守)

`articles` に `feed_url TEXT`(nullable)を追加:
- PG: `pg_schema.py` の CREATE TABLE 本体 + **末尾 ALTER ADD COLUMN IF NOT EXISTS** + その後に index
  (`socio_political_intent` の先例どおり。memory `pg_schema_index_ordering` 厳守)。
- SQLite: `_apply_migrations` に冪等 ALTER + index。
`feed_title` は**残す**(表示 fallback + 移行期の安全弁)。

### 3.4 過去記事の backfill

歴史記事は `feed_title` + `url` のみ(feed_url 未永続)。`feed_url` を次の優先で付与:
1. 現レジストリの `{feed_title → url}` で逆引き(ドリフト無いソースはこれで充足)
2. 1 で未解決かつ RSS は記事 `url` のドメイン一致で feeds の url に寄せる(best-effort)
3. それでも未解決 → `feed_url = NULL`(クエリ側で `feed_title` に fallback)

backfill script `scripts/backfill_article_feed_url.py`(冪等・NULL 行のみ対象・再実行可)。

### 3.5 クエリ移行 (fallback 安全)

PRIMARY 4 箇所を `GROUP BY COALESCE(NULLIF(feed_url,''), feed_title)` に変更し、
表示名は **feed_url → レジストリ url の現 feed_title** で解決(未解決は feed_title そのまま)。
これで feed_url 未充足の歴史記事も失わない。

### 3.6 改名の単純化 (狙いの成果)

`rename_source` は **yaml の feed_title を変えるだけ**で完了(DB 移行不要)。
`articles.feed_url` が不変なので統計は自動で追従。「✏️ 名称変更」が構造的に安全になる。

## 4. 段階ロールアウト (各段で停止・検証可能)

1. **Stage 1 (スキーマ+モデル+取り込み)**: `feed_url` 列追加 + Article/ArticleRecord/add_article 配線
   + 各 source が値を設定。**新記事のみ feed_url が入る**(既存は NULL、クエリは feed_title fallback で不変動作)。
2. **Stage 2 (backfill)**: 既存記事に feed_url 付与。検証(被覆率・未解決件数)。
3. **Stage 3 (クエリ移行)**: PRIMARY 4 箇所を feed_url 基準へ。表示名はレジストリ解決。
4. **Stage 4 (rename 単純化)**: `rename_source` から DB 移行依存を外す(yaml のみ)。回帰テスト。

各 stage は前段と互換(fallback 維持)。問題があれば stage 単位で停止。

## 4.5 実装状況 (2026-06-09)

- **Stage 1** ✅ (`38e2f72`): feed_url 列 + 永続化配線。本番デプロイ・列追加確認済。
- **Stage 2** ✅ (`d2d0092`): backfill 実行済、94.3% 被覆 (残 423 は drift/grok/削除済で fallback)。
- **Stage 3** 部分完了:
  - **#1 subscription_analytics (購読統計、ユーザ直面)** ✅: feed_url 結合へ移行。本番 parity 検証で
    全 106 feed_url が 1:1 で feed_title 対応 = 完全一致 (データ損失なし)。frontend は s.url 結合に。
  - **#2 editorial crosstab / #3 pir top_feeds / #4 digest trend** ⏳ 残: 同パターン (feed_title GROUP BY)
    だが内部分析向けの集計で優先度低。移行は GROUP BY を `COALESCE(NULLIF(feed_url,''),feed_title)` に変え、
    表示名はレジストリ url→title で解決する (同方式)。現状は feed_title のままで、改名時にその集計のみ割れ得る
    (= 改名前の挙動と同等。per-source 統計 #1 は安全)。
- **Stage 4** ✅ (検証): rename_source は元々 yaml のみ (DB 移行は持たない)。Stage 3 #1 により改名は
  統計を割らない。test_subscription_analytics で「異 feed_title・同 feed_url → 1 集約」を検証済。

## 5. テスト方針

- unit: source ごとに `feed_url` が規約どおり入る / add_article 往復 / backfill の逆引き・fallback。
- query: feed_url 基準集計が feed_title 基準と同値(移行前後で件数一致を検証)。
- e2e: 改名後に統計が維持される(DB 移行なしで)。
