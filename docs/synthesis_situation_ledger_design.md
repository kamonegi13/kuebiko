# synthesis 状況台帳 (Situation Ledger) 設計 — 選定と物語性の根本再設計

- 日付: 2026-07-03
- 状態: **承認済・段A 実装済** (`src/assessment/` situation_store / assignment / nation_gazetteer /
  ledger、flag `SYNTHESIS_STATE`、段B 以降は未着手)
- 段A 実測 (2026-07-03): プール 7 日間 1162 件中 anchor 皆無 490 件 (42%) の主因は
  involved_country の LLM 抽出漏れ。nation gazetteer (countries.yaml 再利用のタイトル走査) が
  no-anchor の 61% を決定論回収し、割当キー被覆は 58%→84% に改善。残 16% は国が存在しない
  純技術・トレンド記事 (段B の token/LLM fallback の領分)。
- 前提文書: [synthesis_assessment_architecture.md](synthesis_assessment_architecture.md) (状態中心への反転、承認済)、
  [synthesis_reliability_redesign.md](synthesis_reliability_redesign.md) (証拠駆動 ACH、本番稼働中)
- 発端 (ユーザー指摘): ①出力が「ただの事実の列挙」になっている ②synthesis のもととなる事象の
  選定が不透明で、外すと全体に影響する最重要工程なのに統制が弱い

---

## 0. 要約 (BLUF)

synthesis を「毎期ゼロから作る報告書ジェネレータ」から「**持続する情勢台帳 (Situation Ledger) を
毎期更新するオペレーション**」に反転する。分析の単位を「その日の記事から選んだ K 件の判定」ではなく
「**継続して追跡される情勢ライン (Situation)**」とし、日次 run は (a) 新着証拠を既存 Situation に
割当てる (b) 証拠が増えた Situation の判定を ACH で増分更新する (c) 未割当の残余から新規 Situation を
開くか判断する、の 3 操作に分解する。報告 (朝刊/週報/月報/Spotlight) はすべて同一台帳の射影となり、
その本文は必然的に「何が新しく、何が変わり、何が続いているか」= **変化の言語**になる。

これは指摘された 2 症状を同じ根から解決する:

| 症状 | 現行の根本原因 | 台帳化による解決 |
|---|---|---|
| 選定が一発勝負で脆い | 150 件→6 件を LLM 1 パス・タイトルのみ・基準未注入で選び、外した事象は永久に消える | 選定が「割当 (決定論)」と「新規開設判断 (小さく明確な問い)」に分解される。今日見逃しても明日また未割当として現れる = **失敗が回復可能**になる |
| 事実の列挙 | estimate が孤立 KeyJudgment の列で、関係・差分・含意が存在しない。射影 (render) は無いものを書けない | 台帳のプリミティブが「情勢とその推移」なので、新規/強化/転換/収束という **delta が estimate の一級データ**になる。関係・含意も Situation に永続する |

grounded 再設計の確定原則 (対称客観性・ACH 駆動・確度較正・射影原則・推論の可視化) は
**全て維持**し、その上に「状態」を実装する。これは承認済みの状態中心アーキテクチャ
(「事象は過去・未来に連なる、単発の分析に陥ってはならない」) の本体実装である。

---

## 1. この機能の本質

実装から離れて、この機能が何であるべきかを先に定める。

**synthesis の本質は「単独分析官の頭の中にある情勢見積の外部化」である。**
現実の分析官は毎朝ニュースを白紙から要約しない。頭の中に「追いかけている情勢の台帳」
(SharePoint 悪用は続いているか / Mustang Panda の標的は広がっているか / 露宇戦況のサイバー波及) を持ち、
新着報道を**その台帳に照らして**読む。「これは既知の件の続報か、判定を変えるか、新しい件か」。
朝のブリーフィングとは「台帳のうち昨日から変わったこと + 立っている重要判定」の伝達である。
インテリジェンス・ドクトリンでもこれは standing estimate / I&W (indicator & warning) として
定式化されている: 判定は指標を持ち、新着情報は指標に照らされ、見積は改訂され続ける。

