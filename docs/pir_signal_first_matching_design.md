# PIR signal-first 照合 再設計 (2026-07-22)

> **status**: **17 PIR signal-first deploy 済** (2026-07-22〜23、完了)。PoC 3 → ゼロ移行
> jp_company/ally → feed_title agency_alert → keyword_any 6 → critical_infra/emergency →
> Phase A (integrated_cyber_ops `NOT research` 15→8 / jp_targeted victim=jp AND NOT geo
> 391→338) → **概念 PIR 4 件 (apt_leak/attribution/integrated/geopolitical) に LLM 主題
> 判定層を重畳 + geopolitical_cyber 再定義** (2026-07-23、§7 の未決を解決)。
> **Phase 2b (boolean 列スキーマ移行) は A/B で不要と判明・見送り** (§7.6)。
> 残 keyword は意図的: china/dprk/russia APT (具体名 + 主題アクターゲートで最適)・
> known_threat_followup (disabled、時間関係判定は別設計)。entity links 22,472→**7,243**。
> `PIR_SIGNAL_FIRST` は既定 ON 化済 (rollback=0)。
> **⚠ §5 の per-PIR 表には pir_jp_targeted が漏れていた** (Phase A で是正)。
> **継続 SSoT**: LLM 主題判定層・Phase A 以降の per-PIR 現況・実機検証は
> [pir_concept_llm_judge_design.md](pir_concept_llm_judge_design.md) を正とする。
> **SSoT**: 本ドキュメント (signal-first の設計原理と Phase 1-2b の記録)。
> **関連**: [pir_system.md](pir_system.md) / [routing_rule_authoring_design.md](routing_rule_authoring_design.md) /
> [subject_actor_attribution_design.md](subject_actor_attribution_design.md) / CLAUDE.md §13

---

## 1. 問題 — PIR 照合は「弱い分類器」で再分類している

### 1.1 二重分類器問題 (根本原因)

取込時、**1 回の LLM パス (`summarizer.j2`) が全記事に強い意味メタデータを付与**する
(実測充足率 / 30日 posted 7,818件):

| フィールド | 充足 | 由来 | 性質 |
|---|---:|---|---|
| `category` (vulnerability/breach/malware/apt_leak/geopolitical…) | **100%** | summarizer | 密・信頼できる背骨 |
| `socio_political_intent` (influence/espionage/subversion…) | **64%** | summarizer (Diamond intent 軸) | intent |
| `victim_country_iso` / `subject_actor_ids` / `victim_sector_canonical` | 15-19% | summarizer | **疎だが付与時は高精度** |
| `is_ransomware` (boolean 列) | — | summarizer | 存在 |

しかし PIR 照合 (`src/pir/evaluator.py:_row_match_signals` → `src/pir/persist.py:rebuild_pir_entities`)
は、この強いメタデータを **全て捨て**、title+summary+**body** の生テキストに
**キーワード substring OR** をかける、より弱い第 2 の分類器で article×PIR を再導出している。

さらに triage LLM は PIR の title+description を **判定基準として既に読んでいる**
(`src/tools/article_triage.py:_build_prompt_pir_driven`) のに、出力は `importance` 1 スカラーだけで
「どの PIR に該当したか」を破棄している (`TriageDecision`)。**高価な部分 (LLM が PIR を読む) は
既に払っているのに、価値ある出力を捨てている。**

`src/pir/models.py` 自身がこの keyword マッチャを **「LLM judgement なしの MVP」** と明記している —
最初から仮置きであり、置換が前提だった。

### 1.2 keyword-OR 表現の不可約な損失

`strong_signals` は全種を通じ純粋 OR、かつ照合対象が body を含むため:

- **多義性**: `脆弱性`(software vuln ⇔ 軍事的脆弱性)、`制裁`(APT制裁 ⇔ 一般地政学)、
  `サプライチェーン`(cyber ⇔ 物理/経済)、`leak`(APTダンプ ⇔ memory leak)、
  `doctrine`(cyber ⇔ 軍事ドクトリン)。
