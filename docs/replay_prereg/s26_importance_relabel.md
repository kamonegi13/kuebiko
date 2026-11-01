# s26 の要約の教師の importance の付け直し (v8r7) と、その判定 — 事前登録

計画台帳: S26-IMP-PREREG (この文書のコミット) → S26-IMP-GOLD → S26-IMP-PRECHECK → S26-IMP-RELABEL →
S26-IMP-REBUILD、判定は EVAL-S26 の 1 指標 (§6-C。EVAL-S26 の stats.prereg `docs/replay_prereg/s26_eval.md` は 11-02 時点で未作成 —
作るときに §6-C を逐語で取り込む) と、その判定集を作る S26-IMP-RP9-GOLD。
決定: 利用者 2026-11-02 (S26-TEACHER の decided_20261102、U1〜U6)。

## 0. 前提: s25 の不合格の原因 (探索的な分析。合否には使わない)

出典は `data/eval/s25_eval/score.py` の再実行と記事ごとの突き合わせ (正解 high 64 件)。

1. **測る枠が違っていた**: 教師はまとめ記事を除く枠で合格した (`data/eval/s25_case_c_report.md` §2)。s25 の評価はまとめ記事を含めた。
   同じ除外の枠では s25 = triage 境界除く 24/24 (基線 24)・全件 27/32 (基線 29)・severity 21/26 (基線 21)。
2. **基線が同じ条件の腕ではなかった**: 基線は当時の保存の importance で、high の付けすぎがある (適合 26/39・25/76)。
   同じ入力の s24ci65 の腕 (`data/eval/s25_eval/driver.sh:51`) は飛ばした (`driver.log:1738`)。
3. **教師 high → s25 非 high が 6 件、逆は 0 件**。6 件とも正解集の境界の記事で、根拠は high-2 が 4 件 (分析の形の記事 × 複数組織の被害)、
   high-6・high-8 が各 1 件。境界でない記事は 24/24・17/17。
4. **教師の境の揺れ**: 2 回目の判定で 30 行中 2 行が high → medium (`data/eval/s25_case_c/jitter_result.json`)。教師は 1 回の判定だった。
5. 入力の基準 (v8r7) と見出しは、学習・評価・教師で一致していた。

この文書の手当て: 測る枠を事前に固定する (§6)、基線を同じ入力の腕にする (§6-C)、境の型の行を 2 回判定して多数決にする (§3)。
基準の文 (FLAT) は変えない (U1)。分析の形 × 複数組織の high を学習データで水増ししない (U5)。

## 1. 仮説

v8r7 の importance 節で付け直した教師で学習した s26 は、同じ入力・同じ枠で比べたとき、今の本番 (s24ci65 + v8r4) より
要約の importance の high の回収を下げず、high の適合を下げない。

## 2. 母集団

### 2.1 付け直しの対象 (S26-IMP-RELABEL)

- s26 の学習データの**要約の行すべて** (train + valid)。`data/mlx/build_dataset_s26.py` (S25_IMPORTANCE_RELABEL 空、
  S26_EXTRA_GUARD = `data/mlx/s26_teacher/guard_s26.json` sha256 87f347c8…) の組み立てで決まる。
- dry-run (`data/mlx/dataset_s26_dry_manifest.json`, 2026-11-01) の値: 要約は train 934 + valid 60 = **994 行**。
  付け直し前の内訳は high 188 / medium 455 / low 351。
- 付け直しの実行の直前に同じ設定で組み直し、行数と行の鍵 (key) の一覧の sha256 を manifest に書く。994 と違えば差と理由を書く。
- **後から足される要約の行** (GROK-LINK-JUDGE 11-11 が on になり、リンクつきの要約の教師を入れる場合など) は、同じ指示・同じ手順で
  追加分だけ付け直す (§3。線は §6-B と同じ。件数を別に数える)。既に付け直した行は再判定しない。
- s25 の付け直し表 (`data/mlx/teacher/summary_importance_v2_s25.jsonl`) は読まない。判定役には今の importance を見せない (盲検)。
- 除外する行と数える欄: 判定の失敗 (3 回再試行の後) / 地政学の high (build が止める) / 12,500 トークン超 (build の既定)。

### 2.2 事前確認の判定集 gold:imp_precheck_s26 (S26-IMP-GOLD、新規)

- 候補: articles のうち、事象時刻 (`src/storage/event_time.py` の `EVENT_TS_EXPR`) が
  **2026-10-13 00:00 以上 2026-11-02 00:00 未満 (JST)** (11-02 の変更、§12)。
  条件は status = posted (analysis_only を含む)・`duplicate_of IS NULL`・本文 100 字以上・article_id ごとに最新の 1 行。