この本質から 3 つの帰結が出る:

1. **選定の本質は「ニュース価値」ではなく「台帳に照らした評価価値」である。**
   既存見積が無い場所に選定基準は立たない。現行 nominate が「キーウへのミサイル攻撃」を
   CTI ブリーフの筆頭に選ぶのは、照らすべき台帳が無く、LLM の一般的ニュース価値感覚だけが
   残るからである。台帳があれば問いは「未割当の残余のうち、新しく追跡を開く価値があるのは
   どれか (PIR に照らして)」という、はるかに小さく明確な問いになる。

2. **報告の本質は delta communication である。** 「昨日から何が変わったか」が書けない報告は
   構造的に列挙へ退化する。delta は render 時に捻り出すものではなく、台帳更新の副産物として
   生成される一級データでなければならない。

3. **indicators は飾りではなく I&W ループの部品である。** 現行 ACH は「何があれば判定が変わるか」
   (indicators) を毎回生成して**捨てている**。台帳があれば、翌日の新着証拠を前日の indicators に
   照らす検査が可能になり、intelligence cycle が閉じる。

---

## 2. 中心オブジェクト: Situation (情勢ライン)

### 2.1 定義

**Situation = 継続追跡される 1 つの評価可能な情勢**。粒度は現行 nominate の 1 claim とほぼ同じ
(例: 「SharePoint CVE-2026-XXXX の活発な悪用」「Mustang Panda によるインド政府・エネルギー部門攻撃」
「露宇戦争のエネルギーインフラ攻撃киン (地政学文脈)」)。細かすぎ (CVE 1 件ごと) も
粗すぎ (「中国 APT 動向」全部) も禁物で、**「1 つの ACH がかけられる単位」**が基準。

```
Situation:
  id, title (1行), domain (cyber_incident / geopolitical / PMESII軸)
  anchors: 強 entity の組 (actor / CVE / malware / victim_org / nation-pair)  ← identity と割当キー
  status: active / dormant / closed   (+ reopened は revision の delta_type で表現)
  opened_at, last_evidence_at, closed_at
  pir_ids: 決定論で付与 (src/pir/evaluator.py 再利用)
  current_revision → SituationRevision (最新判定)
```

```
SituationRevision (版管理される判定):
  rev, created_at, run_id
  claim (改訂可・監査付き), claim_type: ongoing_activity / discrete_event / structural
  leading_hypothesis, confidence, confidence_basis   (grounded の既存機構そのまま)
  hypotheses (ACH 行列), key_assumptions, missing_evidence, indicators
  implication: 含意 (「だから何か」— PIR/日本防衛に照らした 1-3 文、証拠接地・確度語準拠)
  delta_type: opened / strengthened / weakened / hypothesis_flip / escalated /
              claim_revised / reopened / no_change / closing
  delta_note: 何が・なぜ変わったか (1-2 文)
```

```
SituationEvidence (証拠台帳):        SituationRelation (関係):
  situation_id, article_id,            a_id, b_id,
  polarity, attribution_basis,         type: same_actor / same_campaign /
  excerpt, source_tier, added_at             shared_nation / temporal_sequence
                                       basis: 根拠 (entity 名 / 時系列事実)
                                       ※ 因果は断定しない (temporal_sequence は事実のみ)
```

### 2.2 identity と merge/split

- identity は **anchor entities の組**で決まる。割当 (§3.2) は強 anchor 完全一致を最優先。
- 自動 merge は**強 anchor が完全一致する場合のみ** (保守的)。それ以外の統合・分割は
  UI (情勢ボード) から手動、監査ログ付き。actor_candidates と同じ「機械=検出/人=確定」哲学。

### 2.3 lifecycle

- **active → dormant**: last_evidence_at から `DORMANT_AFTER` (domain 別定数、目安
  cyber_incident=14d / geopolitical=30d) 経過で自動。dormant は追跡継続・報告からは退場。
