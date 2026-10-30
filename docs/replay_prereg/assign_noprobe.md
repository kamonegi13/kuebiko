# RP-5-3: 確率なしの assign の ML (as-of の台帳、分割外の新しい 200 組) の事前登録

- 作成: 2026-10-31 (JST)。**この文書のコミットが標本の抜き出し・Opus のラベル・判定より前**。
  作成時点で新しい 200 組は抜いておらず、ラベルも腕の判定も 1 件も無い
- 対象の判断: SW-ASSIGN (`ASSIGN_PROBE_FEATURE` 1 → 0、`step:assign_evidence_judge` の上書きを外す)。
  判定する構成 = `ASSIGN_ML=1` + 確率なしの `config/models/assign_model.json`
  (sha256 `24400a9d496052d9320b536ac5ad6518a41cef56ab258859ee581ee5e70771cb`、閾値 0.58、645 組で学習)
- 著者が見たもの: 本番の旗 (`ASSIGN_ML=1`・`ASSIGN_EMBED_GATE=on`・`ASSIGN_PROBE_FEATURE=1`)、
  割当のコード、`run_logs` の `assign_gate` の集計行、`situation_evidence` の規則ごとの件数

## 0. 範囲の確認 (この判定で測らないもの)

- 確率ありの腕 (`assign_model_probe.json`) は gemma4:26b の P(yes) を特徴量に要る = Ollama の生成。
  この実行は Ollama の生成を使わない (調整役の指示) ので、**確率ありとの McNemar は実施しない**。
  本番は組ごとの P(yes) も判定も保存していない (`assign_gate_drop` のログは落とした組だけ・1 run 40 件まで)
  ので、記録からも作れない。台帳の `metrics` は確率なしの適合・回収なので、判定はそれで行う
- 代わりに LLM を使わない参照 2 つを同じ組に当て、McNemar を報告する (合否には使わない): (R1) 規則の割当を
  そのまま通す、(R2) 埋込の関門 (cos_title ≥ 0.60、`assign_gate.ASSIGN_GATE_THRESHOLD`)
- 付記: 毎時の `refresh_ledger_assignments` は関門を通さずに規則の割当を書く (読むだけで確認)。
  本番の証拠の多くは R1 と同じ扱いで入っている。報告に書く

## 1. 仮説

確率なしの ML (閾値 0.58) は、分割外の新しい規則の候補の組で、台帳の証拠の割当の適合率の目標 (≥ 80%、
`docs/accuracy_targets.md` §4.4) を満たし、回収も実用の水準を保つ。

## 2. 母集団 (as-of の台帳の割当の候補)

- 記事: 本番の台帳のプールと同じ条件 (`status='posted'`、importance ∈ {high, medium}) で、事象時刻
  (`EVENT_TS_EXPR`) が 2026-10-02 00:00 JST 以上・2026-10-30 00:00 JST 未満。同じ article_id は最新の行に畳む
- 情勢 (as-of = 記事の `created_at` の時点): `kind='event'`、`opened_at ≤ t`、`closed_at` が空か t より後
  (統合は `closed_at` を立てるのでこれで足りる)。標識 (anchors) は開設後に変わらない
  (`situation_store` に更新の経路なし) ので今の値。題名は今の値 (更新の時刻が記録に無い、制約として報告)。
  `claim_type` は `created_at ≤ t` の最新の revision
- 候補の組 = `assignment.match_situation` (本番の規則、最良の 1 情勢) が返す (記事, 情勢)。記事の鍵は
  `entity_keys_for_articles` と同じ表 (`article_entities`) の今の値
- 「埋込の関門を越えた組」の読み: ML が実際に判定する組 = 記事の要約埋込 (`summary_embeddings`) と情勢の
  題名の埋込が両方そろう組 (そろわなければ本番は埋込の関門へ退避し ML は判定しない)。cos ≥ 0.60 での
  絞り込みとは読まない (それは別の判定器の通過を条件にする)