- 除外 (R18 の閉包、§7): s26 の学習の全課題の記事 (dry-run の全行の article_id・見出し・原題・URL ∪ guard_s26.json) と、
  台帳の `gold_sets` の tainted 全集合と、他の判定集 (gold:triage_fresh_s26・gold:triage_fresh_s26_supp・gold:rp9_frozen) に
  繋がる成分を外す。外した数を出どころ別に書く。
- 抽出: 保存の importance で層化。high は全件 (50 件を超えたら 50 件)・medium 50・low 50。乱数種 **20261102**。
  層の母集団の件数 N_h を manifest に書く。
- 正解の付け方:
  - Opus 5.5・effort **high**・**1 回 1 件**、指示は §3 と同じ文。
  - 2 回独立に判定する (呼び出しの順を乱数種 20261102 と 20261103 で変える)。high / 非 high が割れたら 3 回目で多数決。
  - **2 回で割れた記事は境界** (`boundary=true`)。
- 作った後は教師の調整・学習に使わない。S26-IMP-PRECHECK で使ったら、台帳では tainted (used_for = 教師の事前確認) にする。
  EVAL-S26 の判定集にはしない。
- 正解と教師は同じ基準の文で付く。だから §6-A が測るのは「4 件まとめ・effort medium・1 回判定の教師が、1 件ずつ・high・
  多数決の正解からどれだけずれるか」で、基準の文そのものの良否は測らない。

### 2.3 判定集 gold:rp9_imp_s26 (S26-IMP-RP9-GOLD、U6)

- 対象: gold:rp9_frozen の記事 150 件 (`data/eval/rp9_frozen/frozen_articles.jsonl`、5 層 × 30、重み = 列 `weight`)。
- 本文は article_id で DB から読み、`body_sha256` が凍結時と同じか確かめる。違う記事は外して数える。
- 正解の付け方: Opus 5.5・effort high・1 回 1 件、指示は §3 と同じ文、1 回判定。出力に「境界か」(true/false) の欄を足す
  (判定役の自己申告。合否の枠から外す)。
- s26・s24ci65 の出力を見る前に作り、sha256 を固定する。

## 3. 判定役の指示 (教師・2 つの正解集で共通、逐語で固定)

- 指示の文 = `data/mlx/s25_case_c_relabel.py:55-80` の `RUBRIC_TEXT` と同じ構成。ただし importance 節の本文は
  `data/eval/S24A_rubric_v8r7.json` の importance 節を import して逐語で埋め込む。
  - v8r7 の importance 節 = v8r5 = FLAT_* の逐語 + v8r4 の地政学・研究・まとめ記事の段落。
  - FLAT の文は `src/tools/triage_flat_rubric.py` 版 **2026-10-08.8-flat** のまま (U1)。
- 札 `RUBRIC_ID = s26-imp-v8r7`。指示の sha256 を manifest に書く。
- 出力の欄:
  - `importance` (high / medium / low)・`criterion`・`reason` (150 字以内) — s25 と同じ。
  - **追加**: `confirmed_victim_orgs` (確認された被害組織の数、整数か null。攻撃者の主張だけの組織は数えない)・
    `confirmed_by` (被害者 / 当局 / 独立研究者 / 主張のみ / 無し)・`article_form` (事案の報道 / 分析 / 勧告 / まとめ / その他)。
  - 追加の欄は importance を決めない。学習の行にも入れない。§3.2 の検査にだけ使う。
- 教師の呼び方: Opus 5.5・effort medium・1 回 4 件まで・本文 40,000 字まで (s25 と同じ、`BATCH_N=4`・`BATCH_CHARS=48000`)。
  組み分けの乱数種は 1 回目 **20261104**、2 回目 **20261105**、3 回目 **20261106**。

### 3.1 境の型の行の 2 回判定 + 多数決 (U4)

- 2 回目を当てる行は次のどれか。
  - 1 回目の criterion が high-2・high-3・high-5・high-6・high-8 の行
  - 1 回目が medium で、criterion に「単一組織」「分析」「国家系」「主張」のどれかを含む行
  - §3.2 の検査に当たった行
- 2 回目は組み分けと順を変えて判定する (同じ指示)。high / 非 high が食い違えば 3 回目で多数決。
- 最終のラベル = 1 回だけ判定した行は 1 回目、2 回以上判定した行は多数決 (medium と low の食い違いは 3 回目の値)。
- 3 回で割れたまま (1:1:1) の行は medium にして `boundary=true` を記録する。学習から外さない。