- **dormant → active (reopen)**: 新着証拠が割当てられたら復帰、delta_type=reopened。
  「再浮上」記事は新規事象扱いせず、既存 (dormant 含む) Situation への割当として自然に処理される
  — 現行の再浮上フラグ問題のより正しい解決。
- **→ closed**: 収束判定 (パッチ普及+悪用途絶 / テイクダウン確認 / 情勢終息) 自体を
  1 つの判定 (確度付き) として revision に残して閉じる。dormant が `CLOSE_AFTER` (90d) 続いたら
  自動 close (delta_type=closing、低確度の「活動観測されず」)。

### 2.4 claim_type と鮮度の正直さ (自動減衰は不採用)

- `ongoing_activity` (「悪用が継続中」) は時間指標付きの主張である。新規裏付けが
  `STALE_AFTER` を超えて途絶えたら、確度の数字を黙って下げるのではなく
  **主張の時制を落とす** (「継続中」→「継続は未確認 (最終裏付け N 日前)」) + 次回更新で再評価を強制。
- `discrete_event` (「攻撃が発生した」) と `structural` (「規制が施行された」) は減衰しない。
- 確度の自動 decay 曲線は**不採用** (恣意的な数字操作は認識論的正直さに反する。
  鮮度は表示し、判定変更は必ず証拠か再評価を経る)。

---

## 3. 更新オペレーション (daily run の再定義)

現行: pool → nominate(150→6) → 拡張 → ACH → adversarial → render。
新設計: 以下の 7 段。**grounded の ACH エンジン (passes/adversary/estimate の較正機構) は
そのまま部品として再利用**する。

### 3.0 プール

現行 `_build_high_importance_cross_axis` (high+medium・報道時刻窓) を維持。ただし:
- **150 cap の選定競争を廃止**。割当 (3.2) は決定論なので全件処理できる。安全上限 500 に緩和し、
  到達時は必ず log (no-silent-caps 原則)。現行は毎 run cap 飽和 = medium 数十件/日が黙って
  落ちていたが、これが消える。
- `status='posted'` ゲートは維持 (collected は被害台帳であり評価対象でない)。

### 3.1 割当 (assign) — 選定の主経路を決定論化

新着記事 → 既存 Situation (active + dormant) への割当:
1. **決定論第一**: 記事の entity keys (`repo.entity_keys_for_articles`) と Situation の anchors の
   強一致 (actor/CVE/malware/victim_org)。`clustering.py` の entity + タイトル token 機構を一般化。
2. **曖昧のみ LLM**: entity が乏しい地政学系などは、候補 Situation (nation/domain で絞った少数) との
   照合を 1 回のバッチ呼出で判定。
3. 割当結果は SituationEvidence に追記 (polarity 等は 3.3 の ACH が付ける)。

### 3.2 判定の増分更新 (incremental ACH)

新着証拠を得た各 Situation について `ground_and_score` の増分版を実行:
- **入力**: 前回判定 (claim / leading / 確度 / ACH 集計) + 既存台帳の最強証拠抜粋
  (leading 支持・反証それぞれ上位、計 `LEDGER_EXCERPT_MAX` 件) + **新着ソース本文 (全文 4k truncate)**。
  台帳が何年育っても prompt は有界。
- **指示**: 対称に再評価せよ。新証拠が反証なら flip を恐れるな。claim の文言改訂も可 (監査付き)。
- **delta_type はコードが決定** (LLM に自己申告させない): 前後の leading / confidence / ACH 集計の
  比較から決定論的に分類。escalated は被害拡大・標的拡大等の証拠フラグから。
- 確度較正 (`final_confidence` / source_basis cap / 帰属 cap) は現行のまま適用。
- **indicator 照合 (I&W)**: 更新前に、新着記事が前回 revision の indicators に合致するかを検査
  (indicators は entity を含むことが多く決定論で前捌き、LLM で確認)。合致は delta_note に明記。
  現行「生成して捨てる」indicators が初めて機能する。

### 3.3 新規開設判断 (detect new) — 旧 nominate の後継