- **言及 ≠ 主題**: body に 1 度出るだけで無関係記事が丸ごとマッチ。
- **範囲を表現できない**: 「ransomware」の語は「政府/医療/電力に影響する国家級ランサム」を表せない。

config で語を削るのは、この損失の大きい表現の中の whack-a-mole であり **対症療法**。新 feed/話題で再発する。

### 1.3 データが示す皮肉

コーパスの **54% (4,243件) が既に `category=geopolitical`** に分類済み。監査で「誤マッチ」と
判明した Houthi 封鎖・Nord Stream・潜水艦調達・UFO 調査は、**パイプラインが既に
「地政学 (非サイバー)」と正しく判定している**のに、キーワードマッチャがその判定を無視して
cyber PIR に混入させている。**強い分類器の答えを弱い分類器が上書きしている。**

### 1.4 正解の実装が同じコードベースに既にある

**routing エンジンは既に生キーワードを捨て、`PROPERTY_CATALOG` (型付き意味プロパティの述語カタログ)
+ 条件ツリー (`all`/`any`/`not`/leaf) + 型付き演算子で config 駆動判定している**
(`src/cti/routing_rules.py`)。公開プロパティ = `category`・`victim_sector`・`actor_nation`・
`kev`・`zero_day`・`known_apt`・**`apt_leak`**・`japan_critical`・`japan_relevant`・`max_cvss`… で、
**壊れた PIR が本来使うべき軸とほぼ 1:1**。

**PIR 照合だけがこの統一から取り残された生キーワードの孤島。** 本再設計はこれを解消する。

---

## 2. 設計原則

1. **signal-first**: PIR match は、取込 LLM が算出した意味プロパティへの **述語** で決める。
   生キーワードは補強にのみ使い、単独マッチの根拠にはしない。
2. **既存エンジンを再利用 (発明しない)**: routing の `PROPERTY_CATALOG` + 条件ツリー + 演算子を
   共有する。新 DSL を作らない (`vocabulary_expansion` の「検出の種類は固定」方針に合致)。
3. **決定論 + 画面 preview 維持**: 格納済みフィールドへの述語は高速・決定論的。
   keyword が存在した唯一の理由 (LLM なしで PIR authoring を preview する) を、より良い形で満たす。
4. **SSoT / vocab 整合**: 値域 domain (categories / victim_sectors / actor_nations) は統制語彙を参照し、
   複製辞書を作らない ([vocabulary_label_architecture](vocabulary_label_architecture.md) と整合)。
5. **正直な限界の明示**: 意味プロパティで表現できない genre PIR (§5 の `pir_geopolitical_cyber` 等) は
   「マッチの問題」ではなく「PIR 定義の問題」として炙り出す。隠さない。

---

## 3. 目標アーキテクチャ

### 3.1 `ArticleFacts` 共有 read-model (中核 seam)

routing の accessor は `RoutingSignals` を読むが、PIR は post-hoc に DB `articles` 行を読む。
かつ `RoutingSignals` には `socio_political_intent` / `victim_country` / `is_ransomware` が無い。
両者を橋渡しするため、**プロパティ・カタログの唯一の読取対象を `ArticleFacts` に一元化**する:

```
ArticleFacts  (記事の意味的事実の正規化 read-model)
  ├─ from_routing_signals(signals, sq)   # routing 時 (live)
  └─ from_db_row(row)                    # PIR post-hoc / KPI / preview (persisted)
```

- **End-state (Phase 3)**: `PROPERTY_CATALOG` の accessor を `RoutingSignals` → `ArticleFacts` に
  付け替え、routing 側は `ArticleFacts.from_routing_signals` を通す (挙動不変)。PIR / routing の双方が
  **同一の条件ツリーを同一の catalog で評価**し、二重分類器が 1 つになる。