### 3.2 一貫性の検査 (決定論。1 回目の後に当てる)

- (i) `confirmed_victim_orgs ≥ 2`、かつ `confirmed_by` が 被害者 / 当局 / 独立研究者 のどれか、かつ importance が high でない行
  (分析の形の記事を含む。FLAT_HIGH の前置き「分析・解析の形の記事でも…」)。
- (ii) importance が high で、criterion が high-2 なのに `confirmed_victim_orgs < 2` または `confirmed_by` = 主張のみ の行。
- 当たった行は §3.1 の 2 回目の対象に入れる。多数決の後も当たる行の数を §6-B の線で見る。

## 4. 期間と事象時刻

- 教師の行の期間は s26 の組み立てに従う。
- gold:imp_precheck_s26 は事象時刻 2026-10-13〜11-01 (JST、11-02 の変更)。教師と R18 の成分で分ける。
- gold:rp9_imp_s26 は RP-9 の凍結 (manifest の窓) に従う。

## 5. 対の検定

- §6-A・§6-B は教師 1 腕の水準の確認で、対の比較はしない。
- §6-C は同じ記事に s26 と s24ci65 の 2 腕を当てる。high の回収・適合の差は McNemar の正確検定 (両側、記録) と
  事象の根の cluster bootstrap 2,000 回の 95% 区間 (記録) で出す。合否は §6-C の線だけで決める。
- 線は AND で、多重比較の補正はしない。

## 6. 合否の線 (結果を見る前に固定)

枠は 3 つとも同じにする: **まとめ記事を除く** (`src/cti/roundup_detector.roundup_basis(見出し) == "title"`、見出し・DB の title・
original_title のどれかが当たれば除く) + **境界を除く**。まとめ記事・境界を含めた値は記録だけ。

### 6-A 教師の事前確認 (S26-IMP-PRECHECK。付け直しに進むかを決める)

§3 の教師の呼び方 (4 件ずつ・medium、§3.1 の多数決まで) を gold:imp_precheck_s26 に当てる。

- A1: high の回収 ≥ **0.95** (正解 high のうち教師 high の割合)
- A2: high の適合 ≥ **0.85**
- A3: §3.1 の 2 回目の対象の行で、1 回目と 2 回目の high / 非 high の一致 ≥ **0.90**
- 1 つでも外れたら止める (付け直しに進まない)。この集合で指示・線を変えて測り直さない。
  直すときは新しい標本と新しい登録からやり直す。
- 枠の中の正解 high が 20 件未満なら「判定不能」として止め、利用者に報告する。

### 6-B 付け直しの実行の関門 (S26-IMP-RELABEL。表を学習に差す前)

- B1: §3.1 の 2 回目の対象の行で、1 回目と 2 回目の high / 非 high の一致 ≥ **0.90**
- B2: 多数決の後に §3.2 の検査に当たる行 ≤ 母集団の **2%** (994 行なら 19 行以下)
- B3: 判定の失敗 (3 回再試行の後) = 0 行。地政学の high = 0 行
- 外れたら表を差さずに止めて報告する。
- 記録だけ: 付け直し前後の行列 (old → new)・根拠の内訳・2 回目の対象の行数・`boundary=true` の行数・
  まとめ記事 (見出しの規則) の行の high の数。

### 6-C 学習済み s26 の判定 (EVAL-S26 に足す指標。U2・U6)

- 判定集: **gold:rp9_imp_s26** (主)。
- 腕は同じ記事・同じ本文の 2 つ:
  - s26: v8r7、`SUMMARIZER_RUBRIC_V8R4=0`
  - s24ci65: v8r4、`SUMMARIZER_RUBRIC_V8R4=1` = 今の本番
- どちらも EVAL-S26 が rp9_frozen で作る要約の出力の `importance` を使う。この指標のために生成は足さない。
  ただし s24ci65 の腕は v8r4 で全 150 件を作る (EVAL-S26 側の要件として足す)。
- C1 (回収): 正解 high (枠の中) について、「s24ci65 だけが high」の件数 − 「s26 だけが high」の件数 ≤ **1**
- C2 (適合): 枠の中の high の適合が s26 ≥ s24ci65 (重みなしの点推定)
- 合格 = C1 かつ C2。
- 枠の中の正解 high が 10 件未満のときは、gold:triage_fresh_s26 ∪ gold:triage_fresh_s26_supp を判定集に足して、C1・C2 を合わせた集合で見る
  (この切り替えの条件もここで固定する)。
  - その場合も同じ 2 腕で要約を作る (本文入力。正解は見出し + 概要で付けたもので、入力の差を注記する)。
  - 層の重みは TRIAGE-GOLD-S26 の result の N_h/n_h。