**未割当の残余のみ**を対象に (通常 150 件でなく数十件)、「新しく追跡を開く価値がある情勢はどれか」を
1 回の LLM 呼出で判断。現行 nominate との決定的な違い:
- **基準を注入する**: triage と同じ PIR context (`build_synthesis_pir_context`) + 使命序列
  (①PIR 直結のサイバー作戦 ②日本関連 ③サイバー隣接の政策・規制 ④地政学文脈 — 文脈は開設可だが
  headline 優先度は低い、§4.2 の salience で符号化)。
- **問いが小さい**: 「期間の重要事象を最大 K 件」でなく「未割当のうち追跡開始に値するもの」。
  開設数に人工上限を置かない (実際は 0-3 件/日程度)。
- **落選台帳 (situation_detection_log)**: 開設しなかった high 記事は 1 行理由付きで必ず記録。
  「何を選ばなかったか」が初めて監査可能になる。UI と log に露出、90d retention。
- 開設された Situation は通常の grounded 接地 (過去文脈 entity 遡及込み — 初回接地にのみ
  歴史窓が要る、という現行確定原則の正しい適用範囲) を経て初回 revision を得る。

### 3.4 関係更新

- 決定論: 共有 anchor (same_actor / shared_nation) と時系列事実 (temporal_sequence) から edge 生成。
  `AssessmentContext.nation_correlation` (4 レーン) の builder は関係検出の供給源として再接続。
- 任意 LLM パス: 関係の意味ラベル付けのみ (制約付き・因果断定禁止)。
- これで render の chain_section に**初めて実データが供給される** (現行「特記なし」の根治)。

### 3.5 週次 adversarial sweep (定着バイアス対策)

持続する見積の最大リスクは**アンカリング** (一度立った判定に新証拠を吸着させる)。ACH が本来
戦う相手が台帳化で再侵入するのを防ぐ:
- 日次: 変化のあった判定のみ adversarial_review (現行同様)。
- **週次: 全 active Situation を対象に対称 red-team を一巡** (変化が無くても反証を試みる)。
  反証成立は通常の降格/flip 経路。

### 3.6 forecast / freshness の吸収 — **実装済 (2026-07-05 監査 P4)**

- forecast は Situation に付く (indicators + 見通しが採点可能な予測になる)。
  forecast lifecycle (open→scored) は Situation の revision 履歴で自然に追跡。
- freshness は last_evidence_at / staleness 表示 (§2.4) として台帳の一級属性になる。
- render.py で空文字ハードコードされている forecast_alignment / freshness_note が実データを持つ。

実装 (`src/assessment/forecast.py` + `situation_forecasts` テーブル):
- **open**: active Situation の最新 revision の indicator ごとに open forecast (冪等)
- **hit**: 後続 revision の fired_indicators (既存 I&W 照合) に一致 → 的中
- **expired**: 30 日 (FORECAST_HORIZON_DAYS) 発現なし or Situation close → 未発現
- **射影**: build_forecast_context が tradecraft の forecasts/forecast_scorecard/
  forecast_alignment/freshness_note を埋める (既存 UI・forecast_accuracy KPI が復活)。
  freshness_note は「7 日以上裏付けのない継続情勢 N 件 (最古 X 日前)」の正直注記
  (確度 decay 不採用の代替)。全て決定論・LLM なし。

---

## 4. 射影 (render の再定義)

### 4.1 期間 = render 軸 (承認済み原則の実装)

全報告が同一台帳の射影になる:
- **daily (朝刊)**: 24h の delta + 立っている高 salience 判定。
- **weekly**: 7d の revision 軌跡 (強化/転換の物語) + 全 active の棚卸し + adversarial sweep 結果。
- **monthly**: 30d 軌跡の戦略高度レンダー (承認済み「月差分の戦略高度レンダー」そのもの)。
- **Spotlight**: PIR フィルタ射影 (pir_ids で絞るだけ)。
- **daily-focus**: 軽量のまま (確定原則)。

### 4.2 salience — 「最重要」の決定論化

headline と掲載順を LLM の暗黙ニュース感覚から奪還する:

```
salience = 関連性 × 認識論的重み   (2026-07-05 改訂: 乗法化)
関連性     = W_DOMAIN·domain + W_DELTA·delta + W_JP·japan + W_PIR·pir
認識論的重み = conf (high1.0/mod0.85/low0.6) × 仮説クラス (噂系*0.35) × 反証 (*0.6)
delta_magnitude: hypothesis_flip=3 / opened=2 / escalated=2 / strengthened=weakened=1 / no_change=0
domain_weight:   cyber_incident=1.0 / サイバー隣接=0.8 / geopolitical=0.5
噂系仮説 = unverified_or_false / reporting_artifact / propaganda_or_overstated
```

**2026-07-05 改訂の背景**: ボット対策画面のみのソースから開設された leading=
unverified_or_false の判定が、実証拠 7 件の同 delta 判定を差し置いて headline を占有した。
病巣は ACH の誠実な較正結果 (確度・仮説クラス・反証) が配置決定に流れ込む経路の不在。
筆頭価値 = 意思決定関連性 × 接地された知識量 (乗法 — 片方が空洞なら価値も空洞)。
moved 優先のハードルールも廃止 — 「変化優先」は delta 重みから創発し、認識論的に空洞な
変化だけが接地された standing に沈む (その日は headline_mode=quiet で「確度をもって報告
できる大きな変化はない」と正直報告)。噂 Situation の**追跡自体は正しい** (確認されれば
flip する = I&W の設計通り) — 誤りは追跡でなく配置、なのでハード除外はしない
(日本×多 PIR 級の関連性なら未実証でも浮上できる)。

重みは定数モジュール (マジックナンバー禁止)。地政学 Situation は追跡するが、CTI ブリーフの
headline は cyber が原則先行 — 「キーウへのミサイル攻撃」が筆頭に立つ問題の構造的解決。
LLM は**順位を選ばず、選ばれたものの散文だけを書く**。

### 4.3 新セクション構成 (delta の言語)

```
見出し    : 最高 salience の「変化」2 文・100-160 字 (確度語は判定と一致、現行規律維持)
新規      : opened された Situation (claim / 見立て / 確度 / PIR)
変化      : strengthened / weakened / flip / escalated (delta_note = なぜ変わったか)
継続(要注視): 変化なしだが salience 高い standing judgment を各 1 行
収束      : closed / closing
注視指標  : 発火した indicator + 開いている indicator (I&W)
PIR対応   : pir_ids からの決定論ロールアップ + 含意 (implication)
```

現行 6 セクション (weight/chain/cog/spillover/pir) からの移行は render 層のみの変更。
render の規律 (「射影であって再分析ではない」「確度語厳守」) は不変 — **入力が変わるから出力が変わる**。

#### headline 契約 (2026-07-04 follow-up — 実測退行の恒久修正)

段C 初版は headline の散文仕様を「1-2 文」とだけ書いたため、実運用で claim の言い換え
(48-61 字、変化の言語も含意もなし) に収束した。headline = 朝刊太字先頭行の情報量不変条件は
プロンプト文言でなく構造で強制する:

- **書式モードのコード指名** (`render.py:_headline_mode`): moved (変化あり) / quiet (台帳静穏 —
  「変化なし」を正直に明示) / plain (delta 未追跡の rollback 経路 — 「変化なし」と偽らない)。
- **contract** (synthesis_render.j2): 2 文・100-160 字下限重視。1 文目 = 変化種別が分かる
  自然な日本語 + 固有名詞要点、2 文目 = 含意/リスク。確度語一致は従来通り。
- **決定論 floor ガード** (`render.py:_guard_headline`): `_HEADLINE_MIN_CHARS` (70) 未満は
  台帳 field (delta/claim/delta_note/見立て/確度/含意) からの決定論合成に差し替え
  (新規主張ゼロ)。warning log `synthesis_headline_below_floor` で観測可能。

### 4.4 UI: 情勢ボード