- **PoC (Phase 1) では routing の catalog / accessor を触らない**。routing の accessor は
  `RoutingSignals` 束縛のため、PoC は演算子意味論 (`routing_rules._apply_op`, 純粋関数) と
  条件ツリー文法 (`_eval_condition` の all/any/not/always/leaf) だけを再利用し、葉の解決を
  `ArticleFacts` 上の PIR-local property resolver で行う。catalog 一元化は Phase 3 で routing を
  巻き込んで実施する (PoC 中は routing の挙動をゼロリスクに保つ)。

> **⚠ 実装メモ**: `is_ransomware` は DB では `smallint` (0/1) で `boolean` ではない
> (`is_ransomware IS TRUE` は Postgres で空になる)。`ArticleFacts.from_db_row` で `== 1` 変換する。

### 3.2 PROPERTY_CATALOG の拡張 (不足プロパティを追加)

PIR が必要とするが現 catalog に無い意味プロパティを追加する (各 1 行 = 既存の増設 seam):

| 追加 property | kind | domain | 由来 (persisted) | 用途 PIR |
|---|---|---|---|---|
| `intent` | str/set | intents | `socio_political_intent` | disinfo, geopolitical |
| `is_ransomware` | boolean | — | `is_ransomware` 列 | state_ransomware, jp_company_breach |
| `victim_country` | str | countries | `victim_country_iso` | jp_*, ally_cyber |
| `feed_title` | str | (feeds) | `feed_title` 列 | attribution, agency_alert |

### 3.3 PIR match = 条件ツリーの multi-label 評価

- PIR の `strong_signals` (flat OR リスト) を **`match: <条件ツリー>`** に置換する。
  条件ツリーは routing と同一表現 (`{all:[…]}` / `{any:[…]}` / `{not:…}` / `{property,op,value}`)。
- 1 記事 × 全 enabled PIR を評価し、match した PIR id 集合を返す (multi-label)。
  集約だけ PIR 固有 (routing は単一勝者ラダー、PIR は該当全件)。
- **keyword は `keyword_list` property (統制されたカスタム語リスト) の弱補強に降格**。
  単独 clause で match させない (必ず意味プロパティ clause と AND、または `any` の一要素で
  かつ他に強い clause がある構成)。生 keyword substring の sole-match は廃止。

### 3.4 高レバレッジな共通述語: `NOT category ∈ {geopolitical}`

cyber PIR の大半の FP は「地政学 (非サイバー) 記事が多義キーワードで漏れる」こと。
signal-first では **`{not:{property:category, op:in, value:[geopolitical]}}`** を 1 clause 足すだけで、
54% を占める geopolitics コーパスの漏れを一掃できる (現 keyword-OR では表現不可能な効き)。

---

## 4. 派生 boolean の永続化 (Phase 2 の主コスト)

`kev` / `zero_day` / `apt_leak` / `japan_critical` / `known_apt` / `is_security_relevant` は
routing 時に body/metadata から算出され、**per-article 列に永続化されていない**
(`RoutingSignals` にはあるが DB `articles` には無い)。post-hoc PIR 照合でこれらを使うには:

- **列を追加**して取込時に書く (前例: `is_ransomware`・`socio_political_intent`・`subject_actor_*` は
  既に列として永続化されている)。`routing_rule_id` / `routing_reason` も永続化済 = 派生判定は
  取込時に走っている。boolean を落としているだけ。
- **backfill**: 既存行は stored フィールドから再算出できるものは再算出、できないものは
  次回 rebuild 以降 forward 適用 (eventual consistency は persist.py の日次 full-replace が担保)。
- **PG index 順序 gotcha** ([pg_schema_index_ordering](pg_schema_index_ordering.md)): 新列の
  CREATE INDEX は末尾 ALTER ADD COLUMN の後。SQLite `_SCHEMA` と `pg_schema.py` 双方更新。

