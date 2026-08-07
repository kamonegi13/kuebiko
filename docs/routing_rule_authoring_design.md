# 配信ルール設定モデル 再設計 (2026-06-14)

> 議論経緯: flag と 記事属性 の違い → 命名の分かりにくさ → 役割での再構成 →
> ネスト段数の制約は無い → 「現行仕様に引きずられず第一原理で設計し直す」。
> 本書は **ルール著作モデル (operator がどう考え・どう書くか)** の再設計。
> 上流アーキテクチャ (PIR→importance→channel の背骨 / 衛生層 / first-match ladder /
> R0 撤去) は確定済で**変更しない**。engine の評価形 (`when` JSON) も**温存**する
> (理由は §7)。変えるのは「著作モデルと UX」と「語彙定義の一元化」。

## 1. 設計目標 (判定基準)

1. **平易**: 非コーダーがルールを読めて、分類学を学ばず書ける。
2. **決定論的・監査可能**: なぜその channel かを一意に説明できる (rule_id 永続化済)。
3. **十分な表現力**: 型付きプロパティの AND/OR/NOT を任意に。
4. **概念最小**: 必要を満たす最小の直交概念 (flag/field の人工的分離を撤廃)。
5. **安全**: 黙って誤配信しにくい (保存前 preview、検証)。
6. **拡張容易**: コードで検出を 1 つ足すと自動でカタログに載る。
7. **データ/engine 安定**: 著作モデルは安定な評価形にコンパイルされ、engine は単純なまま。

## 2. 現行モデルの問題 (積み上げの歪み)

- **flag と 記事属性 が別 "種類"**: 実は flag = boolean 型のプロパティ。**型を種類に誤昇格**。
  これが数ターンの混乱の根。
- **`any` が OR-of-flags 限定**: 属性を混ぜた OR が書けない (engine は対応済なのに UI 制約)。
- **`always` が clause 種類**: 本来「条件 0 個 = 全件該当」というルール単位の話。
- **flag を否定できない**: 属性は not_in/ne で否定可、flag は不可という非対称。
- **語彙定義が散在**: `_FLAG_FIELDS` / `_SET_FIELDS` / `_NUM_FIELDS` / `_eval_leaf` /
  vocab endpoint に同じ語彙が四散。プロパティ追加が多点編集。

## 3. 中核モデル (4 概念のみ)

### 3.1 Property (プロパティ)
記事の**名前付き・型付き・説明付き**の属性。**唯一のカタログ** (PROPERTY_CATALOG) が SSoT。
flag はここで boolean プロパティに吸収され、独立概念ではなくなる。

| 型 | 例 | 演算子 (基底) |
|---|---|---|
| boolean | kev, zero_day, japan_targeted, known_apt, apt_leak | (真) |
| enum | importance, category, article_type, stance, victim_sector | is / in |
| set | actor_nation, keyword_list | いずれか含む |
| number | max_cvss | ≥ > ≤ < = |

各プロパティ: `{id, label, group(topic), type, value_domain?, description}`。
topic 例: 脅威性 / 深刻度 / 分類 / 日本関連 / 被害 / アクター / カスタム。
**flag/field の代わりに topic で束ねる** = 平易さは topic グルーピングで担保。

### 3.2 Condition (条件、再帰)
- **葉**: `プロパティ <型に応じたテスト> 値`
- **グループ**: `(すべて満たす | いずれか満たす)` の子条件群 — **再帰・段数無制限**
  (engine も JSON も検証も任意深さ対応済。再帰描画が最も素直で、cap はむしろ余計なコード)。
- **否定**: 任意の条件 (葉/グループ) への `でない` 修飾 = **一律 NOT**。
  これで「flag を否定できない」非対称が消える。

### 3.3 否定を括り出して演算子を削減
`でない` を一律にすることで、負の演算子 (not_in / ne) が**著作面から不要**になる:
- boolean: `真` (+ でない)
- enum: `is` / `in` (+ でない)
- set: `いずれか含む` (+ でない)
- number: `≥ > ≤ < =` (数値は否定より明示比較が平易なので維持)

著作面の演算子が縮む。コンパイル時に `でない` → engine の `{not: {...}}` に写す。

### 3.4 Rule / Ruleset
- **Rule**: `{id, when: Condition, channel}`。**条件 0 個 = 全件該当** (catch-all、`always` 廃止)。
- **Ruleset**: 順序付きリスト、**first-match** で channel 決定。

## 4. ルール設定要領 (operator 向け運用指針)

1. **順序 = 優先度**。最も緊急/限定的なルールを上、catch-all を最後に (first-match)。
2. 各ルールは「**この記事が〜のとき → このチャンネル**」。
3. 条件は**プロパティを選んで**作る (topic 別カタログ)。flag/field を選ぶ前置きは無い。
   型に応じて入力欄が変わる。否定は `でない`、組合せは `すべて/いずれか`。
4. **少なく平易に**。大半は数個の AND。「その他全部」は catch-all に委ねる。
5. **検出は固定、選択と組合せが自由** (本セッションの原則)。新しい検出軸が要るときは
   コードでプロパティを 1 つ足す → 自動でカタログに載る ([[vocabulary_expansion]])。
