# 2 系統化の計画 — 本番の LLM を s24 (s 系) と n20 (n 系) だけにする (2026-10-15、案、Opus レビュー反映済み)

> 状態: 案。利用者決定 (10-15) 「本番の LLM 処理を s24 と n20 の 2 系統だけで処理できるように整理する」を受けた設計。
> 改訂: 10-15 の Opus レビュー (20 項目・要確認 4 点、`data/eval/two_family_review.md`) を 3 本すべてに反映済み。利用者判断の項目は「推奨で決定 (10-15)」と書く。
> 数字は出典つきの再掲、推測は「推測」と書いた。詳細: s 系 `s24_plan.md`、n 系 `n20_plan.md`。
> 出典の略: MT = 本番 model_tiers 版 40 (10-12、`app_config_versions`)、SR = `src/tools/model_tiers.py` の `STEP_REGISTRY`、
> TP = `src/tools/task_prefix.py`。
> **数字 (件数・Opus 回数・合否線) の SSoT は本書 §7 の表**。s24_plan・n20_plan の同じ数字は本表と一致させる。

## 0. 本番の現状 4 本 (正体を正しく書く)

| Ollama のタグ | 正体 | サイズ | 担当 (MT の上書き + ティア) |
|---|---|---|---|
| `kuebiko-sft:s23i` | **s 系の現行** (s23 と s22 の補間 i58、fast ティア) | 27 GB | fast ティア + `synthesis_analysis` + `synthesis_detect` + logprob 判定のうち thin・claim |
| `kuebiko-sft:n19` | **n19b** (n 系。`data/cloud-runs/n19b/DONE.json` 701 更新 × accum 4 = 2,804 ≒ `dataset_n19b` の train 2,805 = 事象 1,992・Spotlight 459・総括 354、valid 51。rank 64・30 層・lr 6e-5・max_seq 18,500。n20_plan §2 の旧記載「dataset_n19 1,477」は誤り) | 27 GB | narrative ティア + `digest_deep_dive` / `_select` / `synthesis_narrative` / `event_news` / `pir_spotlight` |
| `kuebiko-sft:26b` | **N1 = n 系の初版** (09-04、narrative 特化 SFT、MLX 8 層・4bit QLoRA、事象ニュースを学習。SYNTHESIS.md:185)。25.2B・Q4_K_M・二重量子化 (`c2c_x_report.md` §3.1)。**素の 26B ではない** | 16 GB | `article_translate`・`actor_sync`・`triage_geo_rescue` |
| `gemma4:26b` | **素の公式 26B** | 17 GB | `assign_evidence_judge`・`org_identity_judge` (logprob) |

- 含意: 今の翻訳は **n 系の弱い LoRA (8 層・4bit)** が担っていて、崩れていない。一方 n20 (bf16・30 層・rank 64) は JSON 偏りが強く、翻訳を学習しないと崩れる前提 (n20_plan §3.1)。
- `kuebiko-sft:26b` を「素の 26B」と書いた箇所は誤記 (docs・docstring を 10-15 に直した)。
- 外部: reasoning = `claudecode:claude-sonnet-5` (SR 上 reasoning ティアの step は `synthesis_analysis` だけで s23i に上書き済み = 実質未使用)、dialog = `claudecode:haiku`。

## 1. 現状の補足

- 実際に載っているのは `ollama ps` で s23i 36 GB (KV 込み・ctx 262144) のみ、unload まで 5 分 (今 10-15 時点の実測)。
  常駐は「呼ばれたときに載る」であり、4 本が同時に載るとは限らない (推測: 毎時の段が重なると 2〜3 本)。
- 目標: **s24 (27 GB) + n20 (27 GB) = 54 GB** (int8 取込み。int4 は効果が消える、`s24_plan.md` §4)。
  別プロジェクトの Ollama 利用 (約 20 GB、memory `noroshi_ollama_tenant_2026_09_11`) と合わせても 128 GB に収まる (推測、KV 込みで s23i 実測 36 GB を 2 本 = 72 GB)。
