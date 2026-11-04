# S26-ML-GOLD: S の切替での ML 学び直しの判定集 (detect・深刻さ C・relation・deep_dive) の事前登録

- 作成: 2026-11-04。**この文書のコミットが標本の選び出し (`scripts/s26_ml_gold/` の `select`) より前**。
  作成時点で標本・Opus のラベルは 1 件も存在しない。著者が見たもの: 台帳 (S26-ML-GOLD・S26-J-ML・
  S-SWITCH-RETRAIN・取り消した KJ-DETECT / KJ-DEEPDIVE / KJ-SEVC / KJ-REL・`gold_sets`)、
  `docs/s_switch_retrain.md` (ブランチ worktree-agent-af650020ef9527002)、`detect_decompose.md` (DETECT-JUDGE)、
  DETECT-JUDGE の裁定の**項目名と件数・合否の要約だけ** (`judge_r18/summary.json`)、各 ML の学習スクリプトと
  学習集合のファイル名、母集団の件数 (§2 の表、状態 × importance と記録の有無の数だけ)。
  記事の中身・各 ML の確率の分布は見ていない
- 対象の判断: S26-J-ML (s26 で学び直した ML の判定、SW-S26 を出すか) の **判定集を作るだけ**。
  この文書は判定集の母集団・標本・問い・汚染の鍵と、判定集の成立条件を固定する。
  S26-J-ML の合否の線は S26-J-ML の prereg (台帳の `stats.prereg` = `s26_ml_retrain.md`、未作成) が持つ。
  本書の §6 は「判定集として使ってよいか」の線
- 台帳との食い違い (調整役の確認が要る): 台帳の S26-ML-GOLD の `stats.prereg` は `s26_ml_retrain.md` を指すが、
  依頼により本書 (`s26_ml_gold.md`) を判定集の prereg にした。§0・§2.4 の判断で台帳の記述と変わる点がある

## 0. 結論: detect の 60 は gold:detect_fresh_1027 (DETECT-JUDGE の裁定) で賄えない

DETECT-JUDGE の裁定 (`data/eval/detect_decompose/judge/verdicts.jsonl` 260 行、正式はやり直しの `judge_r18/`) は、
detect ML の学び直しの判定 (候補の順位の AUC と上位 30 の開設すべきの回収) に使えない。理由:

1. **問いが違う**。裁定の単位は「腕が開いた claim」= (窓, 記事 id の集合, claim 文) で、正しい開設 = Q1 (追跡中の
   情勢と別の事象) ∧ Q2 (追跡価値) ∧ Q3 (claim 文が見出し・要約の範囲内)。detect ML の目的変数は
   **記事 1 本ごとの「新しい追跡事象を開くべきか」** (`gold:detect_goldset` の `gold_open`、
   `scripts/build_detect_goldset.py` の問い)。Q3 は **S が書いた claim 文**の忠実さで、S に依存する
2. **母集団が違う (負例が無い)**。裁定はどちらかの腕が**開いた**単位だけ。AUC と上位 30 の回収には、
   開かなかった候補 (負例の大半と見逃し) のラベルが要る。裁定にはそれが無い
3. **今の版で選ばれた記事だけ**。単位の記事は、今の detect ML (s24ci65 の軸) の top-K で絞った後の候補から
   LLM が開いたもの。学び直し前後の順位を比べると、今の版の順位で選別済みの記事だけを使うことになり今の版に有利
4. Q1 は窓の開始時点の**台帳の状態** (追跡中の題名) に依存し、記事の事実ではない。detect ML の正解は
   吸収の判断を含まない (吸収は割当の履歴から導く、build_detect_goldset の設計)
5. その窓 (10-27〜11-04) の裁定は結果を見たうえで書き手の直し (DETECT_WRITER_GATES) の調整に使われた
   (DETECT-REJUDGE-PREREG の notes)。detect ML の調整ではないが、再利用は避ける

Q2 (追跡価値) は `gold_open` の一部の軸に近いが、単位 (記事の集合) と母集団 (開いたものだけ) の違いは残る。
**detect の Opus 60 は減らさない** (S26-ML-GOLD の Opus は 240 のまま)。
台帳の直しが要る: `gold:detect_fresh_1027` の `created_by` は DETECT-JUDGE、S26-J-ML の detect の判定集は
本書が作る `gold:detect_ml_s26_fresh` (新しい id) に替える。

