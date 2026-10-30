# RP-2: pair の LLM なしの判定 (EVENTNEWS_PAIR_LLM=0) の事前登録

- 作成: 2026-10-30。**この文書のコミットが枠 A ② (GPU-A、pair の同じ入力の対照) より前**。
  作成時点で RP-2 の両腕の判定 (LLM あり / なしの `joins`)、群の単位の再生、Opus の裁定、
  枠 A ② の出力は 1 件も存在しない
- 対象の判断: SW-PAIR (旗 `EVENTNEWS_PAIR_LLM` 1 → 0、`step:pair_judge` の上書きを外す) を出すか
- 判定する構成 (= SW-PAIR が出荷する構成): `EVENTNEWS_PAIR_LLM=0` の経路 (PAIR-LLM-OFF の実装)、
  SW-KIND の学び直しの `config/models/pair_model.json` (KIND-RETRAIN が作る版。sha256 は実行時に manifest)、
  種別の出どころ `EVENT_KIND_SOURCE=emb`
- 設計の根拠: `data/eval/arch_stocktake_2026_10_29/INTEGRATED_DESIGN_v6.md` §3.1、計画台帳の `RP-2`

### 0. 汚染の記録 (v6 §3.1 の指示で冒頭に書く)

- v5 のレビューの 1 本が、走らせる前に試算した数を出している (重みなしの回収 0.917 → 0.786、
  重みつき 0.878 → 0.841、記録の `ml_joined` を使った可能性あり)。本書の著者 (2026-10-30) もこの数を読んだ
- そのため合格線 (§6) は**試算から決めず、目標の側から決めた**。群化の回収は目標 80% に対し本番の
  測定 46% (`docs/accuracy_targets.md` §4.4) で大きく未達なので、回収の低下は実質的に許さない
- 著者がこの文書のために見たもの: 主解析の 118 組の層とラベルの件数 (§2)、特徴の欄の数。
  **両腕の判定・確率・`ml_joined` は見ていない**

## 1. 仮説

`llm_same`・`llm_known` を欠け (0, 0) にして `threshold_llm_off` で判定しても、退役する s23i の
`llm_same` を使う今の判定と比べて、(a) 同じ事象の組の回収が実質的に下がらず、(b) 統合の適合
(誤った結合の少なさ) が下がらず、(c) 群の単位で「LLM なしだけが誤って吸収する」ことが増えない。

## 2. 母集団

### 2.1 対の単位 (主解析)

- 判定集 `gold:f3_v2_pair_shadow` (§8) のうち `source = pair_shadow` の 480 組を、`event_pair_shadow` の
  `left_id`/`right_id` (両向き) に結び、`llm_same IS NOT NULL` かつ `observed_at` が s23i の期間
  `2026-10-10T00:34:00Z` 〜 `2026-10-26T11:22:59Z` の行を持つ組 (`scripts/gpu_window/build_pair_input.py`
  の定義、`EXPECTED_PAIRS = 118`)
- 作成時点の件数 (SELECT で数えた): **118 組**。層 production 36・format_diff 24・cve_other_action 20・
  actor_other_op 17・bulletin_vs_item 8・roundup_vs_item 7・same_format_other 6。ラベル same 51・
  roundup 29・different 19・lineage 16・same_actor_other_op 3。`features_json` は全て 23 欄
- 同じ組に s23i の期間の行が複数あるときは `observed_at` の最も早い行を使う
- 除外して数える欄: (1) 19 欄の行 (kind の 4 欄が無い。主解析から外し、感度として kind=other で埋めた結果を出す。
  作成時点で 0 組)、(2) R18 で学習集合と同じ閉包に入る組 (§7、別の層で報告)、(3) 実行時に記事が DB から
  読めない組
- 副次の報告: 他のモデルの `llm_same` の期間の組 (〜10-10・10-26〜10-29) は判定に使わず参考に出す

### 2.2 群の単位

- `event_pair_shadow` の s23i の期間の全行: `2026-10-10T00:34:00Z` 〜 `2026-10-26T11:22:59Z` と
  `2026-10-29T00:12:00Z` 〜 `2026-11-12T15:00:00Z` (= 11-13 00:00 JST、固定)。v6 §1.1 の時点で
  4,840 + 205 行。2 つの期間は別々に再生し、層として報告する

## 3. 期間と事象時刻の分割

- 閾値 (`threshold_llm_off`) は学習時に決まっている (§9 の前提) ので、RP-2 の中で調整するものは無い。
  **全期間を判定に使う** (README の R18: 調整が無い replay は分割しない)
- 閾値・特徴を RP-2 の中で動かすことはしない。動かす必要が出たら、この判定は不合格扱いにし
  §6.4 の代わりの案へ進む (結果を見てから閾値を選ばない)

### 3.1 入力の時点 (R17)

