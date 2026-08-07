# 本文抽出とエンティティ完全性の抜本再設計

**技術 SSoT。2026-07-27 起票。** 利用者が「イラン系ハッカーが米重要インフラの PLC を悪用」記事
(`rss:https://gbhackers.com/?p=193533`) の脅威アクター表示異常 (`us_nsa` / `us_cyber_command`)
を発端に、本文抽出とエンティティ抽出という**ツールの根幹**の欠陥を指摘したことに対する設計。

対処療法でなく構造欠陥の解消を目的とする。実装は本書をもって自己完結させる (実装者に本書 +
参照ファイルのみで着手可能とする)。

---

## 0. 問題連鎖 (実データ・実再現で確定)

対象記事は氷山の一角。以下は 1 本の因果連鎖である。

1. **全文取得が 403 で失敗**。コンテナ内で実 `ContentExtractor` を走らせ `http_error_403` を再現。
   切り分けの結果、[src/tools/content_extractor.py:36-40](../src/tools/content_extractor.py#L36-L40)
   にハードコードされた `Chrome/120` UA **そのもの**が WAF のボット署名になっていた
   (同一 IP・同一コンテナから `Chrome/126` UA なら 200 で全文 39 万字取得可能)。Playwright
   fallback も同 UA 系で 403。
2. **失敗が無音で feed 抜粋に fallback**。[src/pipeline/briefing.py:271-282](../src/pipeline/briefing.py#L271-L282)
   の `_resolve_body` が INFO ログ 1 行だけ残して `article.summary_html` (WordPress boilerplate
   込み) を body に採用、`status="summarized"` で正常完走。
3. **失敗はどの指標にも現れない**。ダッシュボードの「抽出失敗率」は `status='extract_failed'`
   だけを数える ([src/storage/repo_knowledge.py:636-657])。fallback は status を汚さないため
   **可視失敗率 1.4% に対し実切り株率 ~12%**。
4. **切り株の上で全下流が実行される**。エンティティ抽出・要約・主題判定・IOC 抽出・アクター
   収穫がすべて 674 字の抜粋を入力に走り、元記事の真の主体 **CyberAv3ngers / UNC5691 /
   Shahid Kaveh Group**、OT 系 ATT&CK (T0883/T0885/T0914)、IOC テーブルを**全喪失**。
   さらに summarizer がアドバイザリ番号を **AA26-097A → AA26-027A に改変** (幻覚) しており、
   痩せた入力が事実精度まで劣化させた。
5. **残った抜粋中の "NSA" / "U.S. Cyber Command" が辞書照合でヒット**。これは 2026-06-23
   の「同盟アクター」追加 (commit `6dff1006`、国家情勢ボード用) で入った `kind=organization`
   エントリで、alias "NSA"/"Cyber Command" が本文照合 ([briefing.py:174](../src/pipeline/briefing.py#L174))
   に一致し **mention (言及) actor entity** として書かれた。主題判定層は正しく棄権
   (`subject_actor_source='none'`) しているが、**記事詳細 API が subject を返さず**、UI は
   mention を「脅威アクター」ラベルで無差別表示した ([ArticleDetailPage.tsx:449-480])。

### コーパス規模の定量 (2026-07-27 実測)

| 指標 | 値 |
|---|---|
| GBHackers 導入以来の切り株率 | **953/953 = 100%** (median body 588 字、3 ヶ月間一度も成功なし) |
| 直近 30 日 RSS で本文 <800 字 | 1,201 件 (可視 extract_failed はわずか 1 件) |
| boilerplate マーカー付き body (全期間) | 1,732 件 |
| 本文長と抽出量の相関 (30d) | <800字: IOC 0.04/件・TTP 1.11・CVE 0.17 / 2500字+: IOC 0.93・TTP 5.30・CVE 2.27 |
| high 重要度が切り株の上で分析される率 (30d) | 7.6% (205 件) |
| 西側報告機関のみが actor の記事 | 25 件 (mention 混入は計 44 件、us_nsa 31 / mossad 6 / cyber_command 5 …) |
| **アクター発見経路の死** | CyberAv3ngers は 10 記事に登場し **entity 化 0 件**、`llm_primary_actor_raw` 充足 14/3947 (0.35%) |

---

## 1. 本質的問題 (3 つの構造欠陥)

### P1: 「本文の完全性」がシステムの不変条件として扱われていない
全下流が body を信頼して食うのに、body が全文である保証・記録・監視・修復のどれも無い。
(i) 取得失敗は INFO ログのみ、(ii) fallback は DB に痕跡を残さない (`body_source` 列が無い)、
(iii) 指標は別物を数える、(iv) 一度切り株で保存されると seen 登録で恒久 (backfill は
`body IS NULL` のみ対象)。加えて fetch 戦略が「静的ハードコード UA で 1 回試す」設計で、
動的に変わる WAF に構造的に負ける。有機的結合監査 (2026-07-12) の病因「供給網の無監視」の
再演 — fill-rate 監査に body 完全性の項目が無い。

### P2: 「言及 (mention)」と「主題 (subject)」の混同が表示・集計層に残存
格納は分離済み (mention=`article_entities`、subject=`articles.subject_actor_ids` 列) なのに、
**表示層と集計消費層にその区別が展開されていない**。詳細 API が subject を露出せず、UI は
mention を「脅威アクター」と表示し、地図 flow・国家情勢ボード攻撃元・actor-nation 集計・
脅威スナップショット・チャットも mention を主体として数える。確定原則「言及≠主題」
(2026-07-17、[[subject-actor-attribution-2026-07-17]]) の適用が判定層で止まり、表示・消費層に
未達。

### P3: 辞書成長ループの飢餓 — 新規ブランド名アクターの発見経路が全滅
新顔アクターが辞書に入るための自動発見経路が構造的にすべて閉じている:

| 経路 | 状態 | 理由 |
|---|---|---|
| summarizer `primary_actor` | **死 (0.35%)** | 過負荷 summarizer の末尾フィールド枯死 (既知の病、統合分類器へ移設済のはず) |
| judgment_classifier `subject_actor_id` | **辞書外を出せない** | 候補ゲート (`valid_ids`、[judgment_classifier.py:181-183]) で辞書 id しか emit できない |
| harvest `llm_primary` 信号 | **死** | 入力 `routing_flags.primary_actor_id` = 上の gated 辞書 id → `knows_name=True` で除外 ([actor_candidates.py:94-95]) |
| harvest `vendor_designation` 信号 | 生きているが限定的 | `UNC\d+`/`Storm-\d+`/`TA\d+` 形式のみ。`CyberAv3ngers` は非マッチ |
| find_all mention | 辞書内のみ | 定義上、未収録名は拾えない |

**帰結: `CyberAv3ngers` のようなブランド名アクターは、全文があっても発見される経路が 1 つも
無い**。P1 が P3 を悪化させる (本文が無ければ収穫も再帰属もできない)。D1 で保存している生値
`llm_primary_actor_raw` が収穫に接続されておらず、かつそれ自体が死んでいる。

---

## 2. P1 — 本文完全性の一級市民化 (最優先・根幹)

### 2.1 body の由来と失敗理由を永続化 (監査可能性の回復 = 全ての前提)

`articles` に 2 列追加 (PG schema + SQLite schema 両方 — [[pg-schema-index-ordering]] に従い
CREATE INDEX は末尾 ALTER ADD COLUMN の後):

- `body_source TEXT` — enum: `full_extract` / `playwright_extract` / `prefetch` / `scraper` /
  `grok` / `feed_summary` / `none`。**唯一の SSoT**。
- `extraction_failure_reason TEXT NULL` — fallback/失敗時のみ (`http_403` / `http_429` /
  `timeout` / `content_too_short` / `paywall_suspected` / `extraction_failed`)。成功時 NULL。

書き込み点: [briefing.py:_resolve_body](../src/pipeline/briefing.py#L271-L282) を
`(body, source, failure_reason)` の 3-tuple を返すよう変更し、`ExtractionResult` の
`extraction_method` / `failure_reason` から決定する。fallback 分岐 (現 275 行) では
`source='feed_summary'` + `failure_reason=extraction.failure_reason` を必ず立てる。
[persistence.py:update_article_body](../src/storage/repo_articles.py#L97-L112) を拡張して
3 値を書く。

**不変条件 (guard test 必須)**: `body IS NOT NULL AND body <> ''` ⇒ `body_source IS NOT NULL`。
書き込み seam を 1 点に集約し、迂回 writer は CI で落とす (R1 で確立した
`test_article_write_seam.py` と同型)。

### 2.2 取得戦略の再設計 (静的 1 回 → config 化 + 多段 + retry)

UA ハードコードを廃し、取得を「戦略」に格上げする。

- `content_extractor.py` の UA 定数を `config` (または `.env`) 由来にする。既定は**現行の
  Chrome 安定版**とし、CLAUDE.md の「モデル変更は同時更新」に倣い**UA 更新も文書化された
  保守項目**にする (UA の陳腐化は WAF ブロックの直接原因なので、fill-rate 監査の切り株率
  急落がトリガーになる)。
- **403/429/503 での UA ローテ retry**: 1 回目失敗時に代替 UA 2-3 種で再試行するミニ・
  ラダーを `extract()` 内に入れる (現状 `202/403/429/503` は Playwright fallback のみ)。
  実測で `Chrome/126` は通り `Chrome/120` は落ちたため、UA だけで大半が救済される。
- `Accept` / `Accept-Language` ヘッダも現実的ブラウザ相当を付与。
- SSRF ガード ([content_extractor.py:169-172]) は不変。**robots.txt の扱いは §10 保留のまま**
  (再提案不可の判断に抵触しない)。

### 2.3 失敗の可視化 (指標を実態に合わせる)

- ダッシュボードの「抽出失敗率」を `status='extract_failed'` に加え
  `body_source='feed_summary'` を**分けて**集計 (「全文取得失敗率」と「切り株率」の 2 系列)。
  API [runs.py:extract_failure_rate](../src/ui/api/runs.py#L155) と
  [repo_knowledge.py:636-657](../src/storage/repo_knowledge.py#L636-L657) を拡張。
- **fill_rate_audit に body 完全性メトリクスを登録** ([src/ui/services/fill_rate_audit.py] の
  `METRICS` に 1 行、CLAUDE.md §7 の 3 点セット規約):
  ```python
  FillMetric("full_body", "本文全文取得",
             "a.body_source IN ('full_extract','playwright_extract','prefetch','scraper')",
             _CYBER)
  ```
  per-source の median 本文長と切り株率の週次監査を追加し、**「生まれつき 0%」(GBHackers 型)
  と「急落」の両方を検出**する — MITRE 同期の教訓「上流が壊す供給は下流で消しても取込 filter
  を同時に入れなければ再発する」([[mitre-sync-generic-alias-regression-2026-07-21]]) と同じ、
  供給網の常設監視。

### 2.4 非同期再取得キュー + 下流再処理 (切り株の恒久化を断つ)

body_ja バックログ翻訳ジョブ ([[session-handoff-2026-07-24]]) と同型の毎時ジョブを新設。

- **対象選定**: `body_source='feed_summary'` AND `extraction_failure_reason IN (retriable set)`
  AND body が現存 (retention 90d 内)。重要度・新しさ優先。**NULL body 用の
  `backfill_article_bodies.py` と統合**し「body が不十分 (NULL または feed_summary)」を単一
  条件にする ([repo_articles.py:list_articles_missing_body](../src/storage/repo_articles.py#L114-L132)
  の `WHERE body IS NULL` を拡張)。
- **再取得は保存 URL 直 fetch** で RSS 収集窓を経由しない (seen 登録で再収集されない問題を
  迂回)。2.2 の新戦略で取得。
- **成功時 = body 差し替え + 下流の silent 再処理**。ここが肝。以下を `reprocess_enrichment(
  article_id, new_body)` として抽出し、**Discord 再投稿はしない** (alert は既に発火済み):
  - IOC 再抽出 ([briefing.py:143])・アクター find_all mention 再生成・judgment 再分類
    (subject/intent/diamond/article_type/event_date)・harvest 再実行 (§3)・
    actor_provisional 再生成。
  - **要約も再生成**して stored record を差し替え (幻覚 advisory 番号の是正など)。ただし
    posted_channel/discord_message_id は不変 (再通知しない = 「状況認識は web、警告は Discord」
    原則 [[notification-push-pull-redesign]] と整合)。
  - body_ja を無効化して再訳キューへ・embedding を再計算 (dedup 用、body 変化)。
  - 既存 entity 行の**置換規律**: 再処理は当該 article の mention/ioc/ttp entity を
    delete + re-insert (add-only でなく replace)。subject は determine_subject_actors を
    再実行して `subject_actor_ids` を upsert。**過去の誤 mention (NSA 等) が残らないよう
    replace にする** — 承認時再帰属が add-only だったための残存問題 ([[session-handoff-2026-07-27]]
    の教訓) をここで繰り返さない。
- **監査ログ**: 再取得成功/失敗を job_run_log に記録し、「切り株→全文」への遷移件数を可視化。

### 2.5 UI の正直化

- 「本文 (抽出済 N 字)」の「抽出済」を body_source 依存にする
  ([ArticleDetailPage.tsx:146])。`feed_summary` のとき「**フィード抜粋のみ (全文未取得)**」、
  失敗理由があれば併記 (例「全文取得失敗: 403」)。ラベルは vocab SSoT
  ([[vocabulary-label-architecture]]) 経由で backend 配信。
- 再取得待ちの記事にはその旨の控えめな注記 (地図の暗域 doctrine と同じ「観測の限界を明示」)。

### 2.6 遡及修復

- 既存の切り株 (30日で ~1,200 件、マーカー付き全期間 1,732 件) を再取得キューに投入。
  GBHackers 953 件は UA 修正だけでほぼ全件救済見込み。
- retention purge 済 (body NULL・90d 超) は summary も残っていないため再取得不能 →
  「当時の観測記録」として受容 (§2.4 の二段階と整合)。

---

## 3. アクター抽出の抜本強化 (利用者 Q1「コレで十分か」への回答 = **否**)

§1 P3 のとおり、現状は新規ブランド名アクターの発見経路が全滅している。B+C の「表示分離+
harvest 再接続」だけでは不十分で、**発見信号そのものを作り直す**必要がある。

### 3.1 判断分類器に UNGATED discovery フィールドを追加 (本丸)

判断分類器 ([src/cti/judgment_classifier.py]) は全文を読む正規の actor 推論 LLM。ここに
**候補ゲートを通さない生フィールド** `named_primary_actor: str` を追加する
(確定 `subject_actor_id` とは別)。

- `subject_actor_id` は従来どおり候補ゲート (辞書内のみ) を維持 — **確定帰属の辞書ゲートは
  再提案不可の確定原則** ([[subject-actor-attribution-2026-07-17]])。汚染防止のため不変。
- `named_primary_actor` は**辞書内外を問わず、記事が主題として名指しする攻撃主体名をそのまま
  返す** (無ければ空)。プロンプトで「防御側/報告機関 (CISA/FBI/NSA 等)・被害組織・ベンダは
  除外し、攻撃を実行した主体名のみ」と明示 (NSA 混入の構造的抑止をプロンプト層でも二重化)。
- 出力を `articles.llm_primary_actor_raw` に書く (D1 列を蘇生 — 現在 summarizer 由来で 0.35%
  死んでいるのを、生きている judgment_classifier 由来に置換)。fill_rate_audit の
  `llm_primary_actor` メトリクスがそのまま復活の監視に使える。

### 3.2 harvest を生値に再接続

[briefing.py:203](../src/pipeline/briefing.py#L203) の
`_pid = routing_flags.get("primary_actor_id")` (= gated 辞書 id、死んでいる) を、
**`named_primary_actor` (ungated)** に差し替える。これで harvest の `llm_primary` 信号が
初めて辞書外の新顔を受け取れる:
```
CyberAv3ngers (named_primary_actor, 辞書外)
  → harvest_candidates が knows_name=False で候補化
  → actor_provisional entity として即可視 (「暫定アクター(未承認)」)
  → 週次 mitre-sync 相乗りで actor_update_proposals に提案
  → 人承認 → 辞書 → 次の再帰属で subject 化
```
`vendor_designation` 信号 (UNC5691 等) は §2 で全文が入れば自然に拾える。

### 3.3 これで十分な理由 (と、あえて越えない一線)

- **十分**: (i) 全文取得 (§2) で名前が本文に存在し、(ii) ungated LLM 信号 (§3.1) が主体名を
  拾い、(iii) provisional として即可視化され、(iv) 人承認 → 辞書 → 再帰属で subject 化する。
  4 段が揃って初めて `CyberAv3ngers` 型が拾える。
- **越えない一線 (再提案不可の境界)**: 確定 subject 帰属の**辞書ゲート + 人承認は維持**。
  LLM に確定 subject を自由記入させない — 一般語衝突事故 (Nova/Payload、[[session-handoff-2026-07-27]])
  と辞書汚染事故 2 件 ([[actor-recall-layer]]) が根拠。provisional は「即可視だが未確定」の
  別 type として扱う既存規律 ([[actor-recall-layer]]「暫定は別 type 即可視」) をそのまま使う。
- 承認時の有界再帰属 ([[actor-dictionary-design-2026-07-26]]) は実装済。§2.4 の再処理と合わせ、
  全文が後から入った記事も再帰属対象になる。

---

## 4. エンティティ表示の完全化 (利用者 Q3「全部のせてよいのでは」への回答)

**まず事実**: 記事詳細のエンティティパネルは**既に全 entity_type を表示している**
([articles_feed.py:416](../src/ui/api/articles_feed.py#L416) の
`ordered_types = [*_PIVOT_RELATED_TYPE_ORDER, *sorted(残り)]`)。対象記事がスカスカに見えたのは
entity_type を絞っているからではなく、**切り株 body から 4 種しか抽出できなかったから (P1)**。
全文なら malware_family / cve / victim_org / tool / affected_product / campaign 等も出る。

その上で、**「全部のせる」は正しい方向だが、フラットな全 mention dump は NSA 混同の再生産**に
なる。役割で整理して全部見せるのが本質。埋めるべき実ギャップは 3 つ:

### 4.1 確定主題アクターを役割分離で表示 (NSA 混同の直接解消)

- 記事詳細 API ([get_article_detail](../src/ui/api/articles_feed.py#L454)) の `article` dict に
  **`subject_actor_ids` / `subject_actor_source` / `subject_actor_confidence` を追加**
  (現在返していない = UI からアクセス不能)。
- UI のエンティティパネルを役割で三分:
  1. **主題アクター** (`subject_actor_ids`) — 「この記事が誰の攻撃を扱うか」。未帰属なら
     「**未帰属**」と明示 (対象記事はここが「未帰属」になり、NSA は主題でないと一目で分かる)。
  2. **言及された組織・関係者** (mention actor entity のうち subject でないもの) —
     報告機関・被害組織・ベンダ。NSA/CyberCommand はここに落ちる。
  3. **技術指標** (malware/cve/ttp/tool/ioc/victim_org/campaign/affected_product) — 従来どおり
     全 entity_type。
- ラベルは vocab SSoT 経由。この 3 分割だけで**辞書に role 属性を足さずとも NSA 混同は解消**
  する (主題は評価済み `none`、NSA は「言及された組織」に明示分類される)。

### 4.2 article_type を表示

判断分類器の `article_type` (breaking/advisory/recap/digest、救済済み
[[actor-dictionary-design-2026-07-26]]) を API で返し DIAMOND/判定パネルに追加。現在分類して
いるのに未露出。

### 4.3 (任意・二次) 辞書に posture 属性

地図 flow・情勢ボード等の**集計層**で報告機関を除外するのに、subject-gating (§5) で足りる
場合は不要。subject-gating で拾いきれない mention 集計が残るなら、辞書に
`posture: offensive|defensive|dual` (表示・集計フィルタ専用) を足す。**PIR/routing の述語には
しない** (`actor_nation` 述語の再導入禁止 [[pir-signal-first-matching-2026-07-22]] と同じ境界)。
まず §5 の subject-gating を入れ、残余を観測してから判断 (shadow mode 的に後追い)。

---

## 5. P2 — subject/mention 分離を消費層へ展開

mention actor を主体として数えている下流 5 箇所を、PIR evaluator が既に完了した移行
([evaluator.py:281-332] の subject-gating) と同型に直す:

| 消費先 | 現状 | 修正 |
|---|---|---|
| 地図 attack-flow ([geo_cyber_map.py:328-337]) | `entity_type='actor'` × victim_country | `subject_actor_ids` gated |
| 国家情勢ボード 攻撃元 ([situation.py:224-240]) | 同 mention 集計 | subject gated |
| 概況 actor-nation ([overview.py:190-206]) | 全 actor mention → nation | subject gated |
| 脅威スナップショット ([threat_operations.py:626-635]) | title+summary regex 抽出 | subject 優先・sponsor_org 抑止拡張 |
| チャット actor_activity ([tools.py:186-211]) | スナップショット依存 | 上流修正が波及 |

`_ADVERSARY_NATIONS` filter で既にクリーンな箇所 (overview `_adversary_actors`、situation
adversary 集合) は対象外。legacy 行 (subject_actor_source NULL) の fallback は §2.4 の再処理で
subject が埋まれば自然に解消。

---

## 6. Migration / Backfill

1. schema: `body_source` / `extraction_failure_reason` (§2.1)、判断分類器 `named_primary_actor`
   は既存 `llm_primary_actor_raw` を流用 (新列不要)。SQLite `_SCHEMA` + `pg_schema.py` 両方
   ([[synthesis-situation-ledger]] の教訓)。
2. 既存 body の `body_source` 遡及分類: boilerplate マーカー/長さヒューリスティックで
   `feed_summary` 相当を推定 backfill (完全ではないが監査の出発点)。
3. 切り株再取得 (§2.6) を毎時ジョブで漸進。UA 修正後の初回は GBHackers 集中投入。
4. harvest 再接続 (§3.2) 後、body 現存の cyber 記事に judgment 再分類 backfill を流し
   `named_primary_actor` を回収 (動く分類器を現存本文に流す backfill は有意 —
   [[actor-dictionary-design-2026-07-26]] の確定教訓)。
5. 下流 subject-gating (§5) はコード変更のみ (データ移行不要)。

---

## 7. 境界 (再提案不可・既存確定に抵触しないこと)

- **確定 subject 帰属の辞書ゲート + 人承認は維持** (§3.3)。LLM 自由記入の確定帰属化はしない。
- **`actor_nation` 述語を PIR/routing に再導入しない** ([[pir-signal-first-matching-2026-07-22]])。
  §4.3 posture は表示・集計フィルタ専用。
- **自動ソース提案 (発見支援) は再建しない** ([[discovery-appraisal-removed]])。本設計はソース
  取得の健全化であってソース発見ではない。
- **Discord 再投稿はしない** (§2.4)。再処理は web 側 record の silent enrichment のみ。
- **robots.txt は §10 保留のまま**。UA 更新は識別性の維持であって秘匿化ではない。
- **辞書一覧を活動ランキング化しない** ([[actor-dictionary-design-2026-07-26]])。本設計は表示
  分離であって順位付けではない。

---

## 8. 実装順序と受け入れ基準

**A (根幹) → C (発見) → B (表示) → D (集計)** の順。A が直らないと C/B は効果半減
(全文が無ければ主題も収穫も成立しない)。

| # | 内容 | 受け入れ基準 |
|---|---|---|
| A1 | body_source/failure_reason 列 + seam 集約 + guard test | body 有 ⇒ body_source 有 の不変条件が CI で強制される |
| A2 | UA config 化 + ローテ retry | 対象記事の実 URL が 200 で全文取得でき、body_source=full_extract になる |
| A3 | 失敗可視化 (2 系列 + fill_rate metric) | ダッシュボードで切り株率が全文取得失敗率と別表示され、GBHackers 100% が検出される |
| A4 | 再取得キュー + reprocess_enrichment (Discord 非再投稿) + entity replace | 対象記事が再取得後に全文化し、mention から NSA が消え IOC/TTP が付与される |
| A5 | UI 正直化 | feed_summary 記事に「抜粋のみ (全文未取得)」が表示される |
| C1 | judgment_classifier に named_primary_actor (ungated) | cyber 記事の llm_primary_actor_raw 充足が 0.35% → 大幅回復 |
| C2 | harvest を named_primary_actor に再接続 | CyberAv3ngers が actor_provisional として即可視・提案に載る |
| B1 | 詳細 API に subject_actor_* 追加 + UI 役割三分割 | 対象記事で主題=「未帰属」、NSA=「言及された組織」と表示される |
| B2 | article_type 表示 | DIAMOND パネルに article_type が出る |
| D1 | 下流 5 箇所を subject-gating | 地図/情勢ボード/概況で NSA が攻撃元に出ない |
| D2 (任意) | 辞書 posture 属性 | D1 で残余がある場合のみ着手 |

**e2e 受け入れ**: 対象記事 `rss:https://gbhackers.com/?p=193533` を再取得 → 全文化 →
CyberAv3ngers が provisional 化 → 主題「未帰属」/ NSA「言及組織」表示 → 承認後に主題化、が
一気通貫で確認できること。

---

## 9. 関連

CLAUDE.md §4 (中華系 denylist は不変)・§7 (新列 3 点セット規約)・§10 (robots 保留)。
memory: [[subject-actor-attribution-2026-07-17]] [[actor-dictionary-design-2026-07-26]]
[[actor-recall-layer]] [[organic-integration-audit-2026-07-12]]
[[mitre-sync-generic-alias-regression-2026-07-21]] [[notification-push-pull-redesign]]
[[vocabulary-label-architecture]] [[pir-signal-first-matching-2026-07-22]]
[[session-handoff-2026-07-27]]。
