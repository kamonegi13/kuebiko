# Entity パイプライン インベントリ (収集 → 抽出 → 分析 → 消費)

> 2026-07-29 のコード全棚卸しに基づく **entity 系の詳細処理地図**。README「ツールの構造」節が
> 全体の一枚図、本書がその中の「記事 → article_entities/分析列 → 表示」の内部を file:line 単位で
> 記述する。**現状のコード実挙動**を記述する (設計意図と乖離がある箇所は §6「既知のギャップ」に明記)。
>
> 背骨は README と同じ単方向フロー: **収集(観測) → articles/article_entities(事実) →
> 分析(判断) → 消費(射影)**。逆流は人承認を通る辞書/PIR 提案のみ。

---

## 0. 4 層の俯瞰

| 層 | 主モジュール | 入力 → 出力 | 単一窓口 |
|---|---|---|---|
| ① 収集 | `src/tools`, `src/watchers`, `src/grok`, `src/sources` | 外部ソース → `Article` | `source_router.build_source` |
| ② 抽出 | `src/pipeline/persistence.py`, `src/cti/*` | `Article`(本文) → `article_entities` 行 | `_persist_article_entities` |
| ③ 分析 | `src/pipeline/briefing.py`, `src/cti/judgment_classifier.py` | 本文 → articles 分析列 | `_summarize_and_build` |
| ④ 消費 | `src/ui/services/*`, `src/pir/*`, `src/cti/stix_*` | article_entities/分析列 → 画面/STIX | (分散、下記 §5) |

**最重要の不変条件**: `article_entities` は **mention (言及)**、`articles.subject_actor_ids` は
**subject (記事の主語)**。両者は別テーブル・別書込経路で、混同しないことが entity 品質の要。

---

## 1. 収集層 — Article と body_source の確定

### 経路 (すべて `ArticleSource` Protocol に統一 → `run_pipeline`)

| 経路 | fetch 実装 | 本文の由来 |
|---|---|---|
| RSS | `src/tools/direct_rss_source.py:208` (`DirectRssSource.fetch`) | URL → `content_extractor.extract` |
| scraper/watcher | `src/watchers/sitemap_base.py:247` / `html_listing.py:183` (URL 発見のみ、`summary_html=""`) | URL → `content_extractor.extract` (RSS と同一) |
| Grok | `src/grok/fetcher.py:192` (Playwright DOM) → `src/pipeline/grok_convert.py:44` | メール本文テキスト (URL 再取得しない) |
| ransomware.live | `src/sources/ransomware_ingest.py:202` | 構造化 API (本文/LLM を経由しない別系統) |

### body_source の決定 = `_resolve_body` (`src/pipeline/briefing.py:288-310`)

```
if extraction.success and extraction.text.strip():
    source = "playwright_extract" if method=="playwright+trafilatura" else "full_extract"
    return text, source, None
return _strip_html(summary_html), "feed_summary", extraction.failure_reason   # 切り株
```

| body_source | 意味 | 設定箇所 |
|---|---|---|
| `full_extract` / `playwright_extract` | trafilatura / JS 突破後の抽出成功 (≥200字) | `briefing.py:298-301` |
| `prefetch` | triage 前の薄い feed 先行抽出 (品質保証は full と同一) | `briefing.py:48` |
| `grok` | Grok tweet 本文 | `grok_convert.py:209` |
| `feed_summary` | 抽出失敗 → feed 抜粋 fallback (=切り株) | `briefing.py:303-310` |
| `none` | retention purge で body を NULL 化した後 | `repo_dedup.py:526` |
| `scraper` | **死んだ列挙値** (監査フィルタが参照するが誰も書かない) | — |

**書込 seam は 1 箇所**: `repo_articles.py:97` (`update_article_body`)。呼び出し元は通常投稿
(`persistence.py:264`)・再取得 (`reprocess.py:117`)・手動 backfill の 3 つ。

---