SynthesisTab を「期間レポート閲覧」から「情勢ボード」へ発展:
active Situation 一覧 (salience 順) → 各 Situation の確度推移・revision 履歴・証拠台帳・ACH 行列・
関係グラフ・indicators。**「推論の可視化が最終保証」の原則を時間軸に拡張**したものであり、
分析官は「機械の判定がどう変わってきたか」を初めて検証できる。

---

## 5. スキーマ (PostgreSQL + SQLite fallback)

新テーブル 5 つ (`status_synthesis` は後方互換のまま、報告スナップショットとして継続。
tradecraft への estimate 埋込も維持し、situation_id 参照を追加):

```sql
situations           (id, title, domain, status, anchors JSONB, pir_ids JSONB,
                      opened_at, last_evidence_at, closed_at)
situation_revisions  (id, situation_id, rev, run_id, claim, claim_type,
                      leading_hypothesis, confidence, confidence_basis,
                      hypotheses JSONB, assumptions JSONB, missing JSONB,
                      indicators JSONB, implication, delta_type, delta_note, created_at)
situation_evidence   (situation_id, article_id, polarity, attribution_basis,
                      excerpt, source_tier, added_at, read_at, assessed_at)
situation_relations  (a_id, b_id, type, basis, created_at)
situation_detection_log (run_at, article_id, decision, reason)   -- 落選台帳, 90d purge
```

注意: [[pg_schema_index_ordering]] — 新列 INDEX は末尾 ALTER の後。PG-only path
(GROUP BY 等) は実機 dry-run 必須 (recap の教訓)。

### 5.1 証拠の状態分離 — 観測と判断は別状態 (2026-07-16)

`situation_evidence` の 1 行は **assignment → read → assessment** の 3 状態を遷移する:

| 状態 | 意味 | 列 | polarity/excerpt |
|---|---|---|---|
| 割当 (assignment) | matcher/収穫が記事を紐付けた「観測」 | read_at NULL / assessed_at NULL | **無意味** (物理既定値のみ) |
| 読了 (read) | 接地 prompt に本文が供給された | read_at 有 | 無意味 (引用に至らず) |
| 評価 (assessment) | ACH が証拠として引用した「判断」 | assessed_at 有 | **有意** |

背景 (2026-07-16 監査): 旧 `add_evidence` は (a) 割当を polarity='neutral' の既定値で
書き UI の「中立の証拠」に化けさせ、(b) 既存ペアを冪等 skip したため毎時割当が先行した
記事の ACH 評価が**永久に落ち** (実測: neutral 909 行中 878 行が excerpt 空)、(c) 未評価
キューを「added_at > 最終 revision」で定義したため revision が立つたび未読分が黙って
脱落した。書込は `record_assignment` (観測・冪等) / `record_assessment` (判断・常に最新
で upsert、added_at/assigned_by の来歴は保持) / `mark_read` に分離し、キューは
`unread_evidence` (read_at IS NULL、読むまで残る)。射影 (`_rehydrate_for_projection` /
`evidence_items`) は **評価済みのみ**を EvidenceItem 化し、未評価は `unassessed_count`
で正直に併記する。過去に落ちた評価は status_synthesis の grounded_estimate 埋込から
`scripts/backfill_evidence_assessments.py` が決定論復元した (一回性)。

---

## 6. コスト (Ollama 直列・30 分 timeout 内)

| 段 | 現行 daily | 新 daily |
|---|---|---|
| nominate / detect new | 1 | 1 (未割当のみ・小さい) |
| 割当 | — | 0-1 (曖昧分バッチ) |
| ACH | 6 (毎回フル) | 4-8 増分 (新着ある Situation のみ) + 0-3 新規フル |
| adversarial | 1 | 1 (変化分のみ; 週次 sweep は週 1-2 呼出) |
| render | 1 | 1 |
| **計** | **~9** | **~8-14** |

weekly は再導出が消えるため現行 (~13) より軽くなる。prompt は台帳要約方針 (§3.2) で永続的に有界。

---

## 7. 確定原則との整合

