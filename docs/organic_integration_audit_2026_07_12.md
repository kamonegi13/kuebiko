# 有機的結合 監査 (2026-07-12) — 機能・データは全体として機能しているか

- 動機: 機能・タギング・処理を積み上げてきた結果、各要素が有機的に関連し効率的・効果的に
  機能しているかへの疑問 (ユーザー提起)。
- 方法: ①実データ定量監査 (本番 PG 直接照会: fill-rate 週次 / entity 分布 / テーブル規模 /
  ジョブ・ルーティング実績) ②コード調査 4 系統の並列実施 (生産→消費マップ / 死蔵・残骸 /
  重複・語彙不一致 / オーケストレーション依存) ③断定度の高い所見のスポット裏取り。
- 位置づけ: [full_audit 2026-07-05] (機能の正しさ) / [synthesis 品質監査 2026-07-11]
  (台帳の品質) に続く第 3 の監査 = **結合の監査**。評価のみ、修正は別途。

---

## 0. 結論 (BLUF)

**背骨は有機的に機能している。断線は周辺神経系に、しかも同じ 4 つの病因から反復発生している。**

収集→triage→routing→台帳→synthesis→配信 の主経路は健全に接続され (feed 死活 1/128、
embeddings 被覆 87%、config 12 キー全 R/W、import 孤立ゼロ、prompt 全 12 使用中)、
移行 flag 7 本もすべて新側で稼働。一方で:

1. **抽出→消費の供給網が無監視で、沈黙断線が反復している** — intent (6/29〜、修復済) に
   続き、本監査で **technical_axis (6/29〜) と event_date (7/04〜) の崩壊が新たに発覚**。
2. **後から追加したデータの相当数が write-only** — 誰も読まない列/エンティティが 9 件。
3. **横断概念が方言化** — 「日本関連」4 方言、「重要」6 系統、intent ラベル 3 表記、
   nation ラベル 5 箇所、期間窓の混在。製品間で意味が割れ、同じ DB から矛盾する数字が出る。
4. **機構間の意味の断絶** — 生成物の importance=medium 固定が「要対応」KPI から構造的に
   漏れる、salience の日本判定が routing の是正を打ち消す、forecast の「外れ」が採点されない。

個々の問題は小さいが、**病因が共通のため個別修正では再発する**。対処は病因側 (§4) へ。

---

## 1. 定量所見 (本番 DB 実測)

### 1.1 抽出 fill-rate の週次推移 (posted、カテゴリ内で測定)

| 指標 | 健全期 | 現在 | 判定 |
|---|---|---|---|
| socio_political_intent | 41-79% | 3%→**backfill で回復中** | 修復済 (0663350) |
| **technical_axis_summary** | 13-17% (6月中旬) | **1-2%** (6/29〜) | **暗黒化・未修復** (b8b4e7dd と同時、backfill 対象外) |
| **event_date (vuln/advisory)** | 45-59% | **5%** (7/06 週) | **崩壊** (7/04 の「報道日コピー禁止」強化後) |
| **event_date (cyber 事象)** | 36-54% | **17%** | 同上 |
| compromise_date | 0-2% | 0% | **恒常的に空** — dwell 分析は事実上データ無し |
| remediation | 0% | 19% (7/04 新設) | 供給は開始、**読者ゼロ** (§2) |
| editorial_stance | — | factual 4,252 / analytical 2,174 / opinion 840 / propaganda 115 (null 40) | **健全** (調査 agent の「96% unknown」説は実データで棄却) |

**含意**: intent 暗黒化は孤発でなく、**「正直さ強化のプロンプト変更が弱シグナルを
ゼロ化するオーバーシュート」という同型事故の 3 例目・4 例目**が同時期に起きていた。
event-time 層 (時系列トグル/dwell/作戦史復元) と Capability⇄Infrastructure 軸
(ThreatsTab/NewsPage 表示) は母数が枯れつつある。

### 1.2 PIR タグの偏在 (30 日)

broad PIR が支配: pir_general_agency_alert 3,194 / pir_new_poc_vuln 2,887 /
pir_jp_company_breach 2,822 に対し、戦略 PIR は pir_china_apt **101** / pir_dprk_apt
**100** / pir_integrated_cyber_ops **33** — **約 100:1**。PIR は triage 基準・salience
乗算・daily focus の駆動源のため、broad PIR が優先度信号を희釈している
(news_search_consolidation で既知の「strong_signals 過広」が定量化された形)。

### 1.3 forecast lifecycle の採点偏向

`situation_forecasts`: **open 205 / hit 16 / miss・expired 0**。「外れ」が一度も
記録されておらず、weekly tradecraft の scorecard は的中側のみ表示 = 較正機能の毀損。
(open→scored 同期の期限切れ採点が未実装か不発かはコード確認要 — 評価対象として指摘。)