## 2. 抽出層 — entity_type 完全目録

すべて `_persist_article_entities` (`src/pipeline/persistence.py`) が `article_entities` に
INSERT。UNIQUE(article_id, entity_type, value) で冪等。**add-only** (再取得時のみ replace)。

| entity_type | 抽出元 | 辞書ゲート | 人承認 | 備考 |
|---|---|---|---|---|
| `actor` | `actor_registry.find_all(body)` → R-A (`briefing.py:186`) | **あり** (辞書内 id のみ) | 不要 | word-boundary 一致 |
| `actor_provisional` | LLM `named_primary_actor` / ベンダ designation (`persistence.py:550`) | なし | **要** | 未収録の新興候補を隔離 |
| `cve` | `mitre_techniques`/`iocs`/regex (`persistence.py:386`) | 形式検証のみ | 不要 | |
| `ttp` | `mitre_techniques` (`persistence.py:393`) | 形式検証 (T\d{4}) | 不要 | |
| `ioc_ip/domain/sha256/sha1/md5/url` | `msg.iocs` (LLM+regex) → 型判別 (`persistence.py:601`) | benign 除外のみ | 不要 | |
| `malware_family` | LLM → `malware_normalizer.normalize` | **オープン語彙** (未収録素通り) | 不要 | hallucination 混入余地 |
| `malware_type` | malware_family から type 導出 | config あり | 不要 | **消費者未確認 (write-only 疑い)** |
| `tool` | LLM → `normalize_tool` | オープン語彙 | 不要 | |
| `affected_vendor/product` | 確定 cve → NVD CPE 逆引き | NVD キャッシュ | 不要 | |
| `victim_org` | LLM (`persistence.py:484`) | **なし** (素通り) | 不要 | |
| `victim_city` | LLM (`persistence.py:492`) | なし | 不要 | **消費者未確認** |
| `involved_country` | LLM ISO (`persistence.py:498`) | なし | 不要 | |
| `mentioned_country` | gazetteer (`mention_tagger.py:75`) | 実質辞書 | 不要 | |
| `campaign` | "Operation <Name>" regex + blocklist (`mention_tagger.py:53`) | なし | 不要 | |
| `pir` | signal-first 述語ツリー (`persistence.py:310`) | config (pir.yaml) | 不要 | |

### mention → subject の昇格ライン

- **subject 判定の単一関門**: `determine_subject_actors` (`src/cti/subject_actor.py:117`)。
  通常経路 (`persistence.py:146`) と再取得 (`reprocess.py:133`) の 2 箇所から呼ばれる。
- **証拠等級 3 層**: layer0 `SOURCE_FEED` (構造化断言) > layer1 `SOURCE_TITLE` (title 決定論走査)
  > layer2 `SOURCE_LLM` (`primary_actor_id` が **mention 集合に含まれる場合のみ**採用 = 二重ゲート)。
- **構造ゲート**: LLM が mention に無いアクターを勝手に主題化できない (`subject_actor.py:169`)。

### 一般語ガードの実体 (`src/cti/generic_alias_words.py`)

- 静的 denylist = 元素名 24 語 (potassium 等) + 一般語 7 (`{anonymous, bear, cloud, kitten, panda,
  spider, tick}`) + サイバー犯罪群 14 (`{play, deadlock, lynx, interlock, everest, anubis, chimera,
  axiom, gallium, chaos, cloak, kairos, warlock, morpheus}`) の**計 45 語** (Phase 1 で 31→45)。
- 一般語衝突アクターは辞書側 `ambiguous: true` + `context_cues` で文脈 cue を要求してゲートする。
  cue 未指定時は `resolve_ambiguous_cues` (`actor_normalizer.py`) が非 hacktivist に
  `CYBERCRIME_CONTEXT_CUES` を既定割当 (Phase 1 で SSoT 化)。**`play` / `deadlock` / `lynx` /
  `interlock` / `everest` / `anubis` / `chimera` / `axiom` / `gallium` の 9 件は Phase 1 で
  `ambiguous:true` 化済** (旧 §6 ギャップ① = 是正済)。