**Phase 1 PoC はこの永続化を回避できる** (§6): PoC 対象 3 PIR は intent / category / is_ransomware /
victim_sector の **既に永続化済みフィールドだけ**で表現できる → スキーマ移行ゼロで着手できる。

---

## 5. per-PIR マッピング (現行 → 目標)

`kw弱` = keyword_list 弱補強 (単独マッチ不可)。`[P2]` = 派生 boolean 永続化 (Phase 2) 依存。

| PIR | 現行の主 FP 源 | 目標述語 (signal-first) | 備考 |
|---|---|---|---|
| pir_china_apt | (健全) | `actor_nation in [CN]` | 既にこれ相当。actor 名は kw弱 |
| pir_dprk_apt | (健全) | `actor_nation in [KP]` | |
| pir_russia_apt | (健全) | `actor_nation in [RU]` | |
| **pir_disinfo** | actor_nations CN/RU が中露APT全般を汚染, `IO`2字 | `intent in [influence, subversion]` (+ 偽情報/影響力工作 kw弱) | **actor_nations 撤去**。PoC |
| **pir_new_poc_vuln** | `脆弱性` が全脆弱性記事+軍事脆弱性 | `category = vulnerability` (+ PoC kw弱) | 「業界注目」は将来 `any[kev,zero_day]` [P2]。PoC |
| **pir_state_ransomware** | `ransomware` が全ランサム | `all[ is_ransomware, victim_sector in {government,healthcare,energy} ]` | **AND が無料で付く**。PoC |
| pir_jp_company_breach | 英語 ransomware/breach で非日本, feed OR | `all[ victim_country=JP, any[is_ransomware, category in {breach,incident}] ]` (+ JP feed kw弱) | 英語 kw sole-match 撤去 |
| pir_critical_infra | 重要インフラ/防衛産業 が UFO/NordStream | `any[ victim_sector in {defense,government,energy,telecom,transportation,critical_infra}, japan_critical[P2] ]` (+ ICS/OT/SCADA kw弱) | |
| pir_apt_leak | `leak` が全漏洩 | `any[ apt_leak[P2], category=apt_leak ]` (+ i-Soon/Vulkan/Conti kw弱) | bare `leak` 撤去 |
| pir_apt_attribution | `制裁` が一般地政学 | `all[ known_apt[P2], any[ feed_title in {DOJ,OFAC,警察庁,NSC}, attribution/帰属/indictment kw弱 ] ]` | [P2] |
| pir_emergency_alerts | `緊急` 汎用語 | `all[ feed_title in {CISA,JPCERT}, any[ kev[P2], Emergency Directive/ED- kw弱 ] ]` | [P2] |
| pir_general_agency_alert | alert/guidance 汎用 | `all[ feed_title in {CISA,JPCERT,ENISA,NCSC}, category=advisory ]` | category=advisory は密で clean |
| pir_geopolitical_cyber | 8ヶ国 victim OR + doctrine/geopolitical | **要 PIR 再定義** (category=geopolitical が 54%)。暫定 `all[ intent in {espionage,disruption,prepositioning}, not category in {geopolitical} ]` or D-supplement | §7 で議論。Phase 3 |
| pir_ally_cyber_event | victim US OR + advisory | `all[ victim_country in {US,GB,AU,KR,TW}, not category in {geopolitical} ]` | 非サイバー除外が効く |
| pir_minor_security_vendor_breach | `EDR` 製品名 | `category=breach` (+ security vendor/MSSP kw弱) | EDR 撤去 |
| pir_supply_chain_broad | bare サプライチェーン/supply chain | `all[ not category in {geopolitical}, 認証基盤/MSSP kw弱 or feed in {Okta,Cisco,Microsoft Security} ]` | 物理SC を category 否定で除去 |
| pir_minor_supply_chain | bare サプライチェーン/supply chain | `all[ not category in {geopolitical}, npm/PyPI/dependency kw弱 ]` | 同上。broad と重複は要整理 |
| pir_sw_supply_chain | (比較的健全) | `not category in {geopolitical}` + log4j/xz/npm package/supply chain attack kw弱 | 語が具体的で許容 |
| pir_integrated_cyber_ops | (健全, 14件) | 電磁波/サイバー物理/AI攻撃 kw弱 + `not category in {geopolitical}` | 低優先 |
| pir_known_threat_followup | (signals 空 = no-match) | 変更なし | |