### 1.4 その他

- routing: 30 日で **55% が R7.fallback 着地** (梯子の特異的ルールは 45% しか説明しない)。
  設計意図 (既定=watch) なら可、ただし監視対象にすべき。rule id に `R2.inoreader.*` の
  撤去済みソース名が残存。
- 健全: dedup_seen_urls 22.9k (retention 稼働) / situation_evidence 孤児 13 件のみ /
  job_last_run 全 succeeded。

---

## 2. write-only / 死蔵データ (生産→消費マップの結論)

「作ったが誰も読まない」= 有機的結合の最直接の欠損。全列・全 entity_type の
書き手/読み手を突合した結果 (詳細は調査ログ):

### 完全 write-only (読者ゼロ、grep 確定)

| データ | 書き手 | 皮肉/影響 |
|---|---|---|
| **articles.intent_confidence** | persistence.py:160 | **確度を担う唯一の列が捨てられ、全消費者 (地図色分け/情勢/板/検索 chip) が low 仮説を high と同格に扱う**。本日修復した「確度つき記録」の受け皿が下流に無い |
| **articles.remediation** | persistence.py:161 (7/04 新設) | 「行動可能性 gap を埋める」目的で追加した対処 1 文が、記事詳細含めどこにも表示されない |
| entity `malware_type` | persistence.py:365 | 読者ゼロ (825 行蓄積) |
| daily_briefs.section_count | repo_knowledge.py | API まで運ばれ frontend で未描画 |
| f1_selections のスコア列 | digest/runner.py | dedup_key しか読まれず、4 軸採点 (pir/roi/timeliness/novelty) は較正にも使われない |
| articles.duration_seconds | (常に NULL) | 書き込みすら空 |

### 実質死蔵 (read 経路はあるが live caller 不在)

- **geo_cities / geo_orgs テーブル + entity `victim_city`**: Geocoder.city()/org() を呼ぶ
  本番コードが無い (geo_cyber_map は country() のみ使用 — 「国粒度バブル」の確定設計と
  整合しており、**将来 Phase 用の先行投入が宙に浮いた状態**)。
- situation_detection_log: SELECT が src に 0 件 — ただしこれは**人間監査専用の台帳**として
  実際に監査 (7/11-12) で psql から使われた。「コード読者ゼロ = 死蔵」ではなく
  「意図の明文化 or UI 化候補」が正しい評価。

### 消費が細い (単一読者・想定した接続の未実装)

- entity `mentioned_country` / `campaign`: **Situation Ledger 専用**。tagging survey
  (7/04) が意図した検索 facet への接続は未実装 (docstring に「検索 facet」と書かれたまま)。
- entity `tool` / `ioc_url`: threat_ops 集計のみ。compromise_date: dwell 表示 1 箇所のみ。
- routing_rule_id/reason: 「判定時点 snapshot」を設計意図に持つが、読者は記事詳細の
  監査パネルのみ — ルール有効性の集計 (R7 55% のような) に未活用。

---

## 3. 方言化と機構間断絶 (重複調査の結論、スポット裏取り済)

### 3.1 「日本関連」が 4 方言 — 画面間で数字が矛盾する

| 方言 | 使用箇所 | 基準 |
|---|---|---|
| targeted (厳格) | routing / PIR | LLM japan_targeted or victim=JP (言及を明示排除) |
| victim+channel | ダッシュボード KPI / jp_ci_board | victim=JP **or** channel=japan_watch |
| **channel のみ** | **threat_operations** (japan_targeted_count/filter) | channel=japan_watch のみ — **victim=JP でも alert に投稿された事案は日本標的に数えない** |
| mention (最広) | **salience の日本ブースト** | claim に「日本」or mentioned_country:JP |

実害: ①同一事案の「日本標的件数」が 3 画面で食い違う。②routing が意図的に排除した
「日本への言及だけの記事」由来の判定が、headline 選定では +2.0 の下駄を履く —
**上流の是正を下流が打ち消す**、有機的結合の逆転現象。

### 3.2 「重要」の 6 系統と、生成物の構造的欠落

triage importance (LLM) / salience (決定論) / F1 rubric (LLM 4軸) / 要対応 KPI
(high 限定) / 地図閾値 / board 段階 (キルチェーン) は目的が違うので併存自体は正当。
問題は接続部:
- **synthesis / spotlight / pir_daily_focus / ransomware 取込はすべて importance="medium"
  固定** → 「要対応」(high 限定) から**構造的に**漏れる。ransomware.live の **JP 被害も
  medium 固定**。
- board で最上段 (OT 接近) の medium 記事は、ダッシュボード「要対応」に出ない —
  重要度の順序が 2 系で逆転しうる。

