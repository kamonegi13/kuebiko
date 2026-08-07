# Synthesis ファミリー アーキテクチャ見直し — 出力中心から状態中心へ

> 作成 2026-06-28。synthesis (daily/weekly/monthly) / PIR Spotlight / PIR Daily Focus /
> weekly-recap / forecast を横断調査した結果の設計レビュー。
> 関連: [docs/source_pipeline_architecture_review.md](source_pipeline_architecture_review.md)
> (同じ「N 個の分岐を一つの基盤＋射影に畳む」手法)、[docs/pir_system.md](pir_system.md)、
> [docs/value_improvement_roadmap.md](value_improvement_roadmap.md)。

---

## 0. 結論 (一文)

**synthesis ファミリーは「(レンズ × 期間) のマトリクスで独立に再導出される、その場限りの報告レンダーの扇」になっており、品質分岐・monthly 幽霊化・brief 冗長・recap 孤児化はすべて「持続する canonical な情勢評価 (current estimate) が存在しない」ことの症状である。出力中心 (報告を公開する) から状態中心 (見立てを維持し、報告はその射影とする) へ反転する。**

---

## 1. この設計が奉仕する本質

このツールは汎用要約器ではなく、**防衛 CTI アナリストが OSINT の洪水を「意思決定に資する、正直で一貫した情勢の像」に変えるセンスメイキング装置**。確立済みの本質 (メモリ群):

- **相関であって分類ではない**: 価値は join key (国家 / intent / 時間 / PIR) でサイバー↔地政学を結び、サイバー事象を**情勢**として読むこと。
- **5方向ミッション**: 現在認識 / 将来予測 / 過去参照 / 学習記憶 / 発見支援 — 報告の種類でなく、**一つの状況に対する時間軸と認知機能の断面**。
- **認識論的正直さ**: 報道≠発生 / 観測網≠世界 / 暗域=不明≠安全 / 件数≠脅威分布 / 時間差から因果を読まない。
- **時間的連続性**: 事象は過去・未来に連なる。**単発の分析に陥ってはならない** (本見直しの起点)。

CTI ドクトリン上、synthesis は収集→処理→**分析**→配布の「分析」層 = 生観測が intelligence になる場所 = **このツールの製品の核**。上流 (収集・抽出・分類) は全てこの層に奉仕する。

---

## 2. 根本原因の構造

現状の synthesis ファミリーは、2 軸 (レンズ × 期間) のセルごとに**独立した LLM 呼び出し**:

| レンズ \ 期間 | daily | weekly | monthly |
|---|---|---|---|
| global (PMESII 横断) | status_synthesis 07:30/19:30 | status_synthesis 日18:30 | (週次の便乗生成) |
| PIR 縦断 | pir-daily-focus 06:30 | pir-spotlight 月09:00 | (なし) |
| salience (深掘り) | — | weekly-recap 日18:00 | (なし) |

各セルは **(a) ゼロから context を再構築し (b) 一発のナラティブを生成し (c) Discord に吐いて蒸発する** (recap は永続化すらされない)。共有の分析基盤が無い。これが全症状の根:

- 共有基盤が無い → **改善が1セルにしか宿らない** (品質分岐、§3-A)。
- 期間が「生成軸」 → **monthly が週次の副産物として幽霊化** (§3-B)。
- 状態が無く各ジョブが独立に同データを再計算 → **brief 二重投稿・日曜 Ollama 奪い合い** (§3-C)。
- 見立てが無い → **recap が PIR と切れ、結果が状態に還らない** (§3-D)。

これはこのコードベースが繰り返し解いてきた**同じメタ問題**: routing 判定コード→PROPERTY_CATALOG SSoT、Diamond→統一 intent 軸、source の DB-SSoT seam。**「N 個の分岐を一つの基盤＋射影に畳む」**。synthesis ファミリーだけ未適用。

---

## 3. 観測された不整合 (調査で確定、影響度順)

> file:line は 2026-06-28 調査時点。リファクタで移動しうる。

### HIGH

