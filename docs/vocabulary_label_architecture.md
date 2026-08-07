# 語彙ラベル・アーキテクチャ再設計 (Vocabulary/Label Architecture)

> ステータス: **設計中 (2026-07-21 起草)**。実装前レビュー用。
> 上位方針: CLAUDE.md §11/§12 (UI)、メモリ `ui-copy-policy-2026-07-20`、
> `organic_integration_audit_2026_07_12` (供給網無監視 / SSoT 非強制) を継承・是正する。

## 0. 要約 (BLUF)

UI に出る「コード的な値の日本語ラベル」は、これまで **frontend の静的翻訳テーブル
(`frontend/src/utils/labels.ts` ほか) + 未知キーの原値 fallback** で一律に扱ってきた。
この「回収要領」は **値の性質差 (静的 / 動的 / データ発) を無視した単一機構** であり、
ドリフトを生む機構 (値とラベルが独立に進化する2箇所で著される) を除去せず、症状の
見え方だけを均していた。結果として 350 件監査 → 130 件再監査と **同型の欠陥が再発**した。

本設計は方針を反転する: **「backend が源泉の値については、ラベルを値の源泉に束ね、
データとして配信する。frontend の静的テーブルは *真に frontend 固有の静的テキスト
(ボタン/見出し等)* にのみ縮小する。」** これは新機軸ではなく、本ツールが `channels`
(`useChannelMeta`) で既に成功している唯一ドリフトしない機構を全域へ一般化するもの。

移行後、backend-由来ラベルの **frontend 第2コピーは消滅**し、値↔ラベルの整合は
**同一言語 (Python) 内で強制可能**になる (現状の言語横断は型でもテストでも原理的に
検証不能だった)。

---

## 1. 問題の定義

### 1.1 ラベル表示の鎖とドリフト点

```
backend が値集合を定義 (Python 定数 / Literal / DB / LLM)
   → 値がデータとして frontend に流れる
   → frontend が「値 → 日本語ラベル」を写像して描画   ← ドリフトはここで起きる
```

ドリフトの2形態:
- **Layer 1 (一貫性)**: 同じ値が場所によって別訳 (opinion=「意見」/「論説」)。
- **Layer 2 (完全性・鮮度)**: backend が値を増やした/変えたのに frontend に正しい訳が無い。

### 1.2 現行「回収要領」の2つの構造的誤り

1. **異種の問題を単一機構で処理した。** 値の性質 (§2) が根本的に違うのに、全部を
   「frontend 静的テーブル + 沈黙 fallback」で扱った。動的な値 (§2 C/D) に静的マップを
   置くのは fragile 以前に **カテゴリエラー**で、必ずドリフトする。
2. **「値ラベル」と「UI 文章 (chrome)」を混同した。** 過去監査は *ドリフトする値ラベル*
   と *ドリフトしようがない編集文言 (triage→選別 等)* を同じ「監査して labels.ts に集約」
   で扱った。前者はアーキテクチャ問題、後者は文章品質問題で、解法が異なる。

### 1.3 なぜ型・テストで防げなかったか (構造的核心)

値の SSoT は backend (Python/DB)、ラベルの SSoT は frontend (TypeScript)。
**TS は `KNOWN_ARTICLE_CATEGORIES` を物理的に参照できない**ため、1言語内の型検査だけでは
「backend が値を足し frontend が訳を忘れた」を検出できない。`label()` の原値 fallback は
**空欄でもエラーでもなく“生の英語が黙って出る”** ため、欠落が可視化されない。
→ 本設計の肝は **ラベルを backend に同居させ、値↔ラベルの整合を同一言語で強制可能にする**こと。

---

## 2. 値種別インベントリ (A/B/C/D)

UI に出る「コード的な値」を **変化契機とラベルの持ち主**で4種に分ける。各種で正しい機構が違う。

| 種別 | 定義 | 変化契機 | ラベルの正しい持ち主 | 静的テーブルで守れるか |
|---|---|---|---|---|
| **A** 閉じたコード enum | コード所有の小さな固定集合 | コード変更 (PR) のみ | 値の定義と同居 | 原理可・ただし別コピーは事故源 |
| **B** 設定/分類語彙 | config/DB の語彙、運用者編集可 | config 編集 (無デプロイ) でも | 値と一緒に流す | 部分的 (コード側追加のみ検知) |
| **C** 利用者エンティティ | 利用者が自由に作る、ラベル=自身のフィールド | 実行時・無制限 | エンティティ自身のフィールド | **不可能 (ダイナミック)** |
| **D** データ発・自由記述 | 散在リテラル/LLM 出力、正規定義なし | 実行時・正規集合なし | 上流で定義付与 or 汎用整形 | **不可 (照合対象が存在しない)** |