- 除外して数える: 学習の 662 組 (`data/mlx/assign_goldset.jsonl`) と同じ (情勢, 記事)、R18 (§7)、要約埋込なし、
  題名・要約の欠け

## 3. 期間と分割

- 学習の組は 09-24 作成 (開設が 10-02 より前の情勢を含む)。判定の記事は事象時刻 10-02 以降で切る
- R18 で学習の記事と同じ閉包に入る記事の組は外す (§7)

## 4. 標本と重み

- 適格な組から**単純無作為に 200 組** (`random.Random(20261031)`、組を (situation_id, article_id) の昇順に
  並べてから shuffle して先頭 200)。重みなし (台帳 `weighting`)
- 規則 (anchor / nation / token) ごとの件数と率は副次で報告する (区間つき)

## 5. ラベルと検定

### 5.1 Opus のラベル (盲検、1 組 1 回、計 200 回)

- 入力は学習の正解集と同じ (`data/mlx/build_assign_goldset.py` の SYSTEM と形): 情勢の題名 + 強い固有名詞
  (cve・victim_org・malware_family・actor・actor_provisional・campaign、12 件まで) / 記事の見出し + 要約 600 字。
  規則・ML の確率・判定・埋込は見せない。3 択 same / related / unrelated。**正 = same**
- 判定者: Opus のサブエージェント。組の順は乱数で入れ替え、1 回に 25 組まで

### 5.2 確率なしの ML の判定 (本番の関数を通す)

- 特徴量は `assign_features.assign_feature_vector` (本番の SSoT)。cos_title = 記事の要約埋込 × 情勢の題名の埋込
  (snowflake-arctic-embed2。題名の埋込は保存が無いので Ollama の**埋込**で作る — 生成ではない)、
  cos_seed = 記事 × seed の記事 (`assigned_by='seed'`、t 以前に入ったもの) の要約埋込の平均、
  age_days = 記事の `created_at` − 情勢の `opened_at`
- `load_assign_model` と同じ検証で `assign_model.json` を読み、`probability ≥ threshold` を判定とする

### 5.3 指標・区間

- 適合率 P = |判定 ∧ same| / |判定|、回収率 R = |判定 ∧ same| / |same|
- 区間: **事象の根 (記事の最新の根、根の無い記事は記事自身) の cluster bootstrap 2,000 回**
  (`random.Random(20261031)`、95% 百分位)。Wilson の区間も併記
- 参照 R1・R2 との McNemar の正確検定 (正誤の不一致、両側)。報告のみ

## 6. 合否の線 (結果を見る前に固定、すべて満たして合格)

1. 適合率の点推定 ≥ 0.80 **かつ** bootstrap 95% の下限 ≥ 0.70
2. 回収率の点推定 ≥ 0.60
3. **判定不能**: ML が通す組が 25 未満、または same の組が 25 未満なら合否を出さない (床に届かない)

- 確率ありとの比較 (本番の今の構成との差) は §0 のとおり測らない。合格でも「確率ありと同等」とは書かない

## 7. 汚染の鍵 (R18)

- 学習側 = 662 組の記事、判定側 = 適格な組の記事。README の閉包 (最新の事象の根 ∪ CVE・被害組織の共有 ∪
  要約埋込 cos ≥ 0.80、`scripts/replay/_closure.py`)。ハブの切り方: CVE・被害組織が
  `relation_features.FOCUS_CVE_MAX` を超える記事はその欄を鍵にしない。`max_component_share = 0.30`
- 学習側の記事と同じ成分に入る記事の組は外し、数を報告する

## 8. 判定集の同一性

| id | path | sha256 | 行 | 作成 |
|---|---|---|---|---|
| `gold:assign_fresh_200` | `data/eval/rp5_3_assign_2026_10_31/gold.jsonl` | 実行時に manifest | 200 | RP-5-3 (Opus) |
| 学習 (判定に使わない) `gold:assign_train` | `data/mlx/assign_goldset.jsonl` | 実行時に manifest | 662 | 09-24 |

## 9. 変更履歴

(結果を見る前の変更も、ここに日時・項目・前後・理由を書く)