- **隠れた LLM 呼び出し**: `dedup_cluster_judge` (`.env` `DEDUP_CLUSTER_JUDGE=1`、`orchestrator.py:491` が `Step.TRIAGE` を借りて `dedup_judge.is_duplicate` を呼ぶ)。借用のため `TaskPrefixClient` 経由で `[task: triage]` の印が付く = s18 の課題取り違えと同型。失敗は黙って取り込む側に倒れる。**`Step.DEDUP_CLUSTER_JUDGE` の新設で借用は解消済み** (c7aebefa、STEP_REGISTRY に fast・300 秒で登録、印なし)。割当表 (§3.1) に行を足した。

## 2. 振り分けの原則

- **s 系 = 構造化出力・判定・抽出 (JSON か 1 トークン)**。n 系 = **自由な長文** (散文・翻訳・和訳)。
- 同じ LoRA に「JSON で答える癖」と「自由文」を混ぜると自由文が JSON に崩れる (10-02: 翻訳 637 件・PIR 別の要点が毎日全件、n19 も翻訳 5 件中 4 件、memory `untrained_freetext_steps_broke_2026_10_02`)。
  この原則は s 系に自由文を入れない根拠であり、**n 系に翻訳を入れる側のリスクでもある** (n20_plan §3 で対策: 翻訳なし n20a と翻訳あり n20b の 2 腕で干渉を切り分ける)。
- 判定 4 本 (logprob: thin・claim・assign・org) は s 系: 出力が 1 トークンで JSON の癖と同じ形式 (`{"answer": "` の直後) に揃えられる。ただし **P(Yes) が飽和する危険**がある (§5)。

## 3. 全 step の割当表

SR の全 29 step (c7aebefa で追加された `dedup_cluster_judge` を含む) のうち埋込 `embed` (LLM でない) を除く **28 step: s24 へ 17・n20 へ 7・外部 4**。
(旧版の「27 step: s24 へ 16」は `dedup_cluster_judge` の行を数えていなかった。)

「学習」の件数は案 (SSoT は §7)。「正解集」は評価専用 (学習に入れない)。
**「上書きが要るか」列**: P3 で step 上書きを全部消すと、`digest_deep_dive`・`digest_deep_dive_select`・`article_translate`・`actor_sync` (SR 上は FAST) が s24 に、`synthesis_analysis` (REASONING) が外部 Sonnet に落ちる。**消すのは旧モデルを指す上書きだけ**。恒久に残す上書きは列に明記する。

### 3.1 s24 へ (fast ティア)