> 具体的な所属 (どのドメインが A〜D か・現行マップの所在・backend 源泉) は §2.1〜§2.4 に列挙。
> 実測サマリ: backend の UI 露出 `Literal` enum ~44、frontend の value→label 構築物 ~87
> (COLOR 系 ~16 は別)。ドリフト顕在ドメイン 8 (§2.5)。

### 2.1 A: 閉じたコード enum — 棚卸し

UI 露出の主要 A ドメイン。**正規定数「無」= 移行時に導入が必要**(現状 Literal 型のみ or 散在)。

| ドメイン | 値集合 | backend 源泉 | 正規定数 | 現行 frontend・ドリフト |
|---|---|---|---|---|
| importance | high/medium/low (+表示 unknown) | `config_loader.py` Literal + `articles_feed.py` `_ALLOWED_IMPORTANCE` | **無** | labels.ts(SSoT) + routingLabels(unknown 欠) + ThreatsTab override + News/registry の option 2 種で文言差 |
| run status | running/succeeded/partial_failure/failed | `records.py:32` `RunStatus` | **無**(Literal) | labels.ts |
| article status | 9 値 | `records.py:33` `ArticleStatus` | **無**(Literal) | labels.ts |
| editorial stance | factual_report/analytical/opinion/propaganda/unknown | `llm_routing_flags.py:17` + **重複** `pages.py:1282 _VALID_STANCES` | 有(2 コピー) | labels.ts + EditorialView alias/色 |
| trigger | scheduler/manual/cli | `records.py:64` `triggered_by` | **無**(Literal) | labels.ts に **reactive/recovery** も(backend Literal に無い→要照合) |
| health status | ok/warning/error | `health.py:20` `CheckStatus` | 有(named alias) | labels.ts に **unknown** 追加(要照合) |
| confidence(ICD203) | high/**moderate**/low | `estimate.py:25` + `passes.py:56 _VALID_CONF` | 有 | **二重スケール**: diamond `high/medium/low`。frontend 8+ コピー・`中` vs `中確度` |
| verdict(ACH) | leading/viable/refuted/unscored | `estimate.py:68` | **無**(Literal) | SynthesisTab |
| category | 12 値 | `config_loader.py:548 KNOWN_ARTICLE_CATEGORIES` | **有** | labels.ts(SSoT)+routingLabels(ransomware 有/phishing,recap 欠)+registry で breach/geopolitical 別訳 |
| category groups | vuln/threat/incident_breach | `articles_feed.py:84 _CATEGORY_GROUPS` | 有 | labels.ts |
| article_type | breaking/advisory/recap/tutorial/research/press/opinion | `article_classifier.py:21` + inline dup | 有(+inline) | routingLabels |
| assigned_by | seed/anchor/nation/token/llm/standing | `situation_store.py:34 AssignedBy` | 有 | LedgerView(SQL コメントは standing 欠→doc drift) |
| delta_type | opened/…/closing/no_change(10) | `render.py:136 _DELTA_JA` de-facto | 部分(frozenset 無) | api/situations.ts(SSoT) |
| transport | rss/sitemap/html_scraper | `source_store.py:32 TransportT`+`_TRANSPORTS` | 有 | Wizard/Subscriptions で subset dup(atom も) |
| routing property/op/flags | 多数 | `routing_rules.py PROPERTY_CATALOG`(強 SSoT) | 有 | routingLabels(表示専用) |
| threat tier / activity / capability | critical/high/moderate/watch 他 | `actor_threat.py:71-73` | **無**(Literal) | ThreatsTab(tier は**英語維持=非対象**、activity は和訳) |
| socio-political intent | 12 値 | `diamond_model.py:43`+frozenset+`INTENT_LABELS_JA` | **有(模範)** | utils/diamond.ts(SSoT) — label が値と同居する良例 |
| source reliability tier | official/research/news/social/state_media/unknown | `source_basis.py:34 SourceTier` | 有 | ConfidenceBadge 系 |
| ACH polarity / attribution_basis / claim_type | 3 / 9 / 3 | `estimate.py`+`passes.py` frozensets | 有 | LedgerView/SynthesisTab(claim_type は point_event vs discrete_event drift) |
| job kind/schedule/protection | 各 3 | `job_registry.py:29-33` | **無**(Literal) | jobs/JobDetailPanel |
| SourceType / extract method / STIX mode / log stream | — | `config_loader.py` / `records.py` Literal | **無**(Literal) | 一部のみ UI 露出 |
| 合成・予測・jpci 系(単一コピー低リスク) | ACH hypothesis(14)/forecast verdict/direction/scope/synthesis section/entity type/matched-via/event date basis/jpci stage・domain・precision・status/pmesii role | 各 `src/synthesis` `src/cti` 由来 | 多くが Literal のみ | 各 tab に単一マップ(重複少・後半 Phase で移行) |