---

## 6. 段階計画

### Phase 1 — PoC (スキーマ移行ゼロ, 3 PIR) — ✅ 2026-07-22 deploy 済

**実測 A/B (30日)**: disinfo 239→301 (露APT諜報の混入除去、intent=influence)・
new_poc_vuln 1663→655 (多義語「脆弱性」除去、category=vulnerability)・
state_ransomware 996→28 (AND 合成、is_ransomware AND victim_sector∈{gov,health,energy})・
pir_china_apt 54 不変 (control=keyword)。`PIR_SIGNAL_FIRST=1` で本番有効化
(compose 両サービス env + .env)。rollback = `PIR_SIGNAL_FIRST=0` or DB 版 revert。
有効化手順 = `scripts/apply_pir_signal_first_poc.py` (DB config_store に raw dict で match 注入) →
app rebuild → `rebuild_pir_entities`。**disinfo は当初 intent∈{influence,subversion} だったが
A/B で subversion が政変/テロ/軍事まで拾うと判明し influence 単独に修正** (PoC が設計を検証した例)。

以下は実装内訳:


1. `ArticleFacts` 最小実装 (`from_db_row`) + `PROPERTY_CATALOG` に `intent` / `is_ransomware` /
   `victim_country` を追加 (persisted フィールドのみ、accessor は `ArticleFacts` 経由)。
2. PIR に `match: <条件ツリー>` フィールドを追加 (旧 `strong_signals` と併存、新形が有れば優先)。
   評価器 = routing の leaf/tree 評価を `ArticleFacts` に対して回すラッパ。
3. **disinfo / new_poc_vuln / state_ransomware** の 3 PIR を新形に変換。
4. **A/B 計測**: 各 PIR の before(keyword) / after(signal) の 30日マッチ件数と、
   除外された記事のサンプルを出力 (`scripts/verify_pir_signal_first.py`)。
   基準 = FP が期待どおり落ち、健全マッチが残ること (サンプル目視 + 件数)。
5. env flag `PIR_SIGNAL_FIRST` (既定 OFF → PoC 検証後 ON) で全体 rollback 可能に。

### Phase 2 — 派生 boolean 永続化 + escalation PIR 群

1. `kev` / `zero_day` / `apt_leak` / `japan_critical` / `known_apt` / `is_security_relevant` を
   `articles` 列に永続化 (取込時に書く + backfill)。SQLite `_SCHEMA` / `pg_schema.py` 双方 + index 順序。
2. `ArticleFacts.from_db_row` にこれらを載せ、対応 PIR (apt_leak / emergency / attribution /
   critical_infra の japan_critical 枝) を変換。

### Phase 3 — 全 PIR 変換 + keyword 降格の完了

1. 残り全 PIR を条件ツリー化。生 keyword substring の sole-match を廃止 (keyword_list 弱補強のみ)。
2. `pir_geopolitical_cyber` は §7 の判断に従い再定義 or D-supplement。
3. 旧 `strong_signals` keyword 経路 (`_row_match_signals` の生キーワード分岐) を撤去。
   `PIR_SIGNAL_FIRST` を既定 ON、flag は緊急 rollback 用に一定期間保持。

---

## 7. 未決事項 — genre PIR (`pir_geopolitical_cyber`)

「国家戦略 / 地政学的サイバー分析」は意味プロパティで綺麗に切れない
(`category=geopolitical` が 54%、intent も広い)。選択肢:

- (a) **PIR 再定義**: 何を本当に求めているかを description レベルで絞る
  (例: 「敵性国家の cyber doctrine / 戦略文書 / 能力宣言」に限定 → category=policy + intent フィルタ)。
- (b) **D-supplement (限定 LLM タグ)**: 取込 LLM (triage は既に PIR context を持つ) に、
  この genre PIR だけ per-article の該当可否を出させ、`llm_pir_hint` property として persist。
  forward-only。非決定論だが preview は述語近似で代替。
- **backbone は決定論述語 (本設計) を維持**し、D は genre PIR に限定した補完に留める
  (wholesale LLM 化は非決定論で preview を壊し・履歴再タグが重いので不採用)。

Phase 1/2 の結果を見てから (a)/(b) を判断する。

---

## 7.5 実装知見 (2026-07-22, Phase 2a A/B で判明)

**actor_nation を subject-only で述語化するのは現データでは不可** (A/B で却下、実装は撤回)。

- 30日 posted 7,817件のうち **subject actor_nation を持つ行は 27件のみ** (subject 帰属充足 15%
  × 国家系辞書解決 × NON_STATE 除外)。
- 実測: `disinfo = all[intent=influence, actor_nation∈{cn,ru}]` = **0 件**。
  `china_apt` は keyword 54 → actor_nation[cn] **5** (dprk 39→8 / russia 66→12) で **recall 崩壊**。
- **理由と設計 refinement**: APT PIR の recall は「Volt Typhoon 等の**具体的アクター名**キーワード」が
  担っており、これは多義性が無く keyword が最適。**signal-first 化は多義キーワード PIR 用であって、
  具体名キーワード PIR (APT / log4j / xz) は keyword のまま最適**。actor_nation の subject-only 述語は
  精度は高いが密度が低すぎて置換にならない。
- **決定**: china/dprk/russia APT は **keyword のまま (変換しない)**。disinfo は `intent=influence` 単独の
  まま (CN/RU 絞りは subject 帰属が密になるまで保留)。組織的結合の規約 (消費者なき列/プロパティを
  作らない) に従い actor_nation プロパティ追加は撤回。

**次に有効なのは「密な永続化済みフィールド」への変換** (actor_nation の轍を踏まない):

- **ゼロ移行で可能** (feed_title 100% / category 100% / victim_country 19% は全て persisted):
  - ✅ **`pir_jp_company_breach`** (2026-07-22 deploy 済, 1647→318) →
    `all[victim_country=jp, any[is_ransomware, category∈{breach,incident}]]`
  - ✅ **`pir_ally_cyber_event`** (2026-07-22 deploy 済, 886→356) →
    `all[victim_country∈{us,gb,au,kr,tw}, not category=geopolitical]` (国別 victim の無い
    グローバル脆弱性は new_poc_vuln が拾うため意図的に対象外)
  - ✅ **`pir_general_agency_alert`** (2026-07-22 deploy 済, 1751→52) →
    `all[feed_title contains_any {cisa,jpcert,enisa,ncsc}, category=advisory]`。feed 名は DB で
    完全名 ("CISA Cybersecurity Advisories" 等) なので `feed_title` property + **`contains_any`
    (substring) 演算子** を追加 (signal_match ローカル、routing の _apply_op は非改変)。keyword
    公開/alert が地政学/軍事/脆弱性まで拾っていたのを実 advisory のみに精密化。
  - supply_chain 系 → `not category=geopolitical` で物理/経済 SC の漏れを断つ (keyword_list 弱補強と併用。
    keyword leaf 未実装のため Phase 3)
- **列永続化が要る** (Phase 2b): `kev` / `zero_day` / `apt_leak` / `japan_critical` / `known_apt` は
  routing_signals が **全記事に対し ingest 時に算出済** (= 密) だが per-article 列に未永続化。
  列追加 + backfill で `pir_apt_leak` (apt_leak) / `pir_emergency_alerts` (kev) /
  `pir_critical_infra` (japan_critical) が精密化できる。

---