- **A. 品質改善が status_synthesis に閉じている**。2026-06-27〜28 の改善が兄弟へ未波及:

  | 改善 (2026-06) | synthesis | Spotlight | weekly-recap | daily-focus |
  |---|---|---|---|---|
  | source tier (NATO Admiralty 事前計算 + state_media 割引) | ✅ | △ prompt文言のみ | ✗ | △ UIマーカーのみ |
  | 国家相関4レーン (`_build_nation_correlation`) | ✅ | ✗ | ✗ | ✗ |
  | forecast 注入 (FC2) / freshness | ✅ | ✗ | ✗ | ✗ |
  | 構造的矯正の必須3フィールド (source_caveat/forecast_alignment/freshness_note) | ✅ | ✗ | ✗ | ✗ |
  | B(2) 予測スコアカード + 前期継続性 | ✅ | ✗ (実装可) | ✗ | ✗ |
  | PIR 注入 | ✅ | (PIRネイティブ) | ✗ rubric の pir 軸は pir.yaml 未参照 | (PIRネイティブ) |
  | MITRE TTP 接地 | ✗ | ✅ | ✗ | ✗ |

  最も深いナラティブを出す Spotlight (1 PIR 縦断) が、source 信頼度割引も国家文脈も**前期との継続性**も持たない。`src/spotlight/generator.py:_format_candidates` は reliability を記事 dict に含めない。

- **B. monthly が「幽霊 cadence」**。専用 cron が無く、`weekly-status-synthesis` (日18:30) の `synthesis_periods` 未指定 → `src/synthesis/runner.py` fallback `("weekly","monthly")` で**便乗生成**。実態は「月末まとめ」でなく「毎週日曜に当月1日起点で UPSERT する rolling snapshot」。**月内2回目以降は posted_at で Discord 再配信が silent skip** (`src/synthesis/discord.py`)。monthly spotlight はコードにあるが cron 未登録・UI weekly 固定 = dead code 同然。monthly recap は不在。設定ファイルから monthly 生成が読み取れない。

- **C. brief 冗長 + 日曜 LLM 衝突**。
  - pir-daily-focus (06:30) と daily-synthesis-morning (07:30) が同じ brief ch・同じ24h窓・重複母集合。synthesis の pir_section が全 PIR を内包するため 60 分差で類似情報を2回受信。
  - weekly-recap (18:00, 26B, 最悪~23分) → weekly-synthesis (18:30, 31B, **weekly+monthly の2連続31B推論で最悪~50分**)。Ollama 直列推論ゆえ recap が長引くと synthesis 開始遅延／timeout(900s) リスク。

### MEDIUM

- **D. weekly-recap が永続化されず Retrospect に出ない**。recap 本文は Discord のみ。`f1_selections` は article_id+score だけ。synthesis (全文 DB 永続化) と非対称。「あの週の深掘り」を後から読めない。
- **E. auto-trigger の脆弱性**。`src/synthesis/auto_trigger.py` は debounce 6h 固定。07:30/19:30 の間に大量投稿があると定時外 31B 発火 → 毎時 :00 fetch と競合。
- **F. forecast snapshot が weekly 限定** (`main.py` の `if result.generated.get("weekly")`)。将来 monthly 専用 cron を足すと採点がスキップされる。

### LOW

- generator の cross-axis/freshness SQL が SQLite 構文 `datetime()` を `translate_sql` 経由せず raw execute (PG 互換は**要ライブ確認** §8)。
- 型/コメント陳腐化 (`StatusSynthesisRecord.period_type` コメントに daily 欠落、spotlight headline 字数 60-120 vs 150-280)、`prompts/weekly_recap.j2` の E1 廃止後説明残り。

---

## 4. 本質的反転: 出力中心 → 状態中心

> **synthesis を「公開する報告」ではなく「維持される情勢評価 (current estimate)」として扱う。報告はその状態の view (射影) にすぎない。**

情報機関のアナリストは running estimate (生きた見立て) を保持する: 主見立て (確度付き) / 前提 / 監視指標とその状態 / 予測と前回予測の的中 / ギャップ。日報・週報・月報はこの見立てを**異なる高度と頻度でレンダーし、前回からの差分を添えたもの**であって、独立した再分析ではない。