> 注: `SSE stream status`(connecting/open/done/closed)・`day of week`・`holdings key`・widget titles は
> **client 側 UI 状態/メタ**であり backend 値源泉が無い → §2.6(frontend 固有)扱い。

### 2.2 B: 設定/分類語彙 — 棚卸し

運用者が編集しうるが canonical 集合が存在。**ラベルの一次情報が config/DB にある → そこから配信**(第2定義禁止)。

| ドメイン | 値集合 | backend 一次情報 | ラベル現状 | 現行 frontend・ドリフト |
|---|---|---|---|---|
| victim sector | ~21 canonical(+uncategorized) | `config/victim_sectors.yaml`(runtime=DB config_store) | config に有 | `geo/sectorColors.ts SECTOR_LABELS`(第2定義・要配信化) |
| victim/involved country | ISO2 ~120 | `config/countries.yaml` | config に有 | `utils/countryLabels.ts`(~120 の第2定義) |
| actor sponsoring nation | 18 コード観測 | `actor_aliases.yaml` per-actor + `diamond_model.py:97 NATION_LABELS_JA` | **11/18 のみラベル** | diamond.ts(SSoT) + routingLabels(派生) + **ActorDetail.tsx 独立コピー** |
| actor family | typhoon/panda/lazarus… | `actor_aliases.yaml families:` + `actor_editor.py list_families()` | config に有 | ActorDetail |

### 2.3 C: 利用者エンティティ — 棚卸し

**ラベル=エンティティ自身のフィールド。既に配信済みが多数(=正しい)。静的マップを書かないのが鉄則。**

| ドメイン | 配信 | frontend consumer | 備考 |
|---|---|---|---|
| channel | GET `/api/v1/channels` `{id,label}` | `useChannelMeta`/`ChannelChip`(SSoT) | **例外2件が固定マップに逸脱**: NewsPage:44 / registry:139(custom 欠・改名追随せず→是正対象) |
| PIR | GET `/api/v1/pir` `{id,title}` | `pirApi.list()` | label=title/description |
| actors / aliases | GET `/api/v1/actors`, `/api/v1/actor-options` | `pagesApi.getActors()` / `fetchActorOptions()` | 別名解決も配信 |
| sources / subscriptions | feed 行に feed_id + 表示名 | Subscriptions | — |
| jobs / model-tiers / match-lists / affected-vendors | 各 GET で `{id,label}`/`{tier:{label}}` 等 | 各 api | 既に同伴配信 |

### 2.4 D: データ発・自由記述 — 棚卸し

正規定数が無い **or 複数の分散定数があり SSoT 未指定**。**(1) 閉じているものは正規定数を
1つに定めて A へ昇格/統合、(2) 真の自由記述は humanizer + 未知監査面。**

> **gap 検証の補正**: period_type は「定数無」ではなく **複数の分散定数**が実在する
> (`synthesis/runner.py:22 _VALID_PERIODS` / `spotlight/models.py:10 SpotlightPeriod` /
> `pir/models.py:23 SpotlightWindow` / `job_recovery.py:34 Cadence`)。真の問題は *不在* では
> なく **SSoT 未指定の多重定義**。→ D ではなく **A の「統合」対象**として扱う (下表は是正済み)。