- 防御機関 (`us_nsa` 等 `family=state_organ`) は mention としては cyber カテゴリで今も登録され、
  除外は消費層の `reporter_org_actor_ids` (`actor_roles.py:20`) が担う (§4)。

---

## 3. 分析層 — articles 分析列の生成

### 実行順 (1 記事)

`triage(importance フィルタのみ)` → `summarizer LLM (SummaryOutput)` →
`judgment_classifier LLM (1 呼出で 8 判断)` → `決定論後処理 (diamond/pmesii/victim/temporal/importance cap)` →
`routing` → `determine_subject_actors` → 永続化。

### 列 → 生成元 (主要)

| 列 | 生成元 |
|---|---|
| `importance` | summarizer LLM → `_cap_vuln_importance` 決定論キャップ (KEV+悪用語) |
| `category` | summarizer LLM のみ |
| `article_type` | **judgment_classifier** が summarizer の枯死値(breaking 固定)を上書き (`briefing.py:134`) |
| `editorial_stance` | judgment → routing_flags 経由 → **publish 成否依存で outcome dict に書く** (§6 ギャップ) |
| `socio_political_intent` / `technical_axis_summary` | judgment → `parse_diamond_axes` (`diamond_model.py`) |
| `pmesii_i_infra` | **4 系統の OR** (summarizer / judgment i_infra / feed 既定 / NISC セクターフロア、§6 ギャップ 11) |
| `victim_sector/country` | summarizer LLM (raw) → `taxonomy_normalizer` 正規化 |
| `event_date/compromise_date` | judgment → `_normalize_temporal` 決定論検証 |
| `subject_actor_ids/source/confidence` | `determine_subject_actors` (judgment とは別関数) |
| `llm_primary_actor_raw` | judgment の **ungated** `named_primary_actor` (harvest 用) |

### summarizer 一括 vs focused judgment_classifier

summarizer が 18 フィールドを 1 生成に詰めた結果、末尾の判断系が 0-5% に枯死 (2026-06〜07)。
`editorial_stance`/`diamond`/`event_date` は summarizer prompt から除去し **judgment_classifier に
移設**。`article_type` は prompt 上残存だが実質 judgment が上書き。**judgment 失敗時は
summarizer 由来の枯死値に fail-closed** (article_type=breaking / stance=unknown / intent=unknown)。

---

## 4. 消費層 — mention vs subject ゲート適用状況

| 消費者 | 消費 | ゲート | 状態 |
|---|---|---|---|
| 地図 attack-flow (`geo_cyber_map.py:338`) | article_entities actor | subject_gate + reporter 除外 | ✅ ゲート済 |
| 国家情勢ボード攻撃元 (`situation.py:225`) | actor | subject_gate + reporter 除外 | ✅ ゲート済 |
| 概況 actor-nation (`overview.py:196`) | actor | subject_gate + reporter 除外 | ✅ ゲート済 |
| 記事詳細 (`articles_feed.py:399`) | subject + 全 entity | API で role 分離、frontend が 3 分割表示 | ✅ 分離済 |
| 行動史 (`actor_observed_history.py`) | subject_actor_ids のみ | 設計から subject 限定 | ✅ ゲート済 |
| PIR match (`evaluator.py:235`, `signal_match.py:98`) | entity/subject | subject_gate (env `SUBJECT_ACTOR_GATE`) | ✅ ゲート済 |
| 脅威スナップショット (`threat_operations.py`) | 独自 title+summary regex 抽出 | `passes_subject_gate` + reporter 除外 | ✅ ゲート済 (Phase 1) |
| チャット actor_activity/profile (`assistant/tools.py:186`) | `fetch_threat_operations_snapshot`/`fetch_actor_detail` に従属 | 上記の従属で継承 | ✅ ゲート済 (Phase 1) |
| 国家情勢 `situation_by_nation` (`situation.py:340`) | actor | subject_gate | ✅ ゲート済 (Phase 1) |
| PIR KPI top_actors (`evaluator.py:801`) | matched 記事の全 mention | subject_gate | ✅ ゲート済 (Phase 1) |
| STIX export (`article_ops.py`, `stix_from_briefing.py`) | mention/再走査 | 主題 actor のみ出力 | ✅ ゲート済 (Phase 1) |
| News/Search facet (`articles_feed.py:103`) | actor 完全一致 | 無し (意図的=言及検索) | ⚪ 仕様 |
| fill-rate 監査 (`fill_rate_audit.py:138`) | actor EXISTS | 無し (意図的=供給網監視) | ⚪ 仕様 |