### 3.3 ラベル・語彙の SSoT 違反 (裏取り済)

- **intent 3 方言**: canonical「事前配置」(diamond_model) vs jp_ci_board「**事前潜伏**」
  vs frontend「事前配置」。subversion は「体制動揺・転覆」「工作」「体制転覆」の 3 表記。
  frontend が「backend canonical は diamond_model」と明記しながら backend 内で破られている。
- **nation ラベル 5 箇所** (countries.yaml / overview / geo_cyber_map / diamond.ts /
  routingLabels.ts)、収録国もバラバラ (gb は地図にあり概観に無い)。
- **sector ラベル**: victim_sectors.yaml が SSoT なのに overview.py:114 が 22 canonical を
  ハードコード複製 (yaml 更新に追従しない)。
- **期間窓**: ダッシュボード 7 日 vs jp_ci_board 30/365 日。**editorial-quality は同一
  レスポンス内で 14 日 (記事) と 30 日 (クロス集計) が混在 — API param `lookback_days`
  がクロス集計側で無視されるハードコード (裏取り済、バグ濃厚)**。
- **照合正規化の 2 流派**: NFKC+lower (組織照合/dedup) vs lower のみ (match_lists /
  PIR keyword)。さらに **match_lists は境界なし substring** で、PIR keyword が語境界で
  直した誤爆 (短語の "not/million" 誤マッチ) を運用者定義キーワードでは再現する。
- Discord builder 11 箇所: confidence が embed では生英語 "high"、Web では「確度高」。

### 3.4 由来の混入

ransomware_ingest が pmesii_e / pmesii_i_cyber を**無条件 True** で書くため、PMESII の
E / I-cyber 軸集計はランサム取込件数で系統的に嵩上げされる (集計側に由来フィルタなし)。

---

## 4. 病因 (root causes) — 個別修正では再発する理由

| # | 病因 | 症状の例 |
|---|---|---|
| **R1 供給網の無監視** | 抽出 fill-rate の常設監視が無い (較正監査は importance のみ)。プロンプト変更の影響が数週間沈黙する | intent 2 週間 / tech_axis 2 週間 / event_date 進行中。すべて発見が人力・偶発 |
| **R2 追加時に消費者を設計しない** | 列/entity/表示を足す時、既存消費者への接続 (フィルタ・facet・KPI) が同時に設計されない | intent_confidence (6/30 追加→読者ゼロ)、remediation (7/04 追加→読者ゼロ)、mentioned_country の facet 未配線 |
| **R3 SSoT は宣言されるが強制されない** | 等価性テストは flag 両側には在るが、語彙・窓・判定関数には無い | intent ラベル 3 方言、nation 5 箇所、sector 複製、日本判定 4 方言 |
| **R4 「正直さ強化」の一律切り捨て** | 過剰帰属対策のプロンプト変更が「確度つき記録」でなく「抑圧」で実装される | intent (修復済)・event_date・technical_axis の同型 3 連発 |

補足 — オーケストレーション層は概ね健全: interval anchor は epoch 量子化で設計済み
(「ドリフト」は再起動直後の misfire catch-up 1 発のみ)、単一ロック直列化・収集抑止・
親側失敗通知は機能。残る問題は **CLAUDE.md §7 の要注意時刻表が実スケジュール
(大半 02:00-04:30 に移設済) と乖離**して安全なデプロイ判断を誤らせること、
auto-trigger が runs/job_run_log のどちらにも記録されない可観測性ギャップ、
月曜×月初の深夜 heavy 連鎖、の 3 点。

---

## 5. 所見一覧 (優先度順)

### HIGH (分析品質への実害が現在進行)
| ID | 所見 | 根拠 |
|---|---|---|
| H1 | event_date 供給崩壊 (vuln 5% / cyber 17%) — event-time 層の母数枯渇 | §1.1、7/04 変更後に急落 |
| H2 | technical_axis_summary 暗黒化 (b8b4e7dd、backfill 未対象) | §1.1 |
| H3 | intent_confidence write-only — 低確度仮説が全消費者で確定と同格 | §2 |
| H4 | 「日本関連」4 方言 — 画面間矛盾 + salience が routing の是正を打ち消す | §3.1 (裏取り済) |
| H5 | 生成物/ransomware の importance=medium 固定 × 要対応=high 限定 — JP ランサム被害が「要対応」に出ない | §3.2 |
| H6 | forecast の「外れ」未記録 (205 open / 16 hit / 0 miss) — scorecard 的中偏向 | §1.3 |