| ドメイン | 観測値 | backend 現状 | 対処 |
|---|---|---|---|
| period_type | daily/weekly/monthly | **複数定数** (_VALID_PERIODS/SpotlightPeriod/SpotlightWindow/Cadence) | **A 統合**(1 定数に集約)・frontend ≥6 コピー撤去 |
| detected_via | direct/html_link/common_path/subdomain/category_path(+visual/user_intent) | コメントのみ | **A 昇格** |
| data.kind(actor) | group/organization/contractor | 定数無 | **A 昇格**(per-field 定数) |
| data.kind(proposal) | add_alias/… | 定数無 | **A 昇格** |
| taxonomy proposal tier/status/type | tier_1..3 / pending.. / pattern_1..8 | free str(SQL コメント) | **A 昇格** |
| actor-update proposal type/status | mitre_new_actor 他 / pending.. | free str | **A 昇格** |
| situation rel_type | same_actor/same_campaign/shared_nation/temporal_sequence | コメントのみ | **A 昇格** |
| situation detection decision | assigned/opened/rejected/unassigned | コメントのみ | **A 昇格** |
| job last-run status | succeeded/failed | コメントのみ | run status と統合検討 |
| LLM 由来の自由記述・legacy(grok_x_signal_* 等) | 不定 | LLM 出力 | **humanizer + 未知監査面**(静的化しない) |

### 2.5 現行 frontend 静的マップの所在 (移行対象・重複)

frontend/src の value→label 構築物: **TEXT 系 ~87 / COLOR 系 ~16 / ドリフトあるドメイン 8**。
labels.ts(9 マップ)は全体の小島にすぎない。**重複・逸脱の要注意箇所**:

- **confidence — 最悪(8+ コピー)**: `medium`(labels.ts/diamond/jpci pill/ConfidenceBadge) vs `moderate`(LedgerView/SynthesisTab/SynthesisProse/JpCiBoard)、`中` vs `中確度`。GeoConfidence(thin/medium/rich)は別 enom だが同語。
- **category**: routingLabels に `ransomware`(非 canonical)・`phishing`/`recap` 欠。registry option で `breach`/`geopolitical` を別訳。
- **importance**: `unknown` の有無が labels.ts/routingLabels/ThreatsTab で不一致。News/registry の option で文言差。
- **nation**: `ActorDetail.tsx:15` が diamond.ts SSoT の独立コピー(drift 源)。
- **channel**: 上記2固定マップが served SSoT を迂回。
- **transport / period / day-of-week**: subset dup が複数。
- COLOR マップ(~16)は enum キーだが**ラベルではない**ため本再設計の対象外(別途 tone-token 統一は任意)。

### 2.6 再設計対象外 (frontend 固有の静的テキスト)
UI chrome (ボタン Save/Cancel、見出し、ヘルプ文、jargon→平易化された編集文言) は
**backend の値源泉が無い純 frontend テキスト**でドリフト機構が存在しない。アーキ変更
(backend 送出) はむしろ悪化。→ **用語集 (glossary) + レビュー規律**で扱う。`labels.ts`
は削除せず**この用途へ縮小**する。脅威ティア (Critical/High/…) と CTI 枠組み用語
(PIR/IOC/APT/ATT&CK/ACH/Diamond/ICD 203 等) は英語/原語維持 (メモリ規約)。

### 2.6.1 ラベル言語の方針 (2026-07-21 改訂 — CTI 語彙は原文維持)

**全部を日本語にしない。CTI アナリストが通常 英語で読む語彙は原文の方が読みやすい。**
これは既に脅威ティア (Critical/High/Moderate/Watch) を英語維持しているのと整合し、
記事 importance だけ 高/中/低 に訳すのはむしろ不整合だった (メモリ `ui-copy-policy-2026-07-20`
の rule 4 「importance→高/中/低」を本改訂で上書き)。

- **英語維持 (重大度/確度/能力の ordinal スケール)**: importance = **High/Medium/Low**
  (未判定は **Unknown**)、confidence(ICD 203) = **High/Moderate/Low**、
  capability band = **High/Medium/Low/Unknown**。表記は脅威ティア/CVSS に合わせ **Capitalized**。
- **英語維持 (既存)**: 脅威ティア、CTI 枠組み用語 (PIR/IOC/APT/ATT&CK/TTP/C2/CVE/KEV/0day/
  BLUF/ACH/Diamond/ICD 203/tradecraft 等)、RSS/Atom/sitemap。
- **日本語 (運用・記述系)**: run status (成功/失敗/実行中/一部失敗)、article status、trigger、
  health、period_type、delta_type、situation status、stance (事実報道/分析/…) 等。