- 記録だけ: 層の重み (rp9 の `weight`) で母集団に戻した回収・適合、McNemar の p 値、bootstrap の区間、valid 60 行での s26 と教師の一致。

## 7. 汚染の鍵 (R18)

- 事象の根 (`merged_into` を辿った根)・CVE・被害組織 (正規化後)・要約埋込の cos ≥ 0.80 の和集合で閉包を作る。成分は割らない。
- 教師側: s26 の学習の全行 (全課題) + 台帳の tainted 全集合。
- 判定側: gold:imp_precheck_s26・gold:rp9_imp_s26 (= rp9_frozen、既に教師側を除いて凍結済み)・
  gold:triage_fresh_s26 (+supp) (guard_s26.json で教師側から除外済み)。
- gold:imp_precheck_s26 は rp9_frozen・triage_fresh_s26 (+supp) とも成分で交わらないようにする (§2.2)。
- 閉包の版 (コードの HEAD・スナップショット) を manifest に書く。

## 8. 判定集の同一性

| 集合 | path | sha256 | 行数 | 作る項目 |
|---|---|---|---|---|
| gold:imp_precheck_s26 | `data/eval/imp_precheck_s26/gold.jsonl` | 作成時に記入 | ≤150 | S26-IMP-GOLD |
| gold:rp9_imp_s26 | `data/eval/rp9_imp_s26/gold.jsonl` | 作成時に記入 | ≤150 | S26-IMP-RP9-GOLD |
| gold:triage_fresh_s26 | `data/eval/triage_fresh_s26/gold.jsonl` | e5446d60… | 150 | TRIAGE-GOLD-S26 |
| gold:triage_fresh_s26_supp | `data/eval/triage_fresh_s26/supp_gold.jsonl` | d4603330… | 70 | TRIAGE-GOLD-S26 |

triage_flat_v1・severity_gold_v4・severity_gold_v5_final は tainted (`plan.yaml` gold_sets) なので、記録の参考にも使わない。

## 9. Opus の回数 (内訳。要約 994 行)

| 段 (項目) | 計算 | 回数 |
|---|---|---|
| gold:imp_precheck_s26 (S26-IMP-GOLD) | 150 件 × 2 回 + 3 回目 (割れ 約 8%) | **約 312** |
| 教師を precheck 集合に当てる (S26-IMP-PRECHECK) | 1 回目 150/4 = 38 + 2 回目 (約 30%) 12 + 3 回目 2 | **約 52** |
| 付け直し 1 回目 (S26-IMP-RELABEL) | 994/4 | 約 249 |
| 2 回目 (境の型 + 検査。s25 の表で同じ定義を当てると 289/999 = 29%) | 約 288 行/4 | 約 72 |
| 3 回目 (2 回目の行の約 8%) | 約 23 行/4 | 約 6 |
| 付け直しの計 | | **約 327** |
| 後から足される要約の行 (条件つき、≤40 行) | 1〜3 回 | **≤ 15** |
| gold:rp9_imp_s26 (S26-IMP-RP9-GOLD) | 150 × 1 | **150** |
| 計 | | **約 856** |

止める条件: 段ごとに見積もりの 1.3 倍を超えたら止めて報告する。

## 10. 日程の案 (週の上限 765 = 900 × 0.85。W1 = 10-30〜11-05。11-02 の check.py の週の計)

| 週 | 今の計 | 足すもの | 足した後 |
|---|---|---|---|
| W1 (〜11-05) | 35 | GOLD 312 (11-03)・PRECHECK 52 (11-04)・RELABEL 327 (11-04〜05) | **726** |
| W2 (11-06〜12) | 525 | RP9-GOLD 150 (11-06)・REBUILD のレビュー (batch の分) | 約 705 |
| W3 (11-13〜19) | 620 | 後から足される行 ≤15 (11-12 の判断の後、11-13) | ≤ 635 |
| W5 (11-27〜) | 610 | (S26-TEACHER-AUDIT 400 は据え置き。新しい表で組み直した教師を監査する) | 610 |

- 付け直しが W2 にずれ込むと W2 は 525 + 327 + 150 = 1,002 で上限を超える。ずれたら RP9-GOLD を W8 (280) へ移す。
  W8 は EVAL-S26 (12-16) の後なので、移す場合は EVAL-S26 を 12-18 以降にする必要がある。代わりの案は W7 で、
  655 + 150 = 805 > 765 になるため、別の項目を動かす判断が要る。