## 1. 仮説 (S26-J-ML が本書の判定集で確かめること。線は S26-J-ML の prereg)

s26 の S 由来の特徴で学び直した 4 本 (detect・severity_s3・relation・deep_dive) は、同じ判定集で、今の版
(s24ci65 の特徴) より順位・判定が悪くない (非劣性)。各判定集のラベルは **S に依存しない** (記事・窓・対の事実を
本文から問う) ので、S を替えても作り直さない。pair は F3 (`gold:f3_v2_pair_shadow`) を使い、本書の対象外。

## 2. 母集団

共通: 表 `articles` は同じ `article_id` の複数行を `created_at` の最新の 1 行に畳む。時刻は事象時刻
(`src/storage/event_time.py` の `EVENT_TS_EXPR`)。

### 2.1 detect (`gold:detect_ml_s26_fresh`、記事単位)

- 事象時刻が **2026-10-27T00:00Z 以上 2026-11-04T00:00Z 未満**、`status='posted'`、保存の importance が high / medium
  (detect の入力の段と同じ。Grok を含む)
- 割当済み (追跡中の情勢に入った) 記事も除かない: 正解は「この記事は追跡事象として開くに値するか」で、
  吸収は割当の履歴から導く (§0 の 4)。S26-J-ML で残余だけに絞って報告するのは S26-J-ML の自由
- 作成時の件数: posted ∧ high 285 / posted ∧ medium 1,189 (計 1,474)。うち本番の detect ML の影の記録
  (`detect_ml_shadow`、本番の上位の候補) にある記事 49

### 2.2 深刻さ C (`gold:sevc_s26_fresh`、記事単位)

- 事象時刻が §2.1 と同じ範囲、`status='posted'`、`severity_c_shadow` に `skip_reason IS NULL` の行がある記事
  (本番で問いを当てた記事 = C が判定する母集団。非サイバー・派生・まとめは A の決まりで先に外れている)
- 作成時の件数: 1,073

### 2.3 relation (`gold:relation_s26_fresh`、事象の対)

- 事象 = `src/eventnews/relation_features.load_event_features(repo, days=60, now=2026-11-04T00:00Z)` (本番と同じ
  60 日の窓・統合済みは除く)。候補の対 = `src/eventnews/relations._candidate_pairs` (本番の候補の作り方。珍しい
  指標か主題アクターを共有する対)
- 対象の対: 2 つの事象の `first` の遅い方が §2.1 の範囲に入る対 (早い方は 60 日の窓の中ならよい)
- 除外: 同じ事象どうし、どちらかの事象が relation の学習の正解集 (`data/mlx/event_relations_goldset*.jsonl` の鍵) に
  載る対 (直接の重なり)、§7 の汚染

### 2.4 deep_dive (`gold:deep_dive_s26_fresh`、窓 × 記事)

- 窓 = 本番の週次の深掘りと同じ **168 時間**。窓の終わり = **日曜 16:00 UTC** (本番の実行 15:56 UTC 前後)。
  **2026-11-01・11-08・11-15・11-22・11-29・12-06・12-13 の 7 窓** (重ならない)
- 窓の母集団 = `src/digest/db_filter.fetch_for_deep_dive_candidates(lookback_hours=168, now=窓の終わり,
  pool_max=180, novelty_excluded_dedup_keys=窓の終わりより前の f1_selections の dedup_key)` の候補
  (`scripts/harvest_deep_dive_labels.py` の教師と同じ 180 の幅。ML が並べ替える範囲)
- **台帳の「10-27 以降の 12 窓」から変えた (調整役の確認が要る)**: 重ならない 168 時間の窓は 10-27 から
  11-05 までに 1 つしか作れない。12 窓を 10-27 以前へ伸ばすと学習の窓 (06-22〜09-14) と `deep_dive_opus_12`
  (08-31・09-07・09-14) に重なり、10-26 より前は fast ティアが s23i で、今の版 (s24ci65 の特徴) の比較が
  特徴の食い違いになる。s26 の切替 (12-18) より前に終わる 7 窓を、窓が閉じるたびに選び出す。
  **11-06 に作れるのは第 1 窓 (11-01) だけ**で、残りは 12-13 の窓が閉じた後 (12-14) までに順に作る