- **判断保留 (現状 日本語のまま、必要なら英語化)**: activity_state (急増/活動中/静穏/休眠)、
  verdict、article_type。descriptive 寄りなので当面日本語。
- **設計上の利点**: vocab を SSoT 化したので、**ラベルの言語変更は backend vocab の 1 行**
  (8 ファイル追う必要なし)。方針は安価に調整できる。
- `activity_state`/`capability_band` は脅威ティアの兄弟軸。capability は英語化 (上記)、
  activity は当面日本語。「Tier だけ英語」の誤適用に注意。

### 2.7 gap 検証で判明した追加ドメイン・補正 (adversarial review 反映)

**(a) 棚卸し漏れ (追記)**
- **importance は ≥8 コピー** (§2.1 の 6 に加え inline 三項が `SynthesisTab.tsx:588` /
  `HistoryPage.tsx:271`)。
- **PIR importance は別語彙** (`auto` を追加): `PirDetailPage.tsx:19` / `PirListPage.tsx:17` /
  `PirEditPage.tsx:274`、backend `pir/models.py:22 RoutingImportance` + `compiler.py:156
  _KNOWN_IMPORTANCE`。**記事 importance と統合してはならない** (別 vocab `pir_importance`)。
- **capability_band** は `ThreatsTab.tsx:671` が `IMPORTANCE_LABELS` を流用中だが **別 enum**
  (`actor_threat.py:72`)。独立 vocab 化 (importance 流用をやめる)。
- **actor kind** の inline dup: `ThreatsTab.tsx:267/357` が `ACTOR_KIND_LABEL` を重複。
- **Grok session status** (`GrokSessionCard.tsx:114/120`: ok/session_expired, success/failed) 未棚卸し → A。
- **subscription GROUP_META / LOW_CONTRIB_LABELS** は **frontend 派生の UI グルーピング** (backend 値源泉なし) → §2.6 (chrome) 扱いに確定。
- period_type は ≥6 コピー (`SynthesisTab.tsx:65/68/70` 追加)。

**(b) 分類・事実の補正**
- **verdict はドリフト事例** (clean-A ではない): backend `estimate.py:68
  {leading,viable,refuted,unscored}` に対し `SynthesisTab.tsx:319` は
  `leading→主説/refuted→反証/neutral→中立` を描画。`neutral` は **backend に無い値**、
  `viable`/`unscored` は未処理。→ **backend 側 producer ドリフトを先に是正**してから vocab 化。
- **confidence は 2 つの別スケール** (`estimate.py:25 {high,moderate,low}` vs
  `diamond_model.py:35 {high,medium,low}`)。→ 単一 vocab に押し込めず **名前空間分離**
  (`confidence_icd203` / `confidence_diamond`)。`medium` vs `moderate` の不一致は
  **label 層では解けない backend 側の統一課題**として別途処理 (§4.6 参照)。

**(c) 正直な限界 (over-claim の是正)**
配信機構は **「frontend がラベルを忘れる」ドリフト類**を構造的に消すが、**backend 側の
producer 分裂** (confidence の medium/moderate、verdict の neutral) は **設計が可視化する
だけで自動解決しない**。レジストリはどの producer を正とするか **人が決める**必要がある。

---

## 3. 設計原則

1. **ラベルは値の源泉に束ねる。** backend 由来の値について、frontend は第2の SSoT を
   持たない。ラベルはデータとして値と共に流れる (`channels` パターンの一般化)。
2. **1機構に統一する。** B/C は実行時に変わるため *配信機構が必須*。ならば A も同じ配信
   機構に載せ、機構を1つに保つ (複数機構が過去のカテゴリエラーの温床)。
3. **整合は同一言語で強制する。** 値集合 (Python) とラベル (Python) を突き合わせる
   backend テストで、ラベル欠落を CI で赤にする。言語横断の脆いパースはしない。
4. **未知は隠さず可視化する。** 照合できない値 (D) は原値を黙って出さず、汎用整形しつつ
   監査面に「ラベル無しで観測された値」を surfacing する (fill-rate 監査と同型)。
5. **presentation と domain を混同しない。** 純 UI 文言は frontend 用語集で扱い、値ラベルは
   backend 語彙に置く。i18n は非目標 (単一運用者・日本語のみ)。

---

## 4. 目標アーキテクチャ

### 4.1 backend: 語彙レジストリ (単一 SSoT)