この反転で5方向ミッションが自然に状態に宿る:

| ミッション方向 | 状態中心での実体 |
|---|---|
| 現在認識 | 見立ての現在値 |
| 将来予測 | 見立て内の指標と forecasts (B(2) スコアカードで責任化済 = **時間軸の背骨**) |
| 過去参照 | 見立ての版履歴 + forecast の的中履歴 (Retrospect が初めて意味を持つ) |
| 学習記憶 | 見立てそのものが蓄積知 |
| 発見支援 | 見立ての gap (recap=注意配分はここに繋がる) |

そして**認識論的正直さ (確度 / source tier / freshness / 報道≠発生) は見立ての一級プロパティとして一度だけ適用され、全レンダーが継承する**。今は各プロンプトに散在し不揃い — これは DRY でなく**ミッション上の正しさ**の問題。

---

## 5. 目標設計 (target model)

### 5.1 `AssessmentContext` (共有基盤、状態の中核)

現在 `src/synthesis/generator.py` の `_build_*` が毎回再計算しているものを `src/assessment/` に抽出し、**一回だけ**計算する豊かな状態オブジェクトに:

- 国家相関 (join key: 国家 × {帰属 / 言及 / 標的 / 地政学} の4レーン)
- forecast 指標 (FC2 z-score スパイク) とその検証状態
- reliability (source tier) を付与した高重要記事集合
- PIR 被覆 (どの PIR が何件 match)
- freshness (報道振り返り率)
- **前期見立て** (継続性 = 過去→未来の連結)

### 5.2 レンダー = 射影 (projection)

| レンダー | 射影 = フィルタ × 高度 × 差分 |
|---|---|
| global synthesis | 全軸・深 |
| PIR Spotlight | PIR でフィルタした射影 |
| PIR daily-focus | PIR × 浅い射影 (1-2文、軽量は**意図的に維持**) |
| weekly-recap | salience (注意) 射影 — 見立ての gap + PIR 駆動 |

**「移植」が消える — 源が一つだから** (§3-A が構造的に解消)。射影は AssessmentContext を**読むだけ**で、品質プロパティを自動継承する。

### 5.3 期間 = レンダー軸 (生成 fork ではない)

period は **窓 + 前回差分 (delta-since-last-render-of-this-cadence) + forecast 採点頻度**:

- daily = 戦術差分
- weekly = 作戦再評価 + 予測採点
- monthly = 戦略軌跡 + 予測 track record

**monthly は独立分析でなく「月差分の戦略高度レンダー」** (§3-B 解消、幽霊が消える)。

### 5.4 時間的背骨 (過去→未来)

- 見立ての**版管理** (status_synthesis を canonical store に昇格)。
- forecast の**ライフサイクル** (open → 次期で scored)。B(2) スコアカードを状態の一級機能に。
- 監視指標の**経時追跡**。

### 5.5 朝レンダー統合

朝の brief を**一つの一貫した製品**に (PIR-focus 断面 + global ナラティブをセクション化)。一回の高価な 31B パス + 安価なレンダー群 → 二重投稿も日曜衝突も同時に消える (§3-C)。既存の auto-trigger (差分で見立てを更新) は**システムが既に状態ベースを欲している兆候** — bolt-on でなく組織原理にする。

---

## 6. 段階移行計画 (behavior-preserving、各段が単独で出荷可能)

1. **`AssessmentContext` 抽出**: `_build_*` を `src/assessment/` に関数化。synthesis の挙動は不変 (同じ context を同じ prompt に渡す) → **回帰ゼロで基盤化**。
2. **Spotlight を AssessmentContext に向ける**: §3-A を構造的に解消。source tier / 国家相関 / 構造的矯正 / **前期継続性** を射影で継承。
3. **期間を render+delta に畳む**: monthly を真に意味づけ (§3-B)。専用 cron + 月末 + 配信修正、または明示降格を決着。
4. **朝レンダー統合**: brief 冗長 + 日曜衝突を解消 (§3-C)。
5. **recap を見立て駆動 + 永続化**: §3-D 解消 + Retrospect 連携。salience 射影を gap/PIR に駆動させ、選定を状態に還す (人間 in the loop)。