## 7.6 Phase 2b (boolean 列移行) は不要と判明 (2026-07-23 A/B)

emergency/critical_infra のために派生 boolean (kev/japan_critical) を列永続化する計画だったが、
A/B で **両者ともゼロ移行で解決** = 移行不要と判明 (actor_nation と同じ data-driven 却下)。

- **critical_infra** (deploy 済, 821→431): `any[victim_sector∈{defense,government,energy,telecom,
  transportation,critical_infra}, keyword_any {ICS,OT,SCADA}]`。多義語 重要インフラ/防衛産業
  (月面防衛・欧州再軍備等の地政学を誤爆) を排す。japan_critical は intent に不要。
- **emergency** (deploy 済, 225→1): `keyword_any {Emergency Directive, JPCERT 緊急}`。
  **真の緊急指令は本来レア (30d=1)**。kev boolean は intent 不一致 = KEV/ICS advisory は
  ルーティンなので agency_alert が拾う (emergency=緊急指令 とは別物)。

**結論**: 派生 boolean (kev/japan_critical/apt_leak) は routing/triage が live 算出して使うもので、
どの残存 PIR の照合 intent にも列永続化は不要。スキーマ移行 + backfill のコスト/リスクを回避した。
(将来 triage/routing/分析で列参照したい別目的が生じたら再検討。boolean は body regex +
KEV カタログ CVE 照合由来で大半 backfill 可能。)

---

## 8. 検証・互換・却下案

### 検証
- **A/B behavior-preservation**: `verify_pir_migration.py` パターンを踏襲。
  基準 = 健全 PIR (china/dprk/russia APT) の件数不変、FP 主体 PIR は件数減 + サンプル目視で
  「落ちたのは非サイバー/言及のみ」であること。
- **fill-rate 監査登録**: 新 property / persist 列を `src/ui/services/fill_rate_audit.py` の METRICS に登録
  (有機的結合監査の規約 3 点セット: 消費者・fill-rate 登録・SSoT 参照)。
- **ラベル SSoT**: intent/category/sector/country/actor_nation は既存の統制語彙を参照 (複製しない)。

### 互換・rollback
- env `PIR_SIGNAL_FIRST=0` で旧 keyword evaluator に完全 fallback (Phase 1/2 中)。
- PIR config は DB 版保存 (app_config_versions) で任意版に revert 可能。
- 旧 `strong_signals` は Phase 3 完了まで温存 (新形が無い PIR は旧経路)。

### 却下した代替案
- **A: config 衛生化 (keyword を削る)** — 損失の大きい表現内の whack-a-mole。多義性/範囲は不可約。**対症療法**。
- **B: AND 合成を keyword に足す** — 表現は広がるが依然生キーワードで、多義性 (脆弱性/制裁/leak) は残る。
  signal-first にすれば AND は無料で付くので B 単独は不要。
- **D: 取込 LLM で全 PIR を wholesale タグ付け** — 最高精度だが非決定論で PIR authoring の preview を壊し、
  履歴再タグが重い。genre PIR 限定の補完 (§7) に留める。

---

## 9. 実装チェックリスト (Phase 1)

- [ ] `ArticleFacts` (from_db_row 最小) + PROPERTY_CATALOG に intent/is_ransomware/victim_country 追加
- [ ] `intents` / `countries` domain を vocab に接続 (既存があれば参照)
- [ ] PIR schema に `match` 条件ツリー追加 (models.py, extra 併存)
- [ ] signal-first evaluator (条件ツリー × ArticleFacts) + `PIR_SIGNAL_FIRST` flag 分岐
- [ ] disinfo / new_poc_vuln / state_ransomware を新形に変換 (DB 版保存)
- [ ] `scripts/verify_pir_signal_first.py` (A/B before/after + サンプル)
- [ ] unit test (述語評価 / multi-label / keyword 弱補強が単独マッチしないこと)
- [ ] ruff / mypy strict / pytest 通過