| step | 今 | 上書きが要るか | 学習 (教師) | 正解集 | 移すときの危険 |
|---|---|---|---|---|---|
| article_summary | s23i | 不要 (fast に追従) | する。約 800 (Opus) | c2a・goldset | 暴走率 (合否線)・rubric 版を同時に上げる |
| triage | s23i | 不要 | する。約 743 (replay、Opus 0 回) | triage_gold_flat_v1 | 平たい基準を退行させない (境界除く 96.2%) |
| severity_axes | s23i | 不要 | する。約 570 (Opus) | B4 層別・i58_severity_eval | detect ML が軸の one-hot を特徴量にする → 差し替え確認 |
| pir_llm_judge | s23i | 不要 | 引き継ぐ 729 (`pir_teacher_v2`) | pir_judge_gold_v2/v3 | 境界除き 100% を保つ |
| pir_daily_focus | s23i | 不要 | する。285 を新しい組み立てで作り直し (Opus) | c3 (SIR 別の要点) | 27.5% の誤り。JSON 包みの再発 |
| event_kind / pair_judge | s23i (pair は SFT に不変) | 不要 | 引き継ぐ 591 / 300 (replay) | eval_event_kind 300 / eval_pair_vs_frozen | 群化 ML の特徴量供給元が変わる |
| synthesis_analysis (ACH) | s23i (reasoning ティアの上書き) | **要 (恒久)**: `step:synthesis_analysis=kuebiko-sft:s24` を残す (消すと REASONING の外部 Sonnet に落ちる) | 引き継ぐ 361 (replay) | ach_daytime_eval | 判定 (leading・確度) の不変 |
| synthesis_detect | s23i | SR のティアを確認し、fast と違えば s24 を指す上書きとして残す | する。残り 105 で続ける (s24_plan §12-2) | detect_goldset | 開設すべき件数 (s21 で 9→18) |
| relevance_jp_cascade | s23i (学習なし、自由形式 bool JSON) | 不要 | **しない**。実物確認。崩れたら logprob 判定へ寄せる (推測) | C2b 日本関連の正解 | shadow 専用 (M4)。影響は小さい |
| thin_body_judge | s23i | 不要 (合格後に旧上書きを外す) | **する**。250 (Opus) | p2_audit 120・p2b_audit 100 | しきい値 0.075 の失効 → 較正集で再計算。P(Yes) 飽和 (§5) |
| pir_focus_claim_judge | s23i | 不要 | **する**。300 (Opus) | s5_claims_gold 300 | しきい値 0.22 の失効。飽和 |
| alert_law_enforcement_judge | s23i | — | **しない** (本番未使用: `alert_demotion.py:78-80` は語の条件のみ。R1b_le_gold は正例 10/185 で 181 件が語の条件由来の理由文 = 独立した正解でない) | — (使う日が来たら独立の Opus 正解、正例 ≥40 を先に) | 2 本化の条件から外す (Opus −250) |
| assign_evidence_judge | **gemma4:26b** | 合格後に上書きを外す | **する**。400 (Opus) | assign_probe 662 組 (f2)・`data/mlx/assign_goldset.jsonl` 662 | s23i は AUC 0.537 で無情報だった → 合格線は AUC 0.938 (26B) 以上。ML の再学習 |
| org_identity_judge | **gemma4:26b** | 合格後に上書きを外す | **する**。名前の組 250 (Opus) = 学習例 500 (2 問: event_logit・org_logit) | org_identity_labels_opus 195 | s23i は常に Yes。確認用 98 組で誤同一視 0/62 を保つ |
| triage_geo_rescue | **kuebiko-sft:26b** | 廃止なら上書きごと消す。残すなら 3 本目のモデルが要る | **段ごと廃止を第一案**。まず今すぐ廃止判定を測る (s24_plan §4.3、Opus 0 回から) | triage_rejections 由来 150 (Opus) | M4 (INGEST_RULE_V2・TRIAGE_FLAT) は **10-12 から本番 on** (compose 既定 1)。旧記載「既定 off・M4 が前提」は誤り |
| **dedup_cluster_judge** | s23i (`Step.DEDUP_CLUSTER_JUDGE`、fast。c7aebefa 以前は `Step.TRIAGE` を借用し `[task: triage]` の印が付いていた) | 不要 (fast に追従) | **しない**。借用は解消済みで印なし。切替前に実物と解析失敗率を見る | dedup 判定の実物 (解析失敗率) | 失敗は黙って取り込む側。s24 切替で挙動が変わる前に確認 |

### 3.2 n20 へ (narrative ティア + 自由文)