各段の前に §8 の要確認項目を潰す。

---

## 7. トレードオフ / YAGNI / リスク

- **YAGNI チェック**: 単独運用ツールで estimate オブジェクトは過剰では？ → これは**追加でなく簡約** (4つの独立再導出 → 1基盤+射影)。KISS にむしろ沿う。**段1 だけで止めても価値があるよう増分設計**にする。
- **daily-focus を無理に rich にしない**: 1-2文の軽量さは意図的に正しい。統合は「同一にする」でなく「同一源から射影する」。
- **LLM の narrative 重力**: 構造的矯正 (必須フィールド) の教訓 = 「注入≠使用」。状態を持っても、レンダー時に必須フィールドで使用を強制する設計は維持する。
- **これは設計判断であり実装ではない**。

---

## 8. 要ライブ確認 / open questions

- generator の `datetime()` raw SQL が PG 本番で動作しているか (synthesis は稼働中ゆえ実害無しの可能性大だが、段3 着手前に確認)。
- monthly の posted_at 実挙動 (毎週 UPSERT で2回目以降配信 skip が実際に起きているか)。
- Ollama の並行制約 (`OLLAMA_NUM_PARALLEL` 設定値)。26B/31B 同時要求時の挙動。
- weekly-recap の実測所要時間 (deep_dive_rubric max_tokens=12000 + recap max_tokens=4000 の合計)。

---

## 9. 推奨

段1 (AssessmentContext 抽出) を回帰ゼロで実装し基盤化 → 段2 (Spotlight) で価値を実証 → 段3 以降は**今夜 (2026-06-28) の synthesis レビュー実証**を見て確定。今夜の出力は「構造的矯正の必須3フィールド (source_caveat/forecast_alignment/freshness_note) と B(2) スコアカードが 31B 実出力で機能するか」を示し、**この基盤に何を載せるべきかの実証データ**になる。レビューを待ってから段2以降を確定するのが筋。

---

## 実施ログ

- 2026-06-28: 横断調査 (3 並列 explore) + 本設計文書作成。反転の方向性をユーザー承認 (「事象は過去・未来に連なる、単発分析に陥ってはならない」)。
- 2026-06-28 (同日): **段1 + 段2 実装完了 (回帰ゼロ、未デプロイ)**。
  - **段1 (AssessmentContext 抽出、behavior-preserving)**: `src/assessment/context.py` に `build_nation_correlation`
    / `build_forecast_indicators` / `build_freshness` を generator から **pure move**、`AssessmentContext`
    + `build_assessment_context` を新設。`generate_synthesis` は bundle を使用 (出力 byte 不変)。
    characterization test `tests/unit/test_assessment.py` (4 件) で bundle == 個別 builder を lock。
  - **段2 (Spotlight を assessment に向ける)**: `_format_candidates` に source tier (`classify_source_tier`)
    を事前計算付与、`_build_prompt`/`generate_spotlight` が `AssessmentContext` (国家相関/forecast/鮮度)
    + 前期 spotlight 継続性 (`get_previous_spotlight` repo 追加、`_load_previous_spotlight`) を注入。
    runner が assessment を **1 run 1 回**構築し全 PIR で共有。prompt `pir_spotlight.j2` に横断 context
    + 前期継続性 (「事象は過去・未来に連なる」) + reliability 表示 + 信頼度 tier 利用指示を追加。
  - **判断 (validate-before-propagate)**: 構造的矯正の **出力必須フィールド (source_caveat 等) と B(2)
    スコアカードは spotlight 出力には今回足さない**。理由 = ①spotlight は SPOTLIGHT_MODEL (26B 可) で
    走り得、過負荷で field 脱落の前科 ([[editorial_stance]] 教訓) ②出力フィールド追加 = pir_spotlight
    schema 移行 (pg_schema gotcha)。代わりに **入力 (reliability/前期/相関/鮮度) を全注入**し、確度較正と
    継続性は既存 `outlook` の自由文に織り込ませる (overload ゼロ・schema 不変)。出力フィールド化は今夜の
    synthesis (31B) 実証 + 26B spotlight 検証後に段別判断。
  - **検証**: ruff clean / mypy 新規エラーゼロ (既存 7 件は pir/db_backend/`llm.model` で本変更外) /
    unit 1713 passed・既知ベースライン 9 件のみ fail (nvd/synthesis日付/taxonomy/dashboard = 非regression)。
  - **✅ デプロイ済 + 本番検証 (2026-06-28 17:08 JST, commit 85b7dec)**: `docker compose up -d --build` で
    full+readonly 健全。デプロイ済 refactor コードで daily synthesis を dry-run 検証 → 31B で全 tradecraft
    9 キー populate・必須3フィールド有・forecasts/scorecard 各1 = **段1 behavior-preserving を本番 LLM 経路で確認**。
    デプロイ前の dry-run レビューでも source_caveat が state_media(RT/Sputnik) を明示割引、forecast_alignment が
    ランサム急増を犯罪動向として分離、freshness_note が振り返り 67% を警告、scorecard が前期予測を正直に partial
    採点 = **synthesis 品質改善は 31B で実戦動作**。段2 spotlight は次回 spotlight (月 09:00) から反映。
