# 主題アクター判定層 (Subject Actor Attribution) 設計書

作成: 2026-07-17。契機 = 朝ブリーフの「中国系 APT 動向」に FSB 第16センター記事が
2/3 件混入したインシデント (Salt Typhoon への**比較言及**が PIR の actors テキスト照合に
ヒット)。関連: [actor_mission_threat_design.md](actor_mission_threat_design.md) (言及ベース
観測の限界を明示した前作業)。

---

## 1. 問題の本質: 言及 (mention) ≠ 主題 (subject)

- 記事→アクターの帰属は **alias 走査 (title+summary+body の言及)** しか存在せず、
  「Salt Typhoon の手法と類似」という比較言及も帰属になる。
- PIR evaluator の `actors` シグナルは生テキスト substring 照合 (`a in text`) のため、
  比較言及だけで国別 APT セクションに分類される。
- 潜在バグが増幅: `article_entities` の actor 値は**辞書 id** (`salt_typhoon`)、evaluator の
  照合は**表示名** (`salt typhoon`) — entity 照合と actor_nations の複数語 id が不発で、
  実質「生テキスト照合だけが生きている」状態だった。
  ⚠ この id 照合を主題ゲートなしに直すと entity/actor_nations 経由で同じ誤分類が再発する
  (修復は主題ゲートとセットでのみ入れる)。

## 2. 設計: 決定論 2 層の主題判定 + 既存 LLM 判定の再利用

**新規 LLM 呼出はゼロ**。summarizer が既に出力している
`routing_flags.primary_actor_id` (主要アクター判定 + confidence、Phase 5L-3 から
router が実運用中) を第 2 層として再利用する。

### 判定 (取込時、persistence で全経路一元)

| 層 | 方法 | source |
|---|---|---|
| 1 | **タイトル alias 走査** (word-boundary、翻訳後+原題の両方)。org 抑止 (グループ特定時は親機関を除く) + R-A フィルタ (非 cyber カテゴリでは organization/contractor を主題にしない) — 既存規則の再利用 | `title` |
| 2 | タイトル不在時のみ: `primary_actor_id` (slug) を**辞書に解決**し、**言及集合 (detected_actor_ids) に含まれる場合のみ**主題採用。confidence ∈ {high, medium} のみ (routing の `use_llm_primary` と同一規約) | `llm` |
| — | どちらも不成立 = 主題なし (評価済み) | `none` |

**語彙固定 (原則適合の要)**: LLM は自由記述アクターを主題にできない。
①辞書解決 (確定帰属は辞書ゲート — actor recall layer と同一原則)、
②言及集合への所属 (記事に名前が出ていないアクターを主題にしない = 帰属の捏造防止)、
の二重ゲート。LLM の最悪の失敗は「実在言及の中の選び間違い」に限定される。

### 永続化 (articles 列、intent/intent_confidence の前例に従う)

- `subject_actor_ids TEXT` — comma 連結の辞書 id ('' = 評価済み・主題なし)
- `subject_actor_source TEXT` — 'title' | 'llm' | 'none'。**NULL = 未評価 (legacy 行)**
- `subject_actor_confidence TEXT` — source='llm' のみ high/medium

### 消費 (PIR 照合の核 `_row_match_signals` 1 箇所 → 全サーフェスに波及)

`evaluate_pir_matches` (daily focus / KPI / flow) ・ `rebuild_pir_entities` (夜間 full
replace) ・ `evaluate_pir_for_article` (取込 inline タグ) は全て同一 matcher を通るため、
分岐は 1 箇所:

- **subject_actor_source が非 NULL (評価済み) の行**: `actors` = 主題 id ∩ PIR actors の
  辞書解決 id (+辞書未解決の PIR 名のみタイトル語境界照合に fallback)。`actor_nations` =
  主題 id → nation。生テキスト substring 照合は廃止。マッチ時は matched_via に
  `subject:title` / `subject:llm` を追記 (透明性)。
- **NULL (legacy) 行**: 旧コードを bug-for-bug で温存 (テキスト照合 + 不発の entity 照合)。
  id 修復を legacy 行に適用しないのは §1 ⚠ のため。backfill + 窓の経過で自然消滅する。

### 決定論原則との関係 (2026-07-17 ユーザー合意)

- 最終分類は常に「保存値の上の決定論規則」。LLM は主題という**判定材料の生成**のみ。
- 不明を正直に: LLM 不確信 (confidence=low / 空) は 'none' — 無理に埋めない。
- fail-safe: 判定失敗は NULL (legacy 挙動維持)。rollback は env 2 本:
  - `SUBJECT_ACTOR_LLM=0` — 第 2 層停止 (title 層のみ)
  - `SUBJECT_ACTOR_GATE=0` — 消費停止 (matcher が全行 legacy 照合)

## 3. 実装配置

| ファイル | 内容 |
|---|---|
| `src/cti/subject_actor.py` (新) | 判定純関数 + R-A フィルタの移設 (persistence から verbatim 移動、facade re-export 維持) |
| `src/pipeline/persistence.py` | 取込時に判定→ArticleRecord 3 列 + inline PIR 評価へ伝搬 |
| `src/storage/` records / repo_articles / repo_base(migration) / pg_schema | 3 列追加 (SQLite=migration リスト、PG=末尾 ALTER。index 不要) |
| `src/pir/evaluator.py` | 主題ゲート分岐 + `_resolve_pir_actor_names` (双方向語境界、threat_operations と同規則) + `_actor_nation_by_id` |
| `src/ui/services/fill_rate_audit.py` | METRICS に主題解決率を登録 (分母 = actor entity を持つ記事) |
| `scripts/backfill_subject_actors.py` (新) | title 層のみの決定論 backfill (LLM なし)。title ヒット行のみ書込、それ以外は NULL 温存 |
| `config/actor_aliases.yaml` | russia_fsb に単独 alias "FSB" 追加 (GRU の前例と対称。FSB タイトル記事の title 層を有効化) |

## 4. 意図的な残余 (limitation)

- **歴史行のうち title 非ヒット記事は legacy 照合のまま** (summarizer の routing_flags は
  未永続で LLM 層を遡及できない)。daily focus は 24h 窓なので即日収束、最長 90d
  (rebuild 窓) で完全収束。
- タイトル内の比較言及 (稀) は title 層が主題と誤認しうる。
- 第 2 層は summarizer の判定品質に依存 (閉語彙で捏造は不可能だが選び間違いは可能)。
  matched_via の `subject:llm` と fill-rate 監査で観測する。

## 5. 受け入れ基準 (テストで固定)

- FSB シナリオ: subject={turla} の行は pir_china_apt の actors/actor_nations に**不一致**、
  pir_russia_apt に一致 (subject token 付き)
- legacy 行 (source=NULL): 従来どおり比較言及テキストで一致 (回帰ピン。gate の外)
- 辞書未解決の PIR actors 名はタイトル照合のみに fallback
- LLM 層: slug 解決 / 言及集合外の棄却 / confidence=low の不採用 / flag off
- R-A: 非 cyber カテゴリのタイトル org ヒットは主題にしない