- 「Opus の選定との一致」は記事ごとの 3 軸のラベル (§5) に替える (60 回で 7 窓の選定を Opus に当てさせることは
  できない)

### 2.5 除外して数える欄 (各判定集で件数を manifest に書く)

(1) 本文が読めない記事 (本文が空)、(2) s26 の教師に入った記事 (§7.2)、(3) R18 の汚染 (§7.1)、
(4) 重複行 (畳む)、(5) relation: 直接の重なり・同じ事象どうし

## 3. 期間と事象時刻の分割

- 各 ML の学習・調整の期間:
  detect = `detect_goldset` (08-10〜09-23 前後、held-out 08-13〜09-09) と教師 (06〜09 月)、
  severity_s3 = `severity_gold_v5_final` + `v4` (604 件、10 月中旬までの記事)、
  relation = `event_relations_goldset_v3` (408 対、09 月)、
  deep_dive = `deep_dive_labels` (13 窓、06-22〜09-14)・`deep_dive_opus_12` (08-31〜09-14)
- 判定集の期間は **2026-10-27 以降** (detect・C・relation は 10-27〜11-04、deep_dive は 10-25T16:00Z〜12-13T16:00Z)。
  10-27 は D1 (深刻度の軸を本文入力 + s24ci65 へ) の切替日で、以降の保存の特徴が今の版 (s24ci65) にそろう
- **再判定 DETECT-REJUDGE の窓 (09-24〜10-26) とは交わらない**。前回の DETECT-JUDGE の窓 (10-27〜11-04) とは
  交わってよい: そこで調整したのは detect の書き手 (claim の関門) で ML ではなく、本書の問い (記事単位の開設) は
  DETECT-JUDGE の問いと別。重なった記事の数は manifest に書く (`detect_judge_overlap`)
- 本書は判定集を作るだけで、どの ML の調整にも使わない。S26-ML-RETRAIN は本書の記事を学習に入れない

## 4. 標本と重み

乱数種 **20261106**。各層で記事 id (relation は対の鍵) の昇順に並べ、`random.Random(種 + 層の通し番号)` で
shuffle し先頭から取る (層の順は下の表の順)。層の母集団が割当より少なければ全件を取り、不足分は同じ判定集の
次の層へ足す (表の順、最後の層の不足は最初の層へ)。重み = 層の母集団 N_h / 標本 n_h。

| 判定集 | 層 (表の順) | 割当 | 計 |
|---|---|---|---|
| detect | A: 本番の影の記録 `detect_ml_shadow` (窓の中の run) にある / B: A 以外の high / C: A 以外の medium | 24 / 20 / 16 | 60 |
| 深刻さ C | 記事ごとの最新の `severity_c_shadow` の `p_s3` で H: ≥ 0.50 / M: 0.20〜0.50 / L: < 0.20 | 24 / 20 / 16 | 60 |
| relation | V: 珍しい被害組織か CVE を共有 / K: (V でなく) 珍しいマルウェアかツールを共有 / S: 主題アクターだけを共有 | 30 / 15 / 15 | 60 |
| deep_dive | 窓ごとに I: 母集団の順位 1〜60 (関門 A の内側) / O: 61〜180 | 窓 1〜4 は 6 / 3、窓 5〜7 は 5 / 3 | 60 |

- 層は**今の版や本番の出力で切る** (detect の A・C の p_s3)。陽性がまれで無作為では AUC が測れないため。
  両腕を同じ重みつきの標本で測る (Horvitz-Thompson) ので、層の切り方は腕の比較を偏らせない (分散は増える)
- deep_dive の窓の種 = 20261106 + 窓の番号 × 10 (+ 層の番号)
- 標本のほかに、detect は**母集団の全 id と層** (`population.jsonl`) を残す (S26-J-ML が同じ母集団を両腕で並べ直し、
  上位 30 の回収を重みで推定するため)。deep_dive も窓ごとの母集団 (順位つき) を残す