新モジュール (例 `src/ui/vocabularies.py` または `src/vocab/`) に、閉じた/設定語彙を
**順序付き `value → VocabItem(ja_label, …)`** として定義する `Vocabulary` 抽象と
`_REGISTRY: dict[str, Vocabulary]` を置く。

- **A**: 値集合の SSoT (既存 `KNOWN_ARTICLE_CATEGORIES` / `_VALID_STANCES` / status の
  `Literal` 等) は現位置に残し、レジストリは **その値集合をキーにラベルを与える**。
  起動時 + CI で `registry.keys() == 正規値集合` を assert。
- **B**: ラベルの一次情報が既に config/YAML (victim_sectors.yaml / countries.yaml) や DB に
  ある場合はそこから構築する (第2定義を作らない)。
- 参照 SSoT の重複禁止: intent/nation=`src/cti/diamond_model.py`、sector=
  `config/victim_sectors.yaml`、国=`config/countries.yaml` 等 (CLAUDE.md §7 の 3 点セット規約)。

`VocabItem` 例: `{ value, ja_label, order, description?, deprecated? }`。

**gap 検証で確定した2つの必須制約:**
- **語彙は名前空間で持つ (単一 domain 名に押し込めない)。** 同じ「confidence」でも
  `confidence_icd203 {high,moderate,low}` と `confidence_diamond {high,medium,low}` は
  **別 vocab**。同名 domain に両方入れると `registry.keys() == 正規値集合` が
  どちらの Literal に対しても偽になる。producer ごとに vocab 名を割る。
- **強制は「名前付き正規定数」がある vocab だけ即可能。** `typing.get_args` は
  *named な* `Literal` alias (RunStatus / ArticleStatus / EditorialStance / SpotlightPeriod 等)
  にしか効かない。inline Literal (importance の `_default_importance_map` 戻り型、verdict の
  フィールド Literal) や frozenset 無しの de-facto 定数 (delta_type=`_DELTA_JA` keys のみ、
  detected_via / actor-kind / proposal 系 / situation rel_type・decision) は、**その vocab の
  移行 step 0 で「名前付き定数を1つ mint する」**のが前提。定数が出来て初めて強制が効く。

### 4.2 配信: `GET /api/v1/vocabularies`

全登録語彙を `{ name: [ {value, label, order, …}, … ] }` で返す **単一集約エンドポイント**。
- `src/ui/api/channels.py` と同じ router/pydantic/READ_ONLY 許可 (GET) の作法に合わせ、
  `src/ui/app.py::create_app()` で lazy import + `include_router` 手動登録。

**gap 検証で判明した最重要リスク: first-paint の生値フラッシュ (現状に無い退行)。**
現状 `labels.ts` は **同期 import** で初回描画時にラベルが即座に揃う。素朴に fetch へ移すと、
記事一覧の全行で fetch 解決前に生の `high`/`apt`/`posted` が一瞬出る = **今存在しない退行**。
`useRuntimeFlags` (staleTime:Infinity) は `data || default` を返すだけで **描画を待たせない**。
→ 対策を設計に含める:
- **boot gate**: アプリの本体描画を **`/vocabularies` の解決までゲート**する
  (localhost は sub-10ms、shell/spinner を出す既存 UX の延長で体感ゼロ)。`main.tsx` /
  `AppShell` の最上位で当該 query 未解決なら本体を出さない。
- **graceful degradation**: エンドポイント失敗時は humanizer (§4.5) で描画を続行し
  アプリを止めない (生値の snake_case を直出ししない)。
- これにより codegen の唯一の利点 (初回同期可用性) が boot gate で相殺される → §5 の
  「配信1本」判断が維持される。boot gate の待ちが問題化した場合のみ **A の build 時 embed**
  を fallback とする (localhost では不要の見込み)。

**キャッシュ無効化 (B は複数経路):**
- 通常の config 保存 (channels 等) → 保存後に `["vocabularies"]` を invalidate。
- **sector は taxonomy-review 承認経路**で `config_store` が変わる (単純 config 保存では
  ない)。承認ハンドラでも `["vocabularies"]` を invalidate しないと新 sector が reload まで
  生値になる。→ **両経路で invalidate を配線** (現状どちらも未配線)。

### 4.3 frontend: `useVocab` フック