**Phase 1 (2026-07-29, `b3a0bbd6`, branch `fix/entity-gating-phase1`) で上記 5 箇所すべてに
subject-gate を適用済**。棚卸し時点では設計 doc (`body_extraction_and_entity_integrity_redesign.md`
§5) の「下流5箇所 subject-gating」が実装 3/5 で、脅威スナップショット・チャットが未 gate、さらに
設計 doc に無い漏れ (situation_by_nation / PIR top_actors / STIX 外部流出) を新規発見していた。
Phase 1 でこれを解消し、STIX は主題 actor のみ出力して外部誤帰属流出を防ぐ。

---

## 5. 主要ファイル早見

| 役割 | ファイル |
|---|---|
| 収集オーケストレーション | `src/pipeline/orchestrator.py`, `dispatch.py` |
| body_source 決定 | `src/pipeline/briefing.py:288` (`_resolve_body`) |
| 本文抽出 | `src/tools/content_extractor.py` (paywall 判定 = §6 ギャップ) |
| entity 永続化 (唯一窓口) | `src/pipeline/persistence.py` (`_persist_article_entities`) |
| 主題判定 (唯一関門) | `src/cti/subject_actor.py` (`determine_subject_actors`) |
| アクター辞書照合 | `src/cti/actor_normalizer.py` (`find_all` / ambiguous ゲート) |
| 一般語 denylist | `src/cti/generic_alias_words.py` (45 語, Phase 1 で 31→45) |
| 報告機関除外 | `src/cti/actor_roles.py` (`reporter_org_actor_ids`) |
| subject-gate SQL | `src/cti/subject_gate.py` |
| 統合判断分類器 | `src/cti/judgment_classifier.py` |
| 脅威スナップショット (独自 matcher, subject-gate 済 Phase 1) | `src/ui/services/threat_operations.py` |
| アクター辞書 SSoT | `config/actor_aliases.yaml` (199 actors) |

---

## 6. 既知のギャップ (2026-07-29 棚卸し時点)

> **更新 (2026-07-29)**: Phase 1 (`b3a0bbd6`, branch `fix/entity-gating-phase1`) で **ギャップ 1・2・3
> を是正済** (各項に ✅ で明示、コミットは main 未マージ)。残り 4-14 が未着手。棚卸し時点では
> MEMORY の [[body-extraction-entity-integrity-2026-07-27]] が「下流5箇所 subject-gating 全 deploy+
> 検証済」とするのに対し、コード実査で **3/5 のみ実装**と判明していた。本書が実挙動の SSoT。

### entity / attribution

1. ✅ **[是正済 Phase 1] 一般語衝突アクターがガード外** (`deadlock`/`play`/`crpxo`/`everest`/`lynx`/
   `chimera`/`anubis`/`interlock` 等)。`ambiguous:true` 無し・generic_alias_words 未収録で、本文の
   同綴り一般語を誤 mention 化する (`potassium` 事件 53% 汚染と同型の再発条件)。→ **是正**: 9 件を
   `ambiguous:true` 化 + `_CYBERCRIME_GROUP_WORDS` 14 語を denylist 追加 (31→45 語)。cue 解決を
   `resolve_ambiguous_cues` に SSoT 化し guard test / mitre filter / editor 警告に自動反映。