| 入力 | 種別 | 扱い |
|---|---|---|
| `features_json` の 19 欄 (cos・共有・時間差など) | (i) 当時の記録 | そのまま |
| `features_json` の kind の 4 欄 | 当時の LLM の種別 | **出荷する構成に合わせ、E の種別 (`article_kinds`、KIND-BACKFILL の後の値) から `event_kind.kind_pair_features` で計算し直す** (iii、これから本番に入れる判断なので今の値が正しい)。記録の 4 欄のままの結果を感度として出す |
| `llm_same` (LLM ありの腕) | (i) s23i の記録 | `llm_known = 1` として使う |
| 群の再生の記事・埋込・entity | (i)/(iii) | 再生の時刻 t までに `created_at` がある記事・`joined_at ≤ t` の参加だけを使う (群は今の `event_items` から取らない) |

## 4. 標本と重み

- 対の単位: 118 組の**全数** (抽出なし)
- 重み: F3 の `weight` (層の母集団 / 抽出数、本番の層の分布へ戻す)。118 組の重みの合計 20,368.955
  (最小 1.0・最大 558.96)。主の推定は全層を重みつきで合わせた値
- 難しい層 (format_diff・cve_other_action・actor_other_op・roundup_vs_item・bulletin_vs_item・
  same_format_other) は層ごとに**重みなし**で別に報告する (判定には使わない)
- 群の単位の裁定の標本: 腕で吸収のされ方が違う単位 (§5.3) が 150 を超えたら、
  (向き = なしだけが吸収 / ありだけが吸収) × (期間 2 つ) の 4 層に比例配分して計 150 を
  `random.Random(20261102)` で抜く (単位の id の昇順に並べてから shuffle)。重みは層の母集団 / 抽出数

## 5. 対の検定と手順

### 5.1 両腕 (R20: 本番の関数を通す。記録の `ml_joined` は使わない)

- **LLM ありの腕**: 特徴 (§3.1) + 記録の `llm_same`・`llm_known = 1` → `PairModel.probability` →
  `PairModel.joins(llm_available=True)` (`threshold_llm_on`)
- **LLM なしの腕**: 同じ特徴 + `llm_same = 0`・`llm_known = 0` → `PairModel.joins(llm_available=False)`
  (`threshold_llm_off`)。PAIR-LLM-OFF の旗 0 の経路 (`judge_pairs` に llm を渡さない) と同じ値になることを
  実行の最初に 10 組で照合し、食い違えば実行しない
- モデルは両腕とも同じ `pair_model.json` (§9 の前提を満たす版)

### 5.2 対の単位の指標

- 正 = ラベル `same`、負 = それ以外 (roundup・different・lineage・same_actor_other_op)
- 重みつきの回収 R = Σw·[結合 ∧ same] / Σw·[same]、重みつきの適合 P = Σw·[結合 ∧ same] / Σw·[結合]
- 回収の低下 ΔR = R(あり) − R(なし)、適合の差 ΔP = P(なし) − P(あり)
- 区間: **層の中で組を復元抽出する bootstrap 2,000 回** (`random.Random(20261102)`)、95% パーセンタイル区間。
  1 回の復元抽出で選んだ同じ組に両腕を当て、ΔR・ΔP をその標本で計算する (対応のある bootstrap)。
  R・P は行の重みの比の推定なので、`scripts/replay/_stats.cluster_bootstrap_ci` (層の平均の重みつき和)
  はそのまま使えない — 同じ抽出の手順 (層ごとに組の数だけ復元抽出・百分位) で比を計算し直す。
  層 = F3 の `stratum`、抽出の単位 = 組 (同じ記事を含む組が複数あっても組単位)
- McNemar の正確検定 (両側、`mcnemar_exact`): same の組で回収の不一致 (ありだけ結合 / なしだけ結合)、
  same 以外の組で誤った結合の不一致。**報告するが合否には使わない** (合否は §6 の区間)
- 118 組の対応のある比較の幅は 約 6.5 点 (v6 の推測)。点推定と区間を両方出す

### 5.3 群の単位

- §2.2 の各期間について、記事を最初に観測した時刻の順に、空の状態から両腕で本番の `assign_article`
  (`src/eventnews/grouping.py`) に通して群を作る。辺の可否は各腕の `joins` を `pair_decision` で渡す。
  quorum・`blocked_by_different_victims`・統合 (`plan_merges`)・焦点の関門は**実行時の本番の旗の値**で同じく通す
  (旗の値は manifest に)。推移閉包では作らない
- 両腕の群を比べ、**吸収のされ方が違う単位** = ある記事の参加先 (既存の群への参加か新しい群か、どの群か)
  が腕で違うもの。向きを「なしだけが吸収」/「ありだけが吸収」に分ける
- 裁定 (Opus、2 回、独立): 単位ごとに、その記事と、各腕で同じ群になる記事 (時点 t までの記事の見出し・要約・
  公開日だけ。後の続報・腕の名前・確率は見せない、腕の順は乱数で入れ替え) を渡し、「同じ事象か」を
  `docs/event_identity_definition.md` の定義で判定させる。2 回が食い違う単位は「不一致」として数から外し件数を報告
  (2 回の一致率と κ を出す)
- 誤って吸収 = 吸収した側の群に、裁定で「同じ事象でない」記事が入った単位
- 検定: 「なしだけが誤って吸収」と「ありだけが誤って吸収」の数に McNemar の正確検定 (**片側 5%**、
  なしが多い向き)。層の重みつきの差も併記