| step | 今 | 上書きが要るか | 学習 (教師) | 正解集 | 移すときの危険 |
|---|---|---|---|---|---|
| event_news | n19 (=n19b) | n19 を指す上書きは消せる (narrative ティアが n20 になる) | する。監査・書き直し済み 663 行を引き継ぎ (再監査 50) + 線つき | 要点照合 v3・凍結 38 窓 (eval_n19b.sh は 39 件) | 線の受け皿 (出典番号の帰属) が本番に先に要る |
| pir_spotlight | n19 | 同上 | する。線つき (G1〜G3 の形)。直した教師 152 を引き継ぎ | 凍結 15 窓 + n17c 対 n19b の 30 窓 | 同じアクター ≠ 同一キャンペーン (格上げ) |
| synthesis_narrative | n19 | 同上 | する。直した教師 118 を引き継ぎ (Opus ほぼ不要)。**状況総括は使われている** (SYNTHESIS_GROUNDED=1・RENDER_NARRATIVE 既定 1、朝夕ブリーフ・定時・毎時の 3 経路。`llm_model` 列は分析役の s23i を記録するので使用実態には使えない) | 凍結 15 窓の対読 | — |
| digest_deep_dive_select / digest_deep_dive | n19 (**学習なし**、TP の `UNTRAINED_STEPS`) | **要 (恒久)**: SR 上 FAST のため `step:digest_deep_dive=kuebiko-sft:n20`・`step:digest_deep_dive_select=kuebiko-sft:n20` を残す (または STEP_REGISTRY のティアをコードで変える PR を別に) | **新規**。Opus で作る。選定は「判定」なので s24 に置く案と比べる (n20_plan §7.2) | deep_dive_goldset・frozen_deep_dive (3 窓 → 20 窓以上に増やす) | 初めて学習する課題 |
| article_translate | kuebiko-sft:26b (=N1) | **要 (恒久)**: `step:article_translate=kuebiko-sft:n20` | **新規**。n20b のみ (翻訳・和訳 ≤1,200 例、露出 1 回)。n20_plan §3 | 150 件を translate_body_resumable で両腕訳し直し | JSON への崩れ・識別子の化け・原文の写し |
| actor_sync (MITRE 和訳) | kuebiko-sft:26b | **要 (恒久)**: `step:actor_sync=kuebiko-sft:n20` | 新規。約 200 (翻訳と同じ手順) | 目視 + 関門 | 週次・少量 |

### 3.3 2 系統の外

| step | 今 | 扱い |
|---|---|---|
| pir_compile / selector_proposal / precise_search / assistant_chat (dialog) | 外部 `claudecode:haiku` | **外部のまま** (利用者の明示割当、CLAUDE.md §4)。ローカル fallback (`fallback:dialog`) は未設定で BUILTIN `gemma4:26b` になる。退避先は **4 step の実物を s24 と n20 の両方で 1 回ずつ見て決める** (`pir_compile`・`selector_proposal`・chat の plan は構造化出力なので「自由文が主」は誤り)。**暫定は s24** (推奨で決定 10-15)。BUILTIN は変えない (CLAUDE.md §2 禁止事項) |
| reasoning ティア (`claudecode:claude-sonnet-5`) | 実質未使用 | 上書きなしの step が無い。`fallback:reasoning` は s24 にする。`synthesis_analysis` は上書き (§3.1) で s24 に固定 |
| BUILTIN (fast `gemma4:26b`・narrative `gemma4:31b`) | DB 障害時の fail-safe | 残す。DB 障害時は `raise_if_infrastructure` で止まるため実質使われない (推測)。コードは書き換えない |
| 外部 LLM 割当の step | **なし** (ローカル step はすべてローカル) | 別記: 外部は dialog 4 step のみ。評価の裁定役 Opus は step ではない |

## 4. 移行の順序

s24 を先にする理由: (a) 計画が最も進んでいる (`s24_plan.md`)、(b) 判定 4 本 + 地政学の救済 + 要約・軸の 15 step が乗る、(c) n20 は線の受け皿を本番に実装する前提があり (n20_plan §2)、教師の準備に時間がかかる。
翻訳の失敗 (1,310 件) は学習を待たず、**原因の分類 (GPU 不要) を今すぐ**行う (n20_plan §3.2)。