## 5. ラベルの問い (Opus 盲検、S に依存しない材料だけ)

付け手は新しい文脈の `claude-opus-5-5`。渡すのは**見出し・出典名・公開日・保存の本文 (原文)**。**渡さないもの**:
要約・事実の欄・種別・軸・確率・層・本番の判断 (いずれも S の出力か、腕の情報)。本文は
`data/mlx/build_severity_gold.py` の `BODY_MAX` で切る (既存の深刻さの正解と同じ扱い、切ったことを本文の末尾に書く)。
呼ぶ順は乱数で入れ替える。本文の sha256 を選び出しの時点で記録し、ラベルの時点で一致しない記事は付けない (件数を報告)。

| 判定集 | 問い (基準の文の出典) | 陽性 |
|---|---|---|
| detect | `scripts/build_detect_goldset.py` の `_SYSTEM`・`_TEMPLATE` (importance・trackable・open・watch)。材料の「要約」を「本文」に替える | `open = true` |
| 深刻さ C | `data/mlx/build_severity_gold_v5.py` の `RUBRIC_V5`・`QUESTION`・`SeverityAdjV5` (版 2026-10-22.10)。材料は見出し・公開日・本文 (要約・事実は渡さない) | `build_severity_gold_v5_final.final_severity` が S3 |
| relation | `data/mlx/judge_event_relations.py` の `SYSTEM` (関係の定義 v3)。材料は事象ごとに構成記事 (事象時刻の早い順に最大 3 本) の見出し・本文 | `follow_up` / `side` / `contains` |
| deep_dive | `scripts/build_deep_dive_goldset.py` の `_SYSTEM`・`_TEMPLATE` (urgency・need_to_know・depth)。材料の「要約」を「本文」に替える | `need_to_know ≥ 2 ∧ urgency ≤ 1 ∧ depth ≥ 2` |

- 基準の文の出典ファイルの sha256 をラベルの manifest に残す (`data/mlx/` は git 外のため)
- 判断不能 (付け手が材料不足と答えた・構造化の失敗) は判定集の外へ (件数を報告)。2 回目の独立な裁定は
  予算の外 (台帳 240) なので行わない。一致率は報告しない (限界として書く)
- 裁定は `data/mlx/adjudication_cache` を通し、`data/eval/adjudication_ledger.jsonl` に記録 (同時に走らせる裁定は 1 本、
  `data/eval/adjudication.lock`)

## 6. 判定集の成立条件 (結果を見る前に固定。満たさない判定集は S26-J-ML で使わない)

- 各判定集で、判断可能なラベルが標本の **80% 以上**
- R18 の閉包の最大成分の占有率 **< 0.30** (§7.1 の段で満たせなければ判定集を作らない)
- 陽性の数 (重みなし) が **5 以上** (detect・C・relation・deep_dive それぞれ)。5 未満なら AUC を出さず
  「判定不能」と S26-J-ML へ渡す (層・割当は変えない。足すなら新しい標本で本書の変更履歴から)
- Opus の新規呼び出しの上限は見積もり 240 の 1.3 倍 = **312** (キャッシュ当たりは数えない、失敗は数える)

S26-J-ML で使う指標 (参考。線は S26-J-ML の prereg): detect = 重みつき AUC・日ごとの母集団での上位 30 の回収、
C = 重みつき AUC・閾値 0.50 の適合と回収、relation = 閾値での適合と回収、deep_dive = 窓を塊にした重みつき AUC。
区間は塊の bootstrap (detect・C = 事象の根、relation = 対、deep_dive = 窓)。

## 7. 汚染の鍵

### 7.1 R18 (README の閉包)

