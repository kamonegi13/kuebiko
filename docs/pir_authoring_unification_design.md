# PIR authoring 言語の一本化 (2026-07-23)

> **status**: **実装・deploy 済** (2026-07-23、config v11)。U1-U4 完了:
> APT 3 件のツリー化 A/B = **legacy と完全一致** (109/109/125) → **enabled 全 20 PIR が
> 同一言語 (match ツリー)**。実機 compile スモーク = 意味プロパティ主枝 + keyword 弱補強
> + NOT geopolitical の設計ガイド準拠ツリーを生成 (警告ゼロ)。P1 (compile での silent
> 消失) / P2 (新規 PIR が旧言語) / P3 (chips の実効性乖離) すべて解消。
> **前提 SSoT**: [pir_signal_first_matching_design.md](pir_signal_first_matching_design.md)
> (照合の signal-first 化) / [pir_concept_llm_judge_design.md](pir_concept_llm_judge_design.md)
> (LLM 主題判定層)。本ドキュメントは **authoring (作成・編集・compile・UI) の統一**を扱う。

---

## 1. 問題 (2026-07-23 調査結果)

**評価エンジンは既に一本** (`pir_match_signals` 単一 dispatch、全経路共通)。しかし
**PIR を記述する言語が 3 層に分裂**し、UI は旧言語しか話せない:

| 層 | 記述 | 対象 | UI で作成/編集/可視 |
|---|---|---|---|
| Tier 1 (旧) | strong_signals (keyword/actors OR + 主題アクターゲート) | APT 3 件 | ○ (chip UI) |
| Tier 2 | match 条件ツリー | 12 件 | × (script 注入のみ) |
| Tier 3 | match ツリー + llm_judge | 5 件 | × |

具体的な問題:

- **P1 (バグ)**: 編集画面の「AI で構造化」成功ハンドラが compile 結果 (match=None) に
  ホワイトリスト フィールドだけを重ねるため、**match / llm_judge / spotlight が黙って
  消える** (変換済 17 PIR で「説明編集 → 構造化 → Save」すると旧 keyword ロジックに退行)。
  素の編集は full-object 往復で偶然安全だが、フロント `Pir` 型に両フィールドの宣言が
  なく「たまたま」でしかない。
- **P2 (構造)**: compiler は strong_signals しか生成しない → **UI で新規作成した PIR は
  必ず旧言語** = 二重分類器問題が新規 PIR で再発する。
- **P3 (乖離)**: 編集/詳細画面の chip (keywords/sectors/countries/feeds) は Tier 2/3 では
  照合に使われないのに表示され、実際の照合基準 (ツリー / judge 質問) は画面に出ない。

## 2. 設計原則

1. **PIR is canonical intent は不変**: description が正、structured は compile された
   中間表現。**compile の生成先を条件ツリーに変える**のが本設計の核心。
2. **記述言語は条件ツリー 1 つ**: 全 PIR (新規含む) が同じ文法。Tier 1 の
   actors/主題アクターゲート照合は **ツリーの leaf として移植** (意味論は bug-for-bug
   温存 — 主題ゲート/legacy 分岐/title fallback を変えない)。
3. **strong_signals は補助メタデータに降格** (撤去しない): actors は脅威ページ PIR 連携
   (`threat_operations._build_pir_actor_index`) と forecast が現役消費、全体は
   `PIR_SIGNAL_FIRST=0` rollback の受け皿。照合の主役ではないことを UI に明記する。
4. **LLM に再帰 JSON を書かせない**: compiler の中間表現は **DNF (OR of AND-groups)**
   の flat スキーマ。決定論 builder が樹形化し、validator が検証する。
   (26B ローカル LLM で再帰スキーマは構造化出力の信頼性を損なう。)
5. **UI は「生成 + 可読表示 + 検証」**: ツリービルダー GUI は作らない (YAGNI)。
   compile が生成 → 日本語文で可読表示 (値ラベルは vocab SSoT) → 上級者は JSON 直編集
   → preview で効果確認 → save 時に構造検証。

## 3. 設計

### 3.1 新 leaf: `actor` / `actor_nation` (Tier 1 の移植)

```
{property: "actor",        op: "any_of", value: ["Volt Typhoon", ...]}
{property: "actor_nation", op: "in",     value: ["cn"]}
```

- 意味論 = `_row_match_signals` の actors / actor_nations 分岐**そのまま**:
  subject 評価済み行は主題 id 照合 (+辞書外名は title 語境界 fallback)、未評価 (legacy)
  行は text/entity 照合を bug-for-bug 温存。`SUBJECT_ACTOR_GATE=0` rollback も透過。