| 段 | 内容 | 本番の LLM 構成 (暫定) |
|---|---|---|
| P0 (今〜) | 割当表の照合 (MT の step 上書きと本表)・翻訳失敗の分類・較正集と教師づくり (Opus)・**地政学の救済の廃止判定を今すぐ測る (Opus 0 回)**・probe 周辺のコード (§5) | 4 本のまま |
| P1 | s24 を学習 → 評価 → 切替。**logprob 4 本は 1 本ずつ合格したものから** s24 に寄せる。不合格の判定は旧モデルのまま | s24 + n19 + kuebiko-sft:26b (+ 不合格の判定がある間だけ gemma4:26b) |
| P2 | n20a・n20b を同じ夜に学習 → 評価 → 切替。翻訳・和訳・深掘りは合格したものから | s24 + n20 (+ 不合格の step がある間だけ旧モデル) |
| P3 | 2 本になる条件 (§5) が全 step で成立 → 旧モデルを `ollama stop`・**旧モデルを指す step 上書きだけ**を削除 | s24 + n20 |

- **P3 の上書きの扱い (旧版の「step 上書きを削除」は誤り)**: 消すのは旧モデル (`s23i`・`n19`・`kuebiko-sft:26b`・`gemma4:26b`) を指す上書きだけ。**残す**: `step:digest_deep_dive` / `step:digest_deep_dive_select` / `step:article_translate` / `step:actor_sync` = `kuebiko-sft:n20` (SR 上 FAST なので消すと s24 に落ちて崩れる)、`step:synthesis_analysis` = s24 (消すと外部 Sonnet に落ちる)。
- **暫定の残し方**: 不合格の step は **step 上書きを旧モデルのまま**にする (MT の `step:` キー)。P1 の間 `triage_geo_rescue` は `kuebiko-sft:26b` のまま (毎時 5 分しか載らない)。M4 は既に on なので、廃止判定が通れば廃止。
- **P1 と P2 の順序入れ替え**: n20 の教師が先に揃えば逆順でもよい。両方同時に切り替えない (退行の切り分けのため、1 夜 1 本)。n20a・n20b は同じ夜の学習で、切替は合格した方 1 本。
- 切替のたびの手順 (共通): 全 step の実物の出力確認 (10-02 の教訓)・MT の全 step 上書きの照合・`PREFIX_TRAINED_MODELS`・`EVIDENCE_TRAINED_MODELS`・`UNTRAINED_STEPS` の更新・下流の shadow 比較 (s24_plan §9)。

## 5. 「2 本だけ」になる条件 (全 step の合格線)

次を**全部**満たしたときだけ P3 に進む。1 つでも欠けたら、その step だけ旧モデルを残す。

| 対象 | 条件 | 出典となる測定 |
|---|---|---|
| 要約・軸・triage・kind・pair・PIR・ACH・detect | s24_plan §7.2 の合否線 (暴走率・s23i 比で退行なし) | s24_plan §7.2 |
| logprob 4 本 (共通) | 各 AUC ≥ 現行モデルの値 − 0.02 (AUC は logit で)、確信帯の正解率 ≥ 現行、較正集で決めたしきい値で現行の動作点を割らない。**加えて飽和検査**: (i) Yes と No の**両方**が top_logprobs に入る率 ≥ 95% (s23i は assign で same 0.999 / unrelated 0.974、Yes/No が出ず欠けた組 11、`f2_report.md`) (ii) logit が ±15 に張り付く割合と同点数が学習前以下 (org の現行しきい値は logit ≥12・上限 15 の際) (iii) 較正集の ECE。張り付いたら境界例を軟ラベルで入れる版 or 例数を半分にした版を作る。AUC の区間が −0.02 をまたぐ判定は較正集を足して測り直す | 4 本の正解集 + 較正集 |
| 判定ごとの基線 (下の表) | 判定ごとに現行モデル・指標・必要な正例数 | 下表 |
| triage_geo_rescue | 廃止判定 (s24_plan §4.3) が通る。通らなければ s24 に地政学の難例を学習して再測定 | triage_rejections 由来 150 |
| 翻訳 | **主指標** = 境界を除く重い問題 (現行 18/105 = 17.1%)、**副指標** = 境界込み (46/150 = 30.7%)。**合格は両方とも現行以下** (同じ夜に両腕を訳し直して測る。目標 ≤5% は別)。識別子の化け ≤ 2.1%・JSON 混入 0・原文の写し < 1% (**記事 id 単位**、本番 7 日の率で数える) | 両腕訳し直し 150 件 (n20_plan §3.4) |
| 和訳 (actor_sync) | JSON 混入 0・`broken_translation_reason` 通過 100% | 目視 50 件 |
| event_news / spotlight / synthesis | **n20 対 n19 の同じ集合での直接対読**で勝ち ≥ 負け (符号検定) + 線の格上げ率 0。線つきは合否に含めない (線つきの教師は入れ、評価は参考値で記録、推奨で決定 10-15)。n19 の基線 (事象 16:2・Spotlight 22:0・状況総括 14:0) は参考 | 要点照合 v3・Opus 盲検 |
| 深掘り | 学習なし n19 より盲検で勝つ (初の学習)。選定は s24 に置く案と比べる | deep_dive_goldset (20 窓以上) |
| 運用 | 7 日連続で `translate_fidelity_garbled`・`broken_translation`・暴走が基線以下。timeout 超過 0。翻訳が毎時の事象ニュース (600 秒) と `NUM_PARALLEL=1` で順番待ちになる件は処理量を実測 | 本番ログ |
| メモリ | 2 本同時ロードで OOM・swap 0 (`ollama ps` と Activity Monitor) | 実測 |