2. ✅ **[是正済 Phase 1] 脅威スナップショットの subject-gate 計画倒れ** (`threat_operations.py`)。
   mention をそのまま「アクター活動件数」に集計していた。→ **是正**: `passes_subject_gate` を
   snapshot core 3 クエリ + 記事詳細 + 子集約に適用。cue 解決も `resolve_ambiguous_cues` に集約
   (残: 独自 regex matcher 自体の `actor_normalizer` 統合は未 — DRY は部分改善に留まる)。
3. ✅ **[是正済 Phase 1] 設計外の無ゲート消費者** = PIR KPI top_actors / STIX export (外部流出) /
   situation_by_nation。→ **是正**: 3 箇所すべてに subject-gate。STIX は主題 actor のみ出力。
4. **recap の title 層 subject 汚染が未対処**。`article_type != "recap"` ガードは LLM 層のみで、
   `determine_subject_actors` の layer1(title 走査)に article_type が渡らない (`subject_actor.py:117`)。
   ダイジェスト記事の title にアクター名があると主題に誤昇格しうる。
5. **malware_family/tool はオープン語彙** (辞書ゲート無し、LLM hallucination 素通り)。
6. **reprocess の replace 対象から `malware_type` が漏れ** (`reprocess.py:32`)。再取得で
   malware_family が変わっても古い malware_type が add-only で残存 (二重登録)。
7. **`malware_type`/`victim_city` の消費者未確認** (write-only 疑い、CLAUDE.md §7 規約要確認)。

### 本文抽出 / body_source

8. **paywall 薄切りが success=True で素通り** (`content_extractor.py:252-292`)。`_looks_like_paywall`
   が `本文長 < 200字` の分岐内でしか呼ばれず、「プレビュー段落 + Subscribe」が 200 字以上だと
   paywall 判定を経ず `full_extract` 確定。→ **切り株率 (feed_summary) では検出不能な本文欠落**。
9. **切り株率の分母不整合**: scraper/watcher の抽出失敗は `summary_html=""` のため feed_summary に
   ならず body=NULL(`none`) になる (`briefing.py:281`)。RSS/Grok の失敗しか切り株に数えない。
10. **恒久失敗 blacklist が空回り**: 再取得失敗時に extraction_failure_reason を書かないため
    (`reprocess.py:75`)、reason ベースの恒久除外 (`repo_articles.py:444`) がほぼ機能しない
    (3348 件中 reason 記録 1 件)。取れるソース(GBHackers=実測 100% 再取得可)と取れないソース
    (FDD=WAF 403)を区別できず無駄 churn。

### 分析

11. **i_infra は下限にしか効かない**。judgment の厳格判定 (「個別CVE/SaaS=false」) を feed 既定
    (Dragos/Nozomi/ENISA=feed 名で強制 True) と NISC セクターフロア (victim_sector が CI 系なら
    True) が OR で上書き (`nisc_sectors.py:324`)。→ 「CISA KEV 追加=i_infra:True」の正体。
12. **routing_flags["confidence"] の二重意味衝突** (`briefing.py:124`)。subject 判定確度が、
    無関係な japan_targeted regex fallback ゲート (`routing_signals.py:288`) を乗っ取る。
13. **editorial_stance が publish 経路依存で欠落** (`persistence.py:217`)。skipped_duplicate だと
    値があっても NULL 永続化 (他の分析列は metadata から直接読むのに stance だけ outcome dict 経由)。
14. **judgment_classifier 失敗の可観測性ギャップ**。失敗すると 5 列がまとめて枯死値に戻るが、
    triage と違い run 単位カウンタ・ops 通知が無く、検知は週次 fill-rate 監査のみ (最大 1 週間ラグ)。