- 裁定は `data/eval/adjudication_ledger.jsonl` に記録 (R23、同時に走らせる裁定は 1 本)

## 6. 合否の線 (結果を見る前に固定。すべて満たして合格)

1. **回収**: 重みつきの ΔR の点推定 ≤ 2 点 **かつ** bootstrap 95% 区間の上限 ≤ 6 点
2. **適合**: 重みつきの ΔP の bootstrap 95% 区間の下限 ≥ −3 点
3. **群の単位**: 「なしだけが誤って吸収」が「ありだけが誤って吸収」を McNemar (片側 5%) で有意に上回らない。
   **吸収のされ方が違う単位が 20 未満なら「判定不能」** (床に届かない判定を合格扱いにしない、R7)

- 統合の適合 ≥95% の絶対の線はここでは測らない (AT-1 で別に測る)
- 1 つでも不合格、または 3 が判定不能なら §6.4 へ

### 6.4 代わりの案 (1 つだけ)

- **P = S (s24ci65) の `llm_same`**: 枠 A ② (`scripts/pair_same_input_control.py`、同じ 118 組・同じ入力)
  の組ごとの出力 `<stamp>_pairs.jsonl` の s24ci65 の `llm_same` を LLM ありの腕に入れ、記録の s23i の腕と
  §6 の 1・2 で比べる (向き: S − s23i を「なし」の位置に置く)
- 枠 A ② は 118 組しか回さないので、P では §6 の 3 (群の単位) を測れない → **3 は「判定不能」と書き**、
  1・2 の結果とともに利用者の判断 (v6 §3.1: 「回収の低下を受けて LLM を外す」か「s23i の退役を 12-02 まで延ばして
  pair の学び直しを待つ」か、決定 12-01) へ渡す。新しい案を足さない
- 枠 A ② の s23i の出力 (同じ入力での再判定) は、記録の `llm_same` との一致率を**再現性の感度**として報告する
  (判定には使わない)

## 7. 汚染の鍵 (R18)

- pair の学習の 365 組 (`gold:pair_eval_sample` = `data/eval/eval_sample.json`・`eval_sample2.json`・
  `eval_sample3.json`) と、§9 の新しい `pair_model.json` の学習集合 (KIND-RETRAIN の記録) の記事を学習側、
  118 組の記事を判定側として、README の閉包 (最新の事象の根 ∪ CVE・被害組織の共有 ∪ 要約埋込 cos ≥ 0.80、
  `scripts/replay/_closure.py`、`R18_VERSION` を manifest に) を作る
- ハブの切り方: CVE・被害組織が `relation_features.FOCUS_CVE_MAX` (= 3) を超える記事はその欄を鍵にしない
  (一括の勧告・一覧が全体を 1 つに繋ぐのを防ぐ、RP-9 と同じ)。閉包の最大成分の占有率の上限
  `max_component_share = 0.30`
- 組のどちらかの記事が学習側と同じ成分に入る組は**主解析から外し**、別の層として同じ指標を報告する
  (v5 の確認では学習 365 組との直接の重なりは 0 組)

## 8. 判定集の同一性

| id | path | sha256 | 行 | 使う行 | 作成 |
|---|---|---|---|---|---|
| `gold:f3_v2_pair_shadow` | `data/eval/f3_gold_v2.jsonl` | `2182dc0b170a2a2bad87255c97b1b472ac871d0729596096bb84f28c0996302d` (git blob `7a7e58a5da6d`) | 600 | `source = pair_shadow` の 480 → 主解析 118 | F3 (10-28 の付け直しの後の `label`) |

- 判定にしない集合: `gold:pair_eval_sample` / `gold:pair_train` (pair_model の学習)
- 群の単位の Opus の裁定 (最大 150 × 2) は RP-2 の出力として作り、他の判定に流用しない

## 9. 実行の前提 (満たさなければ実行しない = 判定不能)

- 新しい `pair_model.json` (KIND-RETRAIN) の学習・閾値の選定・早期終了に F3 (`f3_gold_v2.jsonl`) の行を
  使っていないこと。使っていれば F3 は tainted で RP-2 は判定不能 (調整役に戻す)。KJ-PAIR が F3 で判定するのは
  調整ではないが、**KJ-PAIR の結果を見て pair_model を作り直した場合も** F3 は tainted とする
- `threshold_llm_off` は学習時の値をそのまま使う (RP-2 で選ばない)
- `require_prereg("pair_llm_off", …)` (scripts/replay/_gate.py) が通る (この文書のコミットが実行の HEAD の祖先で、
  実行開始より前)
- manifest に: `prereg_git_sha`・`prereg_blob_sha256`・HEAD・`pair_model.json` の sha256・F3 の sha256・
  model_tiers の版・実行時の旗 (`EVENTNEWS_PAIR_LLM`・`EVENT_KIND_SOURCE`・統合と焦点の関門)・R18 の版と
  閉包の占有率・各除外の件数

## 10. 変更履歴

(結果を見る前の変更も、ここに日時・項目・前後・理由を書く。結果を見た後の変更は探索的扱い — README)