6. **保存前に preview** (シナリオ差分)、結果は記事の「配信判定」(rule_id) で監査。
7. **背骨を侵さない**: PIR→importance は上流。ルールは channel 決定 + 衛生 (降格/cap)。
   PIR 優先度をルールに再エンコードしない (関心の分離、確定済)。

## 5. プロパティ・カタログの一元化 (backend)

散在していた語彙定義を **単一の宣言的 PROPERTY_CATALOG** に集約し、そこから
(a) vocab API、(b) フロント編集 UI、(c) 検証、(d) `_eval_leaf` dispatch を導出する。
プロパティ追加 = カタログに 1 entry (型/topic/label/値域/説明) を足すだけ = §3 目標6 の seam。
等価性は既存の約18000ケース matrix テストで behavior 保存を証明する。

## 6. コンパイル (著作モデル → engine when JSON、安定)

著作モデルは**既存の評価形にコンパイル**する (engine 無変更・既存ルール無移行):

| 著作 | コンパイル先 (既存 engine 述語) |
|---|---|
| boolean プロパティ `kev` | `{flag: "kev"}` |
| enum/set/number 条件 | `{<id>: {<op>: <val>}}` |
| グループ すべて/いずれか | `{all:[...]}` / `{any:[...]}` |
| でない | `{not: {...}}` |
| 条件 0 個 | `{always: true}` |

既存の保存ルール (`{flag}`, `{field:{op}}`, `{any}`) は**そのまま新エディタで開ける**
(boolean プロパティ条件 / enum 条件 / グループ として解釈)。**移行不要**。

## 7. 評価形も統一する (決定: 2026-06-14、選択肢2 採用)

当初は「評価形は温存」を実務推奨としたが、ユーザー判断で**評価形 (保存 JSON の葉) も
`{property, op, value}` に統一**する。理由 = 表現を概念と端から端まで一致させ、積み上げの
歪みを*データ層からも*除去する (operator 不可視の wart も残さない)。

- **統一された葉**: `{property: <id>, op: <op>, value: <val>}`。boolean も同型
  (op=`is_true`、value 省略可)。`{flag:x}` / `{field:{op:val}}` の不均一を撤廃。
- **combinator は温存**: `{all:[]}` / `{any:[]}` / `{not:{}}` は既に均一・再帰的で健全。
  葉だけ統一する。`{always:true}` は「条件 0 個」の保存表現として残す (catch-all)。
- **eval 一経路**: `_eval_flag` 特例を撤去し、`_eval_leaf` が catalog から型/accessor を
  引いて op を適用する単一経路に。
- **後方互換は normalizer で**: 旧形 (`{flag:x}` / `{field:{op:val}}`) を **load 時に新形へ
  正規化**する `_normalize_condition` を置く。既存 DB ルール (config_store の旧版) と
  旧 seed はそのまま動き、再保存で新形になる。**破壊的移行をしない**。
- **de-risk**: 既存の約18000ケース等価マトリクス (`test_equivalence_over_matrix`、engine vs
  `_route_legacy`) を**全段階で緑に保つ**ことを behavior 保存の絶対条件とする。seed を新形に
  書き換えても engine 出力が legacy と一致することを証明し続ける。

## 8. 実装範囲

**frontend (中核)**:
- `clauseModel.ts` を再帰 Condition モデルに作り替え (葉|グループ、でない、型別)。
- `RuleEditorFields.tsx` を**自己再帰の条件エディタ**に (型適応入力 + でない + すべて/いずれか
  + 任意ネスト)。情報フローの Rule Drawer と共有。`clauseSummary` を文章的要約に。
- vocab を **typed property catalog** 受信に変更し汎用描画。

**backend**:
- `PROPERTY_CATALOG` 宣言を新設し vocab/検証/`_eval_leaf` dispatch を導出 (語彙一元化)。
- engine の評価ロジック・`when` JSON・保存形式・既存ルールは**無変更**。

**テスト**: clause↔when 往復 (再帰)、型別入力、catch-all、でない、18000ケース等価維持。

## 9. 非スコープ / 維持する確定事項

- PIR→importance→channel の背骨、衛生層 (降格専用)、first-match ladder、R0 撤去 — 不変。
- routing を scoring/decision-table 化しない (first-match は決定論的で監査可能、緊急度
  ladder に適合)。
- engine 評価形は `{property,op,value}` に統一する (§7、選択肢2)。combinator・first-match・
  eval セマンティクスは不変 (葉表現のみ統一、等価マトリクスで behavior 保存を証明)。

## 10. 段階

1. backend: PROPERTY_CATALOG 一元化 (等価テスト維持) — engine 評価は不変。
2. frontend: 再帰条件エディタ + 型適応 + でない + すべて/いずれか + catch-all + JSON 逃げ道。
3. 共有先 (flow Drawer / summary) 追従 + テスト。
4. quality gates → commit → idle 窓で deploy。