| 確定原則 (再提案不可) | 本設計での扱い |
|---|---|
| 対称な客観性 / 確度 cap 方向中立 / 仮説禁止せず過確信のみ防ぐ | 不変 (ACH エンジンをそのまま再利用) |
| 報告は estimate の射影 | 強化 (estimate が richer になり射影が成立する) |
| 推論の可視化が最終保証 | 時間軸に拡張 (情勢ボード) |
| ノミネート窓は対象期間・接地のみ過去に伸ばす | 精神を維持: 新規開設判断は期間内のみ。継続性は再ノミネートでなく**状態**が担う。過去文脈遡及は新規 Situation の初回接地に限定 (本来の適用範囲) |
| 利用率は K でなく裏取り拡張で上げる | 維持: K cap は render 層 (掲載数) に移動、追跡数と分離 |
| 仮説メニュー = コード SSoT | 不変 |
| daily-focus 軽量は意図的 / 期間 = render 軸 | 本設計がその実装 |
| 時系列は事実供給・因果断定しない | relations の temporal_sequence も事実のみ |
| 認識論的正直さ = estimate 一級プロパティ | 拡張: considered/assigned/unassigned/opened/**rejected** を毎 run 記録 |

## 8. 採用しない代替案 (検討済み)

- **actor 中心の状態**: actor を持たない情勢 (CVE 悪用・政策) を表せない。anchors の 1 種に格下げ。
- **PIR 中心の identity**: Situation は複数 PIR を横断する。PIR はリンク/射影フィルタであって identity でない。
- **現行 nominate への基準注入だけの弥縫策**: 一発勝負・回復不能・列挙という構造は残る。
  基準注入は本設計の §3.3 に吸収 (捨て実装にしない)。
- **確度の自動 decay 曲線**: 恣意的数字操作。staleness の正直表示 + 再評価強制で代替 (§2.4)。
- **delta_type の LLM 自己申告**: ドリフト源。コードの前後比較で決定論化 (§3.2)。
- **無制限の自動 merge**: 情勢の identity 汚染。強 anchor 完全一致のみ自動、他は人間 (§2.2)。

## 9. 段階移行 (各段 単独出荷可・flag 裏)

flag: `SYNTHESIS_STATE` (0/1/shadow)。前提 `SYNTHESIS_GROUNDED=1`。rollback = flag 0 で
現行 per-period grounded に即復帰 (台帳テーブルは残るが読まれないだけ)。

- **段A — 台帳の骨格 (shadow)**: schema + Situation store + 決定論割当 + 現行 nominate 出力からの
  bootstrap (初回 run で Situation 開設)。出力不変、台帳が裏で育つ。
  `scripts/bootstrap_situations.py` で直近 14d を replay して温める (任意)。
- **段B — 増分更新と新規開設**: incremental ACH + delta 判定 + lifecycle + PIR 基準注入の
  detect new + 落選台帳 + indicator 照合。出力はまだ現行セクション構成 (中身は台帳由来)。
- **段C — delta render (体感が変わる段)**: §4.3 新セクション + salience 決定論ランキング +
  headline 規則。Discord/Web の朝刊が「変化の言語」になる。
- **段D — ファミリー統一と情勢ボード**: weekly/monthly/Spotlight を台帳射影化、
  relations、週次 adversarial sweep、SynthesisTab → 情勢ボード。

## 10. 検証 (受入基準)

- **選定の回復可能性**: 意図的に 1 事象を初日の detect new から除外 → 翌日 run で未割当として
  再浮上し開設されること (回帰テスト化)。
- **recall 監査**: high 記事の「未割当かつ未開設かつ落選理由なし」率 = 0 (no-silent-caps)。
- **列挙の解消 (定量)**: chain 相当セクションの「特記なし」率、および delta 言明
  (新規/変化/収束) の件数/朝刊を、切替前後 2 週間で比較。
- **アンカリング検査**: golden test — 反証証拠を後日投入した Situation が flip すること
  (USB ゴールデンの時系列版)。
- **既存規律の回帰**: 確度語一致・cap・対称性の既存 grounded unit を台帳経路でも green。
- 実機 PG dry-run (SQLite unit では PG-only bug を検出できない — recap の教訓)。