**logprob 判定ごとの基線と必要な正例数** (LE は除外 — s24_plan §4.2):

| 判定 | 現行モデル | 指標 (合格線の動作点) | 必要な正例数 |
|---|---|---|---|
| thin_body_judge | s23i | s23i の本番値 (しきい値 0.075 での適合率・回収。gemma4:26b AUC 0.994・i58 0.982) を較正集で再測定して記す | 回収の 95% 区間の半幅 ≤ 0.10 に正例 ≥ 50 (p=0.85 で 49) |
| pir_focus_claim_judge | s23i | s23i の本番値 (しきい値 0.22。`s5_report.md`) を同様に | 同上 ≥ 50 |
| assign_evidence_judge | gemma4:26b | ML 込みの適合 85.1%・回収 79% (本番相当 260 組、f2)・AUC 0.938 以上 | 同上 ≥ 50 (半幅 ≤0.05 なら ≥ 200) |
| org_identity_judge | gemma4:26b | 誤同一視 0 (確認用 98 組で 0/62) と回収 ≥ 85% (現行 89%)。**2 問**はそれぞれ較正 (event_logit 12・org_logit −4 が現行) | 同上 ≥ 50 |

- 正解集ごとの正例数は未集計 → 較正集を作る時に数えて上表に書き足す。足りない判定は正例を足す (必要なら Opus)。

**判定が参照するコードの前提** (P0 で直す。テストとセット):
- **候補モデルを本番と同じ経路で測る**: `decision_probe._JSON_BIASED_MODELS = PREFIX_TRAINED_MODELS` は固定集合で `SFT_TASK_PREFIX_MODELS` の追加分を含まない (一時タグは chat 経路で測られる)。`calibrate_probes.py` は `resolve_step_model` で決めるため切替前の候補を測れない → **`calibrate_probes` はモデルを引数で受け、`_is_json_biased` も env の追加分を読む**。生の `top_logprobs` を 1 件見て経路一致を確認する。
- **印は `with_task_prefix(…, step, model)` で付ける** (`_json_biased_prompt` に直接足すと s23i の判定も変わる。`UNTRAINED_STEPS` の除外は `with_task_prefix` にしかない)。**s23i・i58 の `UNTRAINED_STEPS` に判定 4 step を足す**。印・Yes/No 化は s23i のしきい値を変えるので、**切替前に s23i の較正を取り直す**。
- **補間 `s24i` (s24 × s23i) は判定を薄める**: t の採用条件に logprob の合格線 (飽和検査を含む) を足す。s24i は較正と `PREFIX_TRAINED_MODELS` 登録を別に行う。