`/api/v1/vocabularies` を React Query (`staleTime`) で読む `useVocab(name)` /
`useVocabLabel(name, value)` を新設。`(value) => label` を返し fallback は §4.5 の humanizer。
**A/B ドメインの静的マップ (`labels.ts` / `routingLabels.ts` の値マップ) を全廃**し
これに置換。frontend は backend 由来ラベルの写像を1つも持たなくなる。

> **render 外 (純ヘルパ関数) からの解決** (Phase 1 実装で確立): tooltip 文字列生成など
> hook を呼べない箇所は、共有 `queryClient` の cache を同期読みする **非フック アクセサ
> `vocabLabel(name, value)`** を使う (`hooks/useVocab.ts`)。boot gate 済みなので cache は
> 常に暖まっている。render 内は `useVocab`/`useVocabMap`/`useVocabOptions`、render 外は
> `vocabLabel` の 2 経路 (どちらも未知値は原値 fallback)。DI で resolver を引数に流す方式は
> 呼び出し側改変が増えるため非採用。

> **文脈依存の文言は「ドリフト」ではない (centralize しない)。** フィルタ選択肢の
> 「全重要度」「高のみ」(NewsPage/registry) や、taxonomy 提案確度の `高/中/低`
> (対して他所は `高確度/中確度/低確度`) は **意図的な文脈差**であり canonical ラベルへ
> 平坦化しない。vocab は *canonical ラベル* を提供し、consumer 側は正当な文脈でのみ
> 別文言を使ってよい (これは chrome=§2.6 の範疇)。§2.5 の「統一対象」から除外する。

### 4.4 C: 利用者エンティティ

`channels` (`useChannelMeta`) を規範とする。C は **静的マップを一切書かない**。各エンティティ
API が `{id, label/name/title}` を返し、frontend はそれを描画する。§2.3 で各 C エンティティ
(sources / PIR / alias / actor 等) がラベルを同伴しているか確認し、欠けていれば是正。
「Page ↔ tabs 内 View」等での固定マップ再生を lint/レビューで禁止 (過去 grok_daily 再生の前例)。

### 4.5 D: データ発・自由記述

- **実は閉じている値** (assigned_by=anchor/nation/standing/token/seed 等) → **上流で正規定数化
  して A/B に昇格**しレジストリ登録 (これが真の是正。メモリ「生値残置」の恒久対処)。
- **真に自由記述** (LLM 出力・legacy `grok_x_signal_*`) → **humanizer** (slug/snake_case →
  可読) で内部トークンを直出ししない + **未知値の監査面**に surfacing。

### 4.6 強制機構 (再発を構造的に不可能にする)

1. backend pytest: 各 A/B 語彙で `registry.keys() == 正規値集合` を assert (同一言語なので
   確実)。**backend に値を足してラベルを忘れると CI が赤**。ただし §4.1 の通り
   **名前付き正規定数がある vocab のみ即適用**。定数が無い vocab は「step 0 で定数を mint
   →その時点から強制」の順で移行する (定数無しのまま強制テストは書けない)。
2. frontend guard (軽量): 移行済みドメインの静的マップが `labels.ts`/`routingLabels.ts` に
   再生していないことを grep で検査。
3. 「frontend がラベルを忘れる」は **frontend にラベルが無いので原理的に起きない** (唯一の
   backend 定義から来る)。

> **本機構が閉じないドリフト (正直な限界)**: この強制は「frontend 重複」を消すが、
> **backend 側の producer 分裂**は消さない。例: confidence の `medium`(diamond) vs
> `moderate`(ICD-203)、verdict の `neutral`(frontend が描画するが backend に無い)。
> これらは vocab を名前空間分離しても *どちらを正とするか* は人的判断で、**別タスクの
> backend 統一**として §7 の各 Phase で個別に是正する (label 層では解決不能)。

---

## 5. 却下した代替案 (再提案時のコンテキスト保持)

- **ビルド時 codegen (backend 語彙 → TS 生成)**: 却下 (ただし条件付き fallback として保持)。
  B/C は実行時に変わるため *配信機構が必須* で、codegen を A 専用に足すと **第2機構**が復活する。
  codegen の唯一の利点 (初回同期可用性) は §4.2 の **boot gate** で相殺されるため、配信1本に
  統一する。**例外**: boot gate の待ちが localhost でも体感問題化した場合に限り、A の
  **build 時 embed** を fallback として復活させる (現状は不要の見込み)。