- 学習・調整側の記事 (判定集ごと):
  - detect: `detect_goldset.jsonl`・`detect_gold_kinds.jsonl`・`detect_heldout_kinds.jsonl`・`detect_labels.json`・
    `data/mlx/teacher/detect*.jsonl` に現れる記事 id (DETECT-JUDGE §10 と同じ取り方、DB に実在する語だけ)
  - 深刻さ C: `data/eval/severity_gold_v5_final.jsonl`・`severity_gold_v5.jsonl`・`severity_gold_v4.jsonl` の記事
  - relation: `data/mlx/event_relations_goldset*.jsonl` の鍵の事象 (と、その事象へ統合された事象) の構成記事
  - deep_dive: `data/mlx/deep_dive_labels.jsonl`・`deep_dive_goldset.jsonl` の記事
- 閉包の辺 = 最新の事象の根 ∪ CVE・被害組織 (正規化後) の共有 ∪ 要約埋込 (`snowflake-arctic-embed2`) の cos ≥ 0.80。
  1 記事が 3 つを超える値を持つ欄は鍵にしない (`FOCUS_CVE_MAX`)。最大成分 < 0.30 を保つ段は
  `scripts/gpu_window/detect_closure_core.LADDER` を L0 から順に試し、最初に満たす段を使う (DETECT-REJUDGE と同じ)。
  閉包は 4 本の学習側の和集合と全標本の母集団を 1 つの universe にして 1 回作り、汚染は判定集ごとの学習側で判定する
- 汚染した記事 (relation は対のどちらかの事象の構成記事のどれか、deep_dive は記事) を**抽出の前に**母集団から除く。
  成分は割らない。除いた数を層ごとに報告する

### 7.2 S 固有の汚染 (s26 の教師)

s26 の教師に入った記事では s26 の特徴が教師を覚えた値になり、s26 の腕に有利になる。

- 選び出しの時点で存在する s26 の学習データ (`data/mlx/dataset_s26*/{train,valid}.jsonl`) に、記事 id・URL・
  見出し (8 字以上) のいずれかが部分一致で現れる記事を母集団から除く (relation は構成記事のどれか)
- 標本の記事の id・URL・見出しを `exclusion_keys.json` に書き出す。**S26-IMP-REBUILD (11-14) の前に** 台帳の
  `gold_sets` に本書の判定集を files つきで登録し、`guard_s26.json` を作り直して s26 の教師から外すこと
  (`make_guard_s26.py` は `tainted: false` で files のある判定集の記事を拾う)。登録が間に合わなければ、
  S26-J-ML で s26 の教師との重なりを数え直し、重なった行を除いて報告する

## 8. 判定集の同一性

| id | path (データの置き場の下) | sha256 | 行 | 作成 |
|---|---|---|---|---|
| `gold:detect_ml_s26_fresh` | `data/eval/s26_ml_gold/detect/gold.jsonl` | ラベルの完了時に manifest へ | 60 (判断不能を除く) | S26-ML-GOLD |
| `gold:sevc_s26_fresh` | `data/eval/s26_ml_gold/sevc/gold.jsonl` | 同上 | 60 | S26-ML-GOLD |
| `gold:relation_s26_fresh` | `data/eval/s26_ml_gold/relation/gold.jsonl` | 同上 | 60 | S26-ML-GOLD |
| `gold:deep_dive_s26_fresh` | `data/eval/s26_ml_gold/deep_dive/w<窓>/gold.jsonl` (7 窓) | 窓ごとに | 計 60 | S26-ML-GOLD |

- 標本 (`sample.jsonl`) とその sha256 は選び出しの時点で manifest に残す。本文は書かない (sha256 だけ)
- 判定にしてはならない集合 (`detect_goldset`・`severity_gold_v5_final`・`v4`・`event_relations_goldset_v3`・
  `deep_dive_goldset`) と path / sha256 が同じでないことを manifest で確かめる

## 9. 実行の前提

- 本書のコミットが実行の HEAD の祖先 (`require_prereg("s26_ml_gold", …)`)
- 本番 DB は読むだけ (`docker exec postgres psql` を read-only のセッションで、ホストの Python の接続は
  `PGOPTIONS=-c default_transaction_read_only=on` + read-only の接続)。LLM・埋込の計算は呼ばない
  (要約埋込は保存済みの値だけ)
- ラベルの段 (Opus) は**調整役が本書を確認した後**に `label --apply` で走らせる。既定は dry-run

## 10. 変更履歴

- 2026-11-04: 初版