### MEDIUM
| ID | 所見 |
|---|---|
| M1 | remediation write-only (追加目的が未達成) |
| M2 | PIR 100:1 偏在 — broad PIR が優先度信号を희釈 (strong_signals 過広の定量化) |
| M3 | intent/nation/sector ラベル方言 + editorial-quality 14/30 窓バグ |
| M4 | match_lists 境界なし substring (PIR と非対称な誤爆挙動) |
| M5 | CLAUDE.md §7 時刻表 stale + auto-trigger 可観測性ギャップ |
| M6 | mentioned_country/campaign の検索 facet 未配線 (survey 意図の未完) |
| M7 | pmesii E/I-cyber のランサム由来嵩上げ |
| M8 | compromise_date 恒常 0-2% (dwell 分析は空箱) |

### LOW / 衛生
| ID | 所見 |
|---|---|
| L1 | geo_cities/geo_orgs/victim_city 先行投入の宙吊り |
| L2 | **docker-compose の `.git:rw` マウント残骸** (auto-commit 撤去済み。セキュリティ的にも外すべき) + stale docstring |
| L3 | Jinja SSR 足場 (base.html + app.py の templates 配線、TemplateResponse 0 件) |
| L4 | malware_type / f1 スコア列 / section_count / duration_seconds の write-only |
| L5 | rule id inoreader 命名 / 台帳孤児 13 / *.bak 4 本 / actor_aliases.translated.yaml |
| L6 | CrewAI agents skeleton (Phase 5 保留のまま起動時検証+UI+テストに配線) |
| L7 | detection_log のコード読者ゼロ (人間監査専用の意図を明文化 or UI 化) |
| L8 | R7.fallback 55% (監視指標化) / 月曜×月初 pileup / shadow モード未使用分岐 |

### 健全性の確認 (問題なし)
- legacy fallback 約 500 行は CLAUDE.md 宣言どおりの意図的残置 (等価性テストが両側を保護) — **削除不要**
- editorial_stance は健全 (調査中の「96% unknown」説は実データで棄却)
- embeddings 87% 被覆 / feeds 1/128 failing / prompts 全数使用 / import 孤立ゼロ /
  config_store 全キー R/W / interval anchor 設計済み

---

## 6. 推奨 (評価に基づく優先順、実装は別途判断)

1. **供給網ヘルス監査の常設化** (R1 への対処、最優先): audit_triage_calibration と同型の
   「抽出 fill-rate 週次監査」— 全分析列 + entity_type をカテゴリ内被覆で監視し、隣接週
   急落で WARN。heartbeat (08:00 ops) に組込めば沈黙断線は 1 日で露見する。
   今回の H1/H2 は**この装置があれば 6/30 に検出できていた**。
2. **event_date / technical_axis の修復** (R4 の同型修理): intent と同じ「確度つき記録」
   への転換 (event_date は basis で正直さを既に表現できる — 切り捨てでなく basis=reported
   を許して分析側でフィルタ)。backfill は既存スクリプトの拡張で可能。
3. **intent_confidence の消費配線**: 地図/情勢/板/検索に確度フィルタまたは表示。
   prepositioning posture 設計 (§12 較正) の前提でもある。
4. **「日本関連」の SSoT 関数化**: `japan_relevance() -> targeted|mentioned|none` を 1 箇所に
   置き、threat_ops の channel-only と salience の mention 直参照を寄せる。
5. **要対応 KPI の定義拡張** (ransomware JP / OT 段階を含める) と forecast miss 採点の修理。
6. **ラベル/窓/照合の SSoT 化スイープ** (M3/M4): intent・nation ラベルの単一供給、
   overview の sector 複製排除、editorial-quality 窓バグ、match_lists の語境界。
7. **衛生スイープ** (L2 の .git:rw は即時、他は低優先でまとめて)。
8. **規約の追加** (R2/R3 への恒久対処): 「新しい列/entity/タグを足す PR は、①最低 1 つの
   消費者 ②fill-rate 監査への登録 ③ラベルは SSoT 参照 — を同時に含める」を CLAUDE.md に。

---

## 付録: 調査の信頼性について

- 4 系統のコード調査は独立の並列調査で実施し、断定度の高い所見 (editorial-quality 窓 /
  intent ラベル方言 / sector 複製 / threat_ops の channel 限定 / 本番 flag 実態) は
  実コード・実 env で裏取りした。
- 調査中に**棄却された仮説**も記録する: 「editorial_stance 96% unknown」(実データで棄却)、
  「article_embeddings 378 行しかない」(pg_stat の stale 推計。実測 15,183 行で健全)、
  「interval ジョブが任意の分にドリフト」(コードは epoch 量子化済み。観測された :24 発火は
  デプロイ直後の misfire catch-up の過渡 1 発)。
- 未検証の推測として残るもの: routing 時 full_text の NFKC 適否 / forecast miss 不発の
  正確な機序 / editorial-quality 14/30 が意図か否か。