- **backend が chrome 文言まで全配信**: 却下。FE の編集文言を BE デプロイに結合し、値源泉の
  無い UI テキストに不適。
- **labels.ts 維持 + 言語横断カバレッジテスト (増分案)**: 却下。SSoT が2つ残り、TS を
  テキストパースする脆い検査で、ドリフト発生源を除去できない。

---

## 6. 非目標 (Out of Scope)

- i18n / 多言語 (単一運用者・日本語のみ)。
- UI chrome 文言の backend 移設 (§2.6・用語集で扱う)。
- 脅威ティア (Critical/High/Moderate/Watch) と CTI 枠組み用語の和訳 (原語維持)。
- Discord 投稿側の文言 (UI とは別系統)。

---

## 7. 段階移行計画

| Phase | 内容 | 完了条件 |
|---|---|---|
| **1 基盤+実証** | 語彙レジストリ + `/api/v1/vocabularies` + `useVocab` + **boot gate** + 強制テスト。**代表 A enum 1件** (run status = 名前付き定数あり) を E2E 移行し静的マップ削除 | mypy/pytest/vite build/ruff green・UI で当該ラベル表示が不変・**first-paint フラッシュ無し**・強制テストが欠落で赤・boot gate 失敗時 humanizer で継続 |
| **2 A 全移行** | 残り A (stance/article status/trigger/health/category/category groups/article_type + capability_band) を移行。`routingLabels.ts` は**値マップ (ARTICLE_TYPE/IMPORTANCE/CATEGORY/NATION) のみ撤去**。**importance と pir_importance は別 vocab**。`labels.ts` の値マップ撤去 | 各静的マップ全廃・強制テスト網羅 |
| **2.5 backend 統一 (先行是正)** | confidence の medium/moderate 統一 or 名前空間確定、verdict の `neutral` 是正、period_type 定数の SSoT 集約、editorial stance の二重定義解消 | producer 分裂の解消 (vocab 化の前提) |
| **3 B 移行** | sector/country/nation/family を config 一次情報から配信。**両無効化経路** (config 保存 + taxonomy 承認) を配線。nation は 11/18 ラベルギャップを埋める | 二次定義ゼロ・運用者編集が両経路で反映 |
| **4 D 是正** | 閉じた D (detected_via/actor-kind/proposal 系/situation rel・decision/Grok session status) を正規定数化して A 昇格 + humanizer + 未知監査面 | 生値の直出し撲滅・監査面に未知値が出る |
| **5 C 監査+収束** | C 各エンティティがラベル同伴か確認・**channel 逸脱2件** (NewsPage:44/registry:139)・**nation ActorDetail 独立コピー**を是正。静的マップ再生 guard。`labels.ts` を chrome 用語集へ縮小 | C の静的マップ皆無・labels.ts の残存が chrome のみ |

**routing OP/FIELD/FLAG は移行対象外** (値ラベルでなく DSL メタ、SSoT は backend
`PROPERTY_CATALOG`、rule editor は既に catalog 参照、`routingLabel` は read-only 要約専用)。
配信化するなら PROPERTY_CATALOG 由来の別経路で (value vocab と混ぜない)。
legacy 非 canonical ルール値 (`ransomware` 等) は canonical vocab に含めないため
**`(id)` fallback 表示が既定になる** (トレーサビリティ設計として許容)。

各 Phase は独立にデプロイ可能。Phase 境界で `mypy src/ tests/` / `pytest` / `vite build` /
実 UI 目視 (実 API レスポンスに生値が無いこと) を確認する。

---

## 8. 検証観点 (ヌケモレ防止)

- **棚卸し網羅性**: §2 の A〜D 表が frontend 全 label マップ (§2.5) と backend 全値源泉を
  1対1で被覆していること。未分類ドメインが無いこと。
- **重複解消**: `routingLabels.ts` 等の重複マップが移行で消えること (既に drift 済み)。
- **強制の実効性**: 「backend 値追加 → ラベル未登録 → CI 赤」を意図的に再現して確認。
- **未知の可視化**: D の未知値が黙って原値表示にならず監査面へ出ること。
- **非対象の保全**: chrome 文言・脅威ティア・CTI 用語を誤って移設していないこと。
- **実データ検証**: デプロイ後に実 API を叩き、生 enum/canonical id が残っていないか目視
  (メモリ教訓: grep 監査ではデータ由来の生値を取り逃す)。