## 6. 戻し方

- 戻しの単位は **MT の版 + step 上書き + 付随フラグ**。切替前に MT を保存 (戻し先は版 40)。
- 旧モデルは P3 の後も **ディスクに残す** (`ollama rm` しない。`data/cloud-runs/s22|s23`・s23i・n19・`kuebiko-sft:26b` は消さない、s24_plan §10-8)。
- 付随: `AXES_FROM_BODY`・detect ML (`data/model_backups/*.bak`)・logprob のしきい値ファイル・`summarizer_rubric` の版・assign ML は **組で戻す** (片方だけ戻すと分布が合わない、s24_plan §9)。
- **判定 4 本の戻し先は版 40 の割当**: thin・claim = `kuebiko-sft:s23i`、assign・org = `gemma4:26b` (旧版の「gemma4:26b に戻す」は thin・claim について誤り)。
- **fail-closed の落とし穴**: 解決モデルの項が `probe_thresholds.json` に無いと判定が `None` を返して黙って止まる。**先に `config/models/probe_thresholds.json` へ s23i と gemma4:26b の項を入れておく** (戻し先の項がないまま切り替えない)。形は `{model: {step: {question: {threshold, auc, ece, n, calib_sha256, date}}}}` (org は 2 問それぞれ、他は question = `main`)。

## 7. 件数・Opus 回数・追加コストの表 (3 本の SSoT)

計算は `scratchpad` のスクリプトで確かめた (合計は下表の行の和)。Opus 回数は推測。LE の教師 −250、n20 の監査済み −400 を差し引いた値。

| 項目 | 学習例数 (train) | Opus 回数 | 備考 |
|---|---|---|---|
| s24 教師 要約 | 800 | 500 | |
| s24 教師 軸 | 570 | 470 | 付け直し 319 + 難例 150 |
| s24 教師 triage | 743 | 0 | replay |
| s24 教師 SIR 別の要点 | 285 | 285 | 全作り直し |
| s24 教師 detect | 105 | 0〜100 (推奨案 0) | |
| s24 引き継ぎ (PIR 729・ACH 361・kind 591・pair 300・知識 QA 200) | 2,181 | 0 | replay |
| **s24 本体 小計** | **4,684** | **1,255 (〜1,355)** | 旧本文の「約 4,900」は誤り |
| s24 判定 thin / claim / assign / org | 250 / 300 / 400 / 500 (org は名前の組 250 × 2 問) | 250 / 300 / 400 / 250 | LE を除外 (旧 250 を引く) |
| **s24 判定 小計** | **1,450** | **1,200** | |
| s24 較正集 4 本 × 100 | — | 400 | 旧 500 (5 本) から −100 |
| **s24 学習総計** | **6,134** (要約の比率 800/6,134 = **13.0%**) | **教師 2,855 (〜2,955)** | 旧本文「16% 前後」は誤り → 13% に揃える |
| s24 評価 | — | 800 | 推測 |
| 地政学の救済の廃止判定 | — | 0 (今すぐ: run_logs の取り込みヒント照合) → 必要なら 300 (150 × 2 回) | |
| n20 教師 事象 | 600 (×3 露出 = 1,800) | 50 | 監査・書き直し済み 663 行を引き継ぎ、再監査 50 |
| n20 教師 Spotlight | 200 窓 (×3 = 600) | 200 | |
| n20 教師 状況総括 | 110 (×3 = 330) | 0 | 直した教師 118 を引き継ぎ |
| n20 教師 深掘り 選定 + 本文 | 150 + 150 = 300 (×3 = 900) | 150 + 150 = 300 | |
| **n20 本体 小計** | **3,630** (露出込み) | **550** | |
| n20 翻訳・和訳 教師 | n20a: 0 / n20b: ≤1,200 例 (露出 1 回)。収集は 1,400 (Sonnet、関門で捨てる余裕) | 監査 150 (Opus) | 翻訳比率: n20b = 1,200/4,830 = 24.8% (≤25%)。2 腕の合算案は 28〜44% で目標超 |
| **n20 教師 総計** | n20a 3,630 / n20b 4,830 | **700** | |
| n20 評価 | — | 816 | 直接対読 (事象 39 + Spotlight 30 + 総括 15 + 深掘り 20 = 104 窓 × 順序入替 2 × 2 腕 = 416) + 翻訳 両腕 150 × 2 = 300 + 深掘り正解づくり 100 (推測) |
| n20 追加の GPU | n20a・n20b を同じ夜に学習 | — | 追加 約 $30 (n20 約 $30 → 計 約 $60、推測) |
| **全体** | | **教師 3,555 (〜3,655)・評価 1,616 (〜1,916 地政学込み)** | 旧 two_family §7 の「s24 3,300〜3,500・n20 1,000〜1,500」は置き換え |