- W6 (1,135) は既に上限を超えている。この登録の項目は W6 に置かない。

## 11. 組み直し (S26-IMP-REBUILD)

- `S25_IMPORTANCE_RELABEL=<付け直し表> S24_OUT_NAME=s26 uv run python data/mlx/build_dataset_s26.py`。
  - 他は s25 と同じ: `S25_KIND_TEACHER=event_kind_kind_v2.jsonl`・`S25_ACH_STANDING_EXTRA=ach_standing_s25_fin.jsonl`・
    v8r7・薄い本文の教師 `summary_s25_thin.jsonl`・`S25_SUMMARY_MAX_TOKENS=12500`・S26_EXTRA_GUARD = guard_s26.json。
  - リンクつきの要約の教師は GROK-LINK-JUDGE の結果に従う。
- 付け直し表の形は s25 と同じ (key・split・origin・category・old・new・criterion・reason・rubric)。
  足すのは `judgments` (各回の判定) と `boundary` (3 回で割れた行)。
- 関門: `s24_gate.py` の全項目合格 + RP-9 の `check_teacher_gate.py`。
- 記録: 要約の high の数 (前 188 → 後)・由来別の high・まとめ記事の high。

## 12. 変更履歴

- 2026-11-02 初版 (下書き)。結果を見る前。
- 2026-11-02 (標本の選び出しの前・Opus の判定の前。見たのは層ごとの件数だけ): **gold:imp_precheck_s26 の窓の始まりを 10-20 → 10-13 に変更**。
  理由: 道具の dry-run (`scripts/s26_imp/build_imp_gold.py --dry-run`) で、10-20 の窓は合併の除外と R18 の後に保存の high が 24 件しか残らず
  (計 124 件)、§6-A の「枠の中の正解 high が 20 件未満なら判定不能」に当たる恐れが高い。窓ごとの R18 の後の件数 (high / medium / low):
  10-20 = 24 / 716 / 438、10-13 = 55 / 1,202 / 808、10-06 = 79 / 1,645 / 1,158、10-01 = 90 / 1,865 / 1,321。
  上限の 50 を埋める最も狭い窓 10-13 を採る。教師の要約の記事で事象時刻が 10-13 以降のものは約 2% (題で照合した 613 件中) で、R18 が成分ごと外す。
  他の規則 (層・乱数種・除外の源・R18 の版) は不変。
- 2026-11-02 (同上、道具の実装で決めた解釈): (a) 2 回目・3 回目の判定は、判定のキャッシュ (adjudication_cache) が同じ答えを返さないよう、
  質問の末尾に「[判定 N 回目]」を足して独立の呼び出しにする (指示の本文は不変、1 回目は従来の文のまま)。
  (b) 3 回目の判定: 正解集 (§2.2) は high / 非 high が割れたときだけ、教師 (§3.1) は食い違いがあれば (medium と low の食い違いも) 当てる。
  (c) §3.2 (ii) で `confirmed_victim_orgs` が null は「2 未満」として扱う。(d) §3.1 の criterion の照合は文字列に含まれていれば当たりとする。
  いずれも 1.3 倍の Opus の止め (§9) の中で行う
- 2026-11-02 (**結果を見た後の変更**。S26-IMP-RELABEL の 1 回目の実行 = B2 不合格の後。利用者の判断 11-02「事前登録を直して当て直す」):
  §3.2 (ii) の `confirmed_victim_orgs = null` の扱いを、**§6-B の B2 の数え方では「不明」として矛盾に数えない**に直す
  (§3.1 の 2 回目の対象の選択は実行どおり null を「2 未満」とみなしたまま — 選択を変えると組み分けが変わり判定が変わるため)。
  理由: 1 回目の実行で B2 に当たった 38 行のうち 27 行は、high-2 で数が null の行だった。読むと「7 万台超」「194 か国・
  8 万台超の認証情報」のように数え切れない規模の被害で、判定は基準どおり。null を「2 未満」とみなしたのは道具の実装時の
  解釈 (上の (c)) で、§3.2 の文 (「< 2」) にも合わない誤りだった。直した数え方では B2 に当たる行は 10 行 (上限 19)。
  判定 (Opus の出力) は 1 回目の実行のキャッシュをそのまま使い、追加の Opus は無い。この再判定は結果を見た後の変更による
  ものであり、EVAL-S26 の報告にこの経緯を書く。B1 (0.934)・B3 (失敗 0・地政学の high 0) は変わらない