- **✅ 段3 (真の月次) 実装+デプロイ済 (2026-06-28, commit d6c927d, container recreate 17:28 JST)**: monthly を
  weekly 便乗 (rolling) から分離し月末専用 cron に。schedule schema に day (day-of-month, 'last') 追加、weekly に
  synthesis_periods:[weekly] 明示で便乗停止、monthly-status-synthesis 新設 (day=last 20:00 JST)。スケジューラ登録
  確認 **`monthly-status-synthesis: cron:[day=last] 20:00`**、**初回発火=2026-06-30 20:00 JST**。配信は generated
  全 period 経由で monthly も brief 配信、forecast 採点は _load_previous_synthesis(monthly) が前月 forecasts を採点。
  **gotcha 教訓**: pipelines.yaml は mount されているため、新 schema field (day) を含む yaml を commit すると旧コード稼働中
  のコンテナで `load_pipelines()` が extra_forbidden で壊れる → **新 schema field を伴う yaml 変更はコードと同時デプロイ必須**。
  **残: UI empty-state 文言 (monthly が「日曜18:30」表示) の更新は frontend 軽微 follow-up**。
- **✅ 段4(b)+段5 実装+デプロイ済 (2026-06-28, commit b2cfb74, container 17:50 JST, 検証済)**:
  - 段4(b): weekly-recap 18:00→17:00 (日 18:30 weekly-synthesis と Ollama decouple)。scheduler 登録確認
    `weekly-recap: cron:[sun] 17:00`。
  - 段5 **critical 修正**: digest/runner.py が未定義 max_items を L189 参照し candidates>0 で NameError
    クラッシュ = **weekly-recap がずっと機能不全だった** (pre-existing mypy name-defined の正体)。修正済。
  - 段5 永続化: weekly_recaps テーブル新設 (PG+SQLite, prod 適用確認)、record_weekly_recap /
    get_weekly_recap_in_window、runner が投稿前に本文保存、build_retrospect が週 window で recap 返す
    (generated_at は ISO 文字列比較で dialect 非依存)。
  - 検証: ruff clean / mypy 新規ゼロ (runner:189 も解消) / unit 1718 passed (既知9件のみ非regression)。
- **◻ 残 (deferral、理由つき)**: 段4(a) 朝レンダー統合 (06:30 focus + 07:30 synthesis 統合、最大 refactor +
  brief UX 変更) / 段5 PIR・assessment 注入 (deep_dive_selector rubric) / UI empty-state 文言 (monthly が
  「日曜18:30」表示、frontend 軽微)。
- **教訓**: ①mount config に新 schema field を含む yaml を「未デプロイのつもり」で commit すると稼働コンテナが
  壊れる (コードと同時デプロイ必須)。②pre-existing mypy エラーを「無関係」と切り捨てない (runner:189 は実クラッシュ bug)。