- 旧見積りとの差: LE −250、較正 −100、要約系 1,300〜1,500 → 1,255〜1,355 (内訳の和)、n20 の監査済み −400 + 再監査 +50。
- **教師の週と評価の週を分ける**。Opus の枠: 教師 3,555 回は s24 (2,855) と n20 (700) を別の週に。

## 8. メモリと運用

- 目標 54 GB (s24 + n20、int8) は、現行 4 本の和 87 GB (27+27+16+17) より小さい。KV 込みの実測は s23i 36 GB なので、2 本同時で 約 72 GB (推測)。
- `OLLAMA_NUM_PARALLEL=1` のまま (大記事で利得ゼロ、CLAUDE.md §7)。2 本が同時に動く時間帯 (毎時の段と朝夕ブリーフ) は GPU を取り合う → **P3 前に timeout 超過を 7 日見る** (§5 の運用)。翻訳を n20 に寄せると毎時の事象ニュース (600 秒) と順番待ちになる → 運用の段で翻訳の処理量を実測。
- 評価・学習は本番コンテナの外 (CLAUDE.md §7)。

## 9. 決定と未決

### 9.1 推奨で決定 (10-15)

| # | 論点 | 決定 |
|---|---|---|
| 1 | 翻訳を n20 に入れるか | **入れる (推奨で決定 10-15)**。ただし **2 腕**: 同じ夜に n20a (翻訳・和訳なし) と n20b (あり、≤1,200 例・25% 以下・露出 1 回) を学習し、事象・Spotlight・総括の差で干渉を帰属 (+約 $30)。**撤退時は公式 `gemma4:26b`** (化け 72.5→50%)。**撤退すると 3 本目のモデルが残る** (2 系統化は崩れる) ことを明記する。外部 LLM にはしない |
| 2 | dialog の退避先 | **推奨で決定 (10-15)**: 4 step (`pir_compile`・`selector_proposal`・`precise_search`・chat) の実物を s24 と n20 の両方で 1 回ずつ見て決める。暫定は s24 (`pir_compile`・`selector_proposal`・chat の plan は構造化出力なので「自由文が主」は誤り) |
| 3 | `triage_geo_rescue` の廃止 | **推奨で決定 (10-15)**: 今すぐ廃止判定を測る (run_logs の救済 77 件 (10-12〜15) のうち取り込みヒントで既に取り込まれていた件数、Opus 0 回) |
| 4 | 翻訳の教師 | **推奨で決定 (10-15)**: Sonnet + Opus 監査 (≥100 件・境界を除く重い問題 ≤5%)。機械の関門を 1 つでも通らない教師は捨てる |
| 5 | 状況総括 | **推奨で決定 (10-15)**: n20 の範囲に残す (直した教師 118 件、Opus ほぼ不要) |
| 6 | 線つきの教師 | **推奨で決定 (10-15)**: 合否に含めない。線つきの教師は入れ、評価は参考値で記録 |

### 9.2 未決

- なし (s24_plan §12 の項目 1・2 は s24 内の判断として残る)。