- `ArticleFacts` に `subject_source` / `subject_ids` / `actor_values` を追加し、
  評価ヘルパ (辞書解決・nation map) は evaluator の既存関数を遅延 import で再利用
  (patch-target を動かさない)。
- 変換: `china_apt = any[actor any_of[9名], actor_nation in[cn]]` (dprk/russia 同型)。
  **A/B 基準 = legacy と完全一致** (同一ヘルパなので件数完全一致が期待値)。

### 3.2 `validate_match_tree` (構造検証)

- errors (save を 400 で拒否): 未知 combinator / leaf 形式不正 / 未知 property /
  property に許されない op / value 型不正。
- warnings (CompileResponse / save 応答で表示): 統制語彙外の category/intent 値、
  keyword 単独枝 (llm_judge 無効時)。
- property×op 対応表が SSoT (signal_match 所有、compiler / save / UI ヒントが参照):

| property | op | value |
|---|---|---|
| category / intent / victim_country / victim_sector | eq, in | 統制語彙 (category は KNOWN_ARTICLE_CATEGORIES で検証) |
| is_ransomware | is_true | — |
| feed_title | contains_any | substring 群 |
| text / title | keyword_any | 語リスト (title=主題近似スコープ) |
| actor | any_of | アクター名群 (辞書解決) |
| actor_nation | in | ISO alpha-2 |

### 3.3 compiler: description → DNF → ツリー (+llm_judge 提案)

- 出力スキーマ追加 (flat):
  `match_branches: [{conditions: [{property, op, value, negate}]}]` (branches=OR、
  conditions=AND、negate=NOT)、`needs_subject_judge: bool`、`judge_question: str`。
- prompt に property カタログ + 統制語彙 (categories/intents/sectors/国コード) +
  設計ガイドを注入: 意味プロパティ優先 / keyword は固有名詞か意味 clause との AND /
  非サイバー除外は `NOT category=geopolitical` / 主題近似は title スコープ /
  概念的 intent (言及≠主題リスク) は needs_subject_judge=true。
- builder `dnf_to_tree`: 決定論で `any[all[...],...]` へ (単一枝/単一条件は簡約)。
  validator の errors がある枝は落として warning。
- strong_signals 生成は従来どおり継続 (§2-3 の補助用途)。

### 3.4 UI

- **P1 修正**: フロント `Pir` 型に `match` / `llm_judge` を宣言。compile onSuccess は
  `match: resp.pir.match ?? 既存` / `llm_judge: resp.pir.match ? resp.pir.llm_judge : 既存` /
  `spotlight: 新規なら compile 結果、編集なら既存保全`。
- **照合条件セクション** (編集 + 詳細): ツリーを日本語文で可読表示
  (`PirMatchTree` component、値ラベルは vocab `category`/`intent`/`sector` を参照、
  property 名は新 vocab `pir_match_property`)。llm_judge の on/off + 質問文 textarea。
  上級者向けに JSON 直編集 (折りたたみ、parse エラー即時表示、保存前 preview 推奨)。
- **chip の注記**: strong_signals 欄に「照合は上の照合条件で行われる。この欄は
  補助用途 (脅威ページ連携・旧方式の予備)」を明記 (P3 の正直化)。

### 3.5 やらないこと (却下)

- ツリービルダー GUI — compile 生成 + JSON 編集で始め、需要が実証されたら検討 (YAGNI)。
- strong_signals の撤去 — 消費者現存 (threats/forecast/rollback)。
- legacy `_row_match_signals` のコード削除 — `PIR_SIGNAL_FIRST=0` rollback の受け皿として
  温存。全 PIR ツリー化で実運用からは自然消滅 (削除は安定後の別作業)。
- compiler の再帰ツリー直接生成 — §2-4。

## 4. 段階と検証

| 段 | 内容 | 検証 |
|---|---|---|
| U1 | leaf (actor/actor_nation) + ArticleFacts 拡張 + validate_match_tree | unit (主題ゲート/legacy/nation の parity、validator errors/warnings) |
| U2 | compiler DNF (+prompt/builder/検証統合) | unit (fake LLM、builder、語彙警告)。実機 compile 1 件目視 |
| U3 | UI (型 + P1 + 可読表示 + judge + JSON 編集 + 注記) + save 検証 + vocab | tsc + build、実機で編集往復 (match 消えない) |
| U4 | APT 3 件をツリーへ変換 (config v11) | **A/B 完全一致** (legacy vs tree、90d)。rebuild 後 entity 数不変 |
| U5 | docs/memory 更新 | — |

rollback: 従来どおり `PIR_SIGNAL_FIRST=0` (全 PIR legacy へ) / DB 版 revert。
compiler 更新は新規 compile にのみ影響 (既存 PIR は再 compile するまで不変)。
