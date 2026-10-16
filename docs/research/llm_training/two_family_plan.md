# 2 系統化の計画 — 本番の LLM を s24 (s 系) と n20 (n 系) だけにする (2026-10-15、案、Opus レビュー反映済み)

> 状態: 案。利用者決定 (10-15) 「本番の LLM 処理を s24 と n20 の 2 系統だけで処理できるように整理する」を受けた設計。
> 改訂: 10-15 の Opus レビュー (20 項目・要確認 4 点、`data/eval/two_family_review.md`) を 3 本すべてに反映済み。利用者判断の項目は「推奨で決定 (10-15)」と書く。
> 数字は出典つきの再掲、推測は「推測」と書いた。詳細: s 系 `s24_plan.md`、n 系 `n20_plan.md`。
> 出典の略: MT = 本番 model_tiers 版 40 (10-12、`app_config_versions`)、SR = `src/tools/model_tiers.py` の `STEP_REGISTRY`、
> TP = `src/tools/task_prefix.py`。
> 改訂 (10-15 学習データの質の監査): §7 の表を作り直し・再監査・揺れの分だけ更新 (s24 教師 4,096・n20 教師 1,000)。判定は `s24_plan.md` §13。
> **改訂 (10-16 利用者決定)**: logprob 判定 4 本は s24 で学習せず、モデルごとのしきい値の自動較正だけにする (根拠 `data/eval/F1_logprob_pin_report.md`)。§3.1・§5・§7 を更新 (s24 train 5,732 → 4,282、s24 教師 4,096 → 2,556、全体の教師 5,096 → 3,556)。detect は新しい期間で作り直す (決定 10-16 → **同日撤回、学習しない**。§7・§9.1-8)。
> **改訂 (10-16 N1 の照合の反映、`data/eval/N1_claim_verify_report.md`)**: 本番・学習データと照合した誤り 7・一部 12・不明 4 を直した。主な変更: (1) s24 の triage は「引き継ぐ」でなく**組み立てを直して作り直す** (s23 の 705 行は入力の「概要:」が全行空だった)、(2) detect は s24 で学習しない (`s24_plan.md` §13.7 に表を合わせた)、(3) PIR 判定の「16 本を足す」を撤回 (本番で LLM 判定が動くのは 5 本だけ)、(4) **借りている Step** を §3.1 に行として追加、(5) dedup の借用は「リポジトリでは修正済み・本番は未デプロイ」、(6) `calibrate_probes.py`・`probe_thresholds.json` は新規に作るもの。s24 train 4,282 → 3,804、s24 教師 2,556 → 2,186、全体の教師 3,556 → 3,186。「未確認」と書いた項目は N1 で裏が取れなかったもの。
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

- 実際に載っているのは `ollama ps` で s23i 36 GB (KV 込み・ctx 262144) のみ、unload まで 5 分 (10-15 の観測。**未確認**: 10-16 時点は `ollama ps` が空で再現できない)。
  常駐は「呼ばれたときに載る」であり、4 本が同時に載るとは限らない (推測: 毎時の段が重なると 2〜3 本)。
- 目標: **s24 (27 GB) + n20 (27 GB) = 54 GB** (int8 取込み。int4 は効果が消える、`s24_plan.md` §4)。
  別プロジェクトの Ollama 利用 (約 20 GB、memory `noroshi_ollama_tenant_2026_09_11`) と合わせても 128 GB に収まる (推測、KV 込みで s23i 実測 36 GB を 2 本 = 72 GB)。
- **隠れた LLM 呼び出し**: `dedup_cluster_judge` (`.env` `DEDUP_CLUSTER_JUDGE=1`、`orchestrator.py:491` が `Step.TRIAGE` を借りて `dedup_judge.is_duplicate` を呼ぶ)。借用のため `TaskPrefixClient` 経由で `[task: triage]` の印が付く = s18 の課題取り違えと同型。失敗は黙って取り込む側に倒れる。**`Step.DEDUP_CLUSTER_JUDGE` の新設 (c7aebefa、STEP_REGISTRY に fast・300 秒、印なし) はリポジトリでは修正済みだが、本番は未デプロイ** (N1 #13: コンテナは 10-15 16:16 のビルドで c7aebefa を含まず、コンテナ内は今も `Step.TRIAGE` を借りて `[task: triage]` が付く。`DEDUP_CLUSTER_JUDGE=1` で稼働中)。デプロイで解消する。割当表 (§3.1) に行を足した。他にも借りている Step が 3 組ある (§3.1 の太字の行、N1 §C)。

## 2. 振り分けの原則

- **s 系 = 構造化出力・判定・抽出 (JSON か 1 トークン)**。n 系 = **自由な長文** (散文・翻訳・和訳)。
- 同じ LoRA に「JSON で答える癖」と「自由文」を混ぜると自由文が JSON に崩れる (10-02: 翻訳 637 件・PIR 別の要点が毎日全件、n19 も翻訳 5 件中 4 件、memory `untrained_freetext_steps_broke_2026_10_02`)。
  この原則は s 系に自由文を入れない根拠であり、**n 系に翻訳を入れる側のリスクでもある** (n20_plan §3 で対策: 翻訳なし n20a と翻訳あり n20b の 2 腕で干渉を切り分ける)。
- 判定 4 本 (logprob: thin・claim・assign・org) は s 系が担うが、**学習はしない** (決定 10-16)。出力が 1 トークンで JSON の癖と同じ形式 (`{"answer": "` の直後) の raw 経路で測る。AUC は s23i と gemma4:26b でほぼ同等で、差はしきい値の位置だけ (`F1_logprob_pin_report.md`)。モデルを替えるたびに `calibrate_probes.py` でしきい値を取り直す (§5)。

## 3. 全 step の割当表

SR の全 29 step (c7aebefa で追加された `dedup_cluster_judge` を含む) のうち埋込 `embed` (LLM でない) を除く **28 step: s24 へ 17・n20 へ 7・外部 4**。借りている呼び出し (§3.1 の 3 行) を専用 Step に分けるなら +3 (いずれも fast = s24 で 31 step)。
(旧版の「27 step: s24 へ 16」は `dedup_cluster_judge` の行を数えていなかった。)

「学習」の件数は案 (SSoT は §7)。「正解集」は評価専用 (学習に入れない)。
**「上書きが要るか」列**: P3 で step 上書きを全部消すと、`digest_deep_dive`・`digest_deep_dive_select`・`article_translate`・`actor_sync` (SR 上は FAST) が s24 に、`synthesis_analysis` (REASONING) が外部 Sonnet に落ちる。**消すのは旧モデルを指す上書きだけ**。恒久に残す上書きは列に明記する。

### 3.1 s24 へ (fast ティア)

| step | 今 | 上書きが要るか | 学習 (教師) | 正解集 | 移すときの危険 |
|---|---|---|---|---|---|
| article_summary | s23i | 不要 (fast に追従) | する。約 800 (Opus。書き直し 139 は作り直し) | c2a・goldset | 暴走率 (合否線)・rubric 版を同時に上げる |
| triage | s23i | 不要 | する。567 (743 の判定から境界 176 を外す。Opus は理由の書き直し 111)。**「引き継ぐ」でなく組み立てを直して作り直す**: s23 の triage 705 行は全行「概要:」が空だった (教師ファイル `triage_flat_teacher_v1.jsonl` に summary 欄が無いのに `build_dataset_s23.py:164`・`build_dataset_s24.py` が `row.get("summary")` を読んだ。N1 #79)。`build_dataset_s24.py` を、Opus が見たのと同じ入力 (`triage_flat_teacher_items.json`、本番 `_triage_content` と同じ本文の冒頭 最大 1,500 字) を key で入れる形に直した。Opus の判定は概要つきで採点されており**教師の作り直しは不要**。品質関門に「入力の欄が空でない」(check 12) を追加 | triage_gold_flat_v1 | 平たい基準を退行させない (境界除く 96.2%)。⚠ 96.2% は概要つきの本番入力での s23i の値で、**s23 の triage 学習は見出しだけの入力だった** → s24 で入力が変わるので退行を実測する |
| severity_axes | s23i | 不要 | する。570 (Opus、419 全件付け直し) | B4 層別・i58_severity_eval | detect ML が軸の one-hot を特徴量にする → 差し替え確認 |
| pir_llm_judge | s23i | 不要 | 直して使う: 境界を外して約 503 (足さない、Opus 0)。本番で LLM 主題判定が動く PIR は `llm_judge.enabled` の 5 本だけ (`llm_judge.py:142`) で、教師の 5 本と完全に一致する (N1 #58。旧「16 本を約 190 例足す」は撤回) | pir_judge_gold_v2/v3 | 境界除き 100% を保つ |
| pir_daily_focus | s23i | 不要 | する。285 を新しい組み立てで作り直し (Opus) | c3 (SIR 別の要点) | 27.5% の誤り。JSON 包みの再発 |
| event_kind / pair_judge | s23i (pair は SFT に不変) | 不要 | 引き継ぐ 446 (breach を間引く) / 132 (pair 300 のうち 168 行が「(要約なし)」を含む。本番 30 日の posted 5,638 件で要約が空は 0 件 = 分布が違うので除く。N1 #83) | eval_event_kind 300 / eval_pair_vs_frozen | 群化 ML の特徴量供給元が変わる |
| synthesis_analysis (ACH) | s23i (reasoning ティアの上書き) | **要 (恒久)**: `step:synthesis_analysis=kuebiko-sft:s24` を残す (消すと REASONING の外部 Sonnet に落ちる) | 引き継ぐ 301 (複製 59・重なり 1 を除く)。361 行は**すべて `ground_incremental.j2` (増分更新)**。同じ `[task: ach]` が付く `ground_ach`・`adversarial`・`claim_rewrite` は教師 0 (下の借用の行)。posture 54 行 (hypotheses_override) の入力は再現できず**未確認** | ach_daytime_eval | 判定 (leading・確度) の不変 |
| synthesis_detect | s23i | s24 に追従するが、**detect は s24 で学習しない** (`s24_plan.md` §13.7、決定 10-16)。切替の評価で detect_goldset を s23i と比べ、不合格なら `step:synthesis_detect` = s23i の上書きを残す (2 本化の例外) | **しない**。255 行 (すべて `detect_new.j2`) も引き継がない。旧「新しい窓で作り直し 120」は撤回 | detect_goldset | 開設すべき件数 (s21 で 9→18)。⚠ 本番の detect の LLM は gemma4:26b でなく s23i (M1 の記述の誤り、N1 #76) |
| relevance_jp_cascade | s23i (学習なし、自由形式 bool JSON) | 不要 | **しない**。実物確認。崩れたら logprob 判定へ寄せる (推測) | C2b 日本関連の正解 | shadow 専用 (M4)。影響は小さい |
| thin_body_judge | s23i | 不要 | **しない (決定 10-16)**。較正のみ。較正集は p2_audit 120・p2b_audit 100 (正例 ≥50 に足す、Opus 上限 100) | p2_audit 120・p2b_audit 100 | しきい値 0.075 は s24 で失効 → 切替時に較正 (§5)。AUC は s23i 0.986・gemma4:26b 0.994 (p2b) で同等 |
| pir_focus_claim_judge | s23i | 不要 | **しない (決定 10-16)**。較正のみ。較正集は s5_claims_gold 300 に足す (裏付けなし 21 → ≥50、Opus 200) | s5_claims_gold 300 | しきい値 0.22 は s24 で失効 → 切替時に較正。AUC は s23i 0.976・gemma4:26b 0.941 |
| alert_law_enforcement_judge | s23i | — | **しない** (本番未使用: `alert_demotion.py:78-80` は語の条件のみ。⚠ ただしその**語の条件の関門自体は本番 on** (`ALERT_EXCLUDE_LAW_ENFORCEMENT=1`、compose 既定 1)。R1b_le_gold は正例 10/185 で 181 件が語の条件由来の理由文 = 独立した正解でない) | — (使う日が来たら独立の Opus 正解、正例 ≥40 を先に) | 2 本化の条件から外す (Opus −250) |
| assign_evidence_judge | **gemma4:26b** | s23i へ移すとき `step:assign_evidence_judge` を s23i に変え、s24 で s24 へ。較正して合格後に上書きを外す | **しない (決定 10-16)**。較正のみ (陽性 109 で足りる、Opus 0) | assign_probe 662 組 (f2)・`data/mlx/assign_goldset.jsonl` 662 | 較正し直してから切替。AUC は s23i 0.956・gemma4:26b 0.970。合格線 = 較正後の適合・回収が gemma4:26b の本番値以上 (ML 込み適合 85.1%・回収 79%)。assign ML の再学習の要否は KS で見る |
| org_identity_judge | **gemma4:26b** | 同上 (`step:org_identity_judge`) | **しない (決定 10-16)**。較正のみ (陽性 59 で足りる、Opus 0)。2 問をそれぞれ較正 | org_identity_labels_opus 195 | 本番は `EVENTNEWS_ORG_IDENTITY=shadow` (記録のみで群化に効いていない。N1 #21)。s23i は既定の境界 0.0 で回収 0。AUC は s23i 0.947・gemma4:26b 0.948。合格線 = 誤同一視 0 (確認用 98 組 0/62) と回収 ≥ 89% (gemma4:26b の本番値) |
| triage_geo_rescue | **kuebiko-sft:26b** | 廃止なら上書きごと消す。残すなら 3 本目のモデルが要る | **段ごと廃止を第一案**。まず今すぐ廃止判定を測る (s24_plan §4.3、Opus 0 回から) | triage_rejections 由来 150 (Opus) | M4 (INGEST_RULE_V2・TRIAGE_FLAT) は **10-12 から本番 on** (compose 既定 1)。旧記載「既定 off・M4 が前提」は誤り |
| **dedup_cluster_judge** | s23i (**本番はまだ `Step.TRIAGE` を借用 = `[task: triage]` の印が付く**。リポジトリは c7aebefa で `Step.DEDUP_CLUSTER_JUDGE` (fast) に修正済み・未デプロイ) | 不要 (fast に追従) | **しない**。デプロイすれば借用は解消 (印なし)。デプロイ前の s24 切替は不可 (借用の印が s24 の triage 課題に当たる)。切替前に実物と解析失敗率を見る | dedup 判定の実物 (解析失敗率) | 失敗は黙って取り込む側。s24 切替で挙動が変わる前に確認 |
| **ioc_llm_verifier** (借用: `Step.ARTICLE_SUMMARY`、`briefing.py:296`。Grok 経路・`body_refetch_backlog` も同じ client) | s23i + `[task: summary]` の印 (本番で動く。48h で `ioc_dropped_by_llm` 13) | **専用 Step を新設して印を外す** (fast、印なし)。上書き不要 | **しない** (教師 0。KEEP/DROP の短い判定)。推奨 = 新設して印を外し、実物と解析失敗率を見る。崩れるなら logprob 判定へ寄せる (推測) | IOC 検証の実物 | summary 課題の形で答えようとして崩れる。失敗時の倒れ方 (候補を残すか落とすか) を先に確認 |
| **detect の ML 和集合・束ねの書き直し** (借用: `Step.SYNTHESIS_DETECT`、`stateful.py:2312`・`2259`、テンプレ `detect_ml_select.j2`) | s23i + `[task: detect]` (7 日で和集合 16・分割 10) | **専用 Step を新設して印を外す** (fast)。detect は s24 で学習しない (§13.7) ので印を付ける理由が無い | **しない** (教師 0。detect の教師は基準未達で作れない)。detect_new.j2 とは別課題 (claim だけ書く) | detect_goldset | 印の付く未学習の課題 |
| **ground_ach・adversarial・claim_rewrite** (借用: `Step.SYNTHESIS_ANALYSIS`、`stateful.py:1556,1711,1790,1986`・`claim_guard.py:123`) | s23i + `[task: ach]` (`step:synthesis_analysis` の上書きに従う) | **専用 Step を新設して印を外す** (fast)。`step:synthesis_analysis` の恒久上書きは ground_incremental だけに効く | s24 の ACH 301 は ground_incremental だけ。推奨 = まず印を外し、本番 7 日の実物と関門 (`claim_guard`・coherence) の失敗率を測る。崩れが有意なら ground_ach・adversarial に教師を作る (Opus、件数は実物確認後に見積もる。**未算入**)。claim_rewrite は学習しない | ach_daytime_eval | 判定 (leading・確度) の不変。同じ step 内の別課題 |

**借りている Step の扱い (N1 §C、推奨)**: 目的の違う問いに学習済みの課題の印が付くのが害 (s18 の課題取り違えと同型)。3 組とも教師 0 で、**学習より先に専用 Step を新設して印を外す**のが最小の手当て (コード変更は別 PR、テストとセット)。教師を作るのは、印を外した後の実物で崩れが見えた課題だけ。dedup の借用 (デプロイで解消) と `CYBER_POLICY_RESCUE` (`dispatch.py:158-163` が `TRIAGE_GEO_RESCUE` の client を借りる。本番 off) も同じ型。借りていないことを確認済みなのは pair・kind (roundup_guard・detect 前段も同じ client)・軸 (SEVERITY_AXES)・深掘り選定/本文・render・PIR focus の再生成・triage_shadow・logprob 4 本。

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

s24 を先にする理由: (a) 計画が最も進んでいる (`s24_plan.md`)、(b) 判定 4 本 (学習せず較正) + 地政学の救済 + 要約・軸の 15 step が乗る、(c) n20 は線の受け皿を本番に実装する前提があり (n20_plan §2)、教師の準備に時間がかかる。
翻訳の失敗 (1,310 件 — 出典は A2 報告の 1 行のみで元ログは消失、再試行込みの件数。今のコンテナの分は 321 件 = 56 記事、N1 #65) は学習を待たず、**原因の分類 (GPU 不要) を今すぐ**行う (n20_plan §3.2)。

| 段 | 内容 | 本番の LLM 構成 (暫定) |
|---|---|---|
| P0 (今〜) | 割当表の照合 (MT の step 上書きと本表)・翻訳失敗の分類・**較正集づくり (Opus 300 回: claim 200・thin 上限 100)** と `calibrate_probes.py` (新規作成)・**assign・org を s23i で較正し直して移す**・教師づくり (Opus)・**地政学の救済の廃止判定を今すぐ測る (Opus 0 回)**・probe 周辺のコード (§5) | 4 本のまま (assign・org が s23i に移れば 3 本) |
| P1 | s24 を学習 → 評価 → 切替。**logprob 4 本は s24 でも学習せず、`calibrate_probes.py` で較正して 1 本ずつ合格したものから** s24 に寄せる。不合格の判定は旧モデルのまま | s24 + n19 + kuebiko-sft:26b (+ 不合格の判定がある間だけ s23i) |
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
| 要約・軸・triage・kind・pair・PIR・ACH・detect | s24_plan §7.2 の合否線 (暴走率・s23i 比で退行なし)。detect は学習しないので「学習なしの s24 が detect_goldset で s23i 以上か」(s24_plan §13.7) | s24_plan §7.2 |
| logprob 4 本 (共通、学習しない) | `calibrate_probes.py` で移行先モデルのしきい値を較正したうえで、各 AUC ≥ 現行モデルの値 − 0.02 (AUC は logit で)、確信帯の正解率 ≥ 現行、**較正後の精度・回収が現行の本番値以上** (thin・claim = s23i の本番値、assign・org = gemma4:26b の本番値。**gemma4:26b を残さないための条件**)、Yes と No の両方が top_logprobs に入る率 ≥ 95%。AUC の区間が −0.02 をまたぐ判定は較正集を足して測り直す。1 本でも満たさない判定は、その判定だけ旧モデルを残す | 4 本の較正集 (下表) |
| 判定ごとの基線 (下の表) | 判定ごとに現行モデル・指標・必要な正例数 | 下表 |
| triage_geo_rescue | 廃止判定 (s24_plan §4.3) が通る。通らなければ s24 に地政学の難例を学習して再測定 | triage_rejections 由来 150 |
| 翻訳 | **主指標** = 境界を除く重い問題 (現行 18/105 = 17.1%)、**副指標** = 境界込み (46/150 = 30.7%)。**合格は両方とも現行以下** (同じ夜に両腕を訳し直して測る。目標 ≤5% は別)。識別子の化け ≤ 2.1%・JSON 混入 0・原文の写し < 1% (**記事 id 単位**、本番 7 日の率で数える) | 両腕訳し直し 150 件 (n20_plan §3.4) |
| 和訳 (actor_sync) | JSON 混入 0・`broken_translation_reason` 通過 100% | 目視 50 件 |
| event_news / spotlight / synthesis | **n20 対 n19 の同じ集合での直接対読**で勝ち ≥ 負け (符号検定) + 線の格上げ率 0。線つきは合否に含めない (線つきの教師は入れ、評価は参考値で記録、推奨で決定 10-15)。n19 の基線 (事象 16:2・Spotlight 22:0・状況総括 14:0) は参考 | 要点照合 v3・Opus 盲検 |
| 深掘り | 学習なし n19 より盲検で勝つ (初の学習)。選定は s24 に置く案と比べる | deep_dive_goldset (20 窓以上) |
| 運用 | 7 日連続で `translate_fidelity_garbled`・`broken_translation`・暴走が基線以下。timeout 超過 0。翻訳が毎時の事象ニュース (600 秒) と `NUM_PARALLEL=1` で順番待ちになる件は処理量を実測 | 本番ログ |
| メモリ | 2 本同時ロードで OOM・swap 0 (`ollama ps` と Activity Monitor) | 実測 |

**logprob 判定ごとの基線と較正集の正例数** (LE は除外・摘発は本番未使用のまま — s24_plan §4.2):

| 判定 | 現行モデル | 指標 (合格線の動作点) | 正例数 (目標 ≥ 50、回収の 95% 区間の半幅 ≤ 0.10 に p=0.85 で 49) |
|---|---|---|---|
| thin_body_judge | s23i | s23i の本番値 (T=0.075: 回収 0.519・誤発火 0.000・精度 0.869、p2b。gemma4:26b は 0.926/0.042/0.949) を較正集で再測定して記す | p2b は薄い 27。p2 の薄いを集計し、足りなければ Opus で上限 100 |
| pir_focus_claim_judge | s23i | s23i の本番値 (T=0.22: 回収 0.476・誤発火 0.009・精度 0.908。gemma4:26b は 0.714/0.018/0.938) | 裏付けなし 21 → ≥ 50。濃縮抽出 200 文を Opus (期待正例 30〜60、推測) |
| assign_evidence_judge | gemma4:26b | ML 込みの適合 85.1%・回収 79% (本番相当 260 組、f2)・26B 単体 AUC 0.970 | 陽性 109 (足さない) |
| org_identity_judge | gemma4:26b | 誤同一視 0 (確認用 98 組で 0/62) と回収 ≥ 89%。**2 問**はそれぞれ較正 (event_logit 12・org_logit −4 が現行) | 陽性 59 (足さない) |

- 較正は既存の正解集を流用して**記事 id (組・key) 単位の交差分割**で行い、報告値は分割外 (調整と測定を分ける)。足した較正集は凍結して s24 の guard に入れる。**Opus 回数 = 300** (旧 較正集 400)。


**判定が参照するコードの前提** (P0 で直す。テストとセット):
- **候補モデルを本番と同じ経路で測る**: `decision_probe._JSON_BIASED_MODELS = PREFIX_TRAINED_MODELS` は固定集合で `SFT_TASK_PREFIX_MODELS` の追加分を含まない (一時タグは chat 経路で測られる)。`calibrate_probes.py` と `config/models/probe_thresholds.json` は**まだ存在せず、新規に作るもの** (N1 #38: 旧記載は既存実装の修正と読めた)。**`calibrate_probes` はモデルを引数で受け (`resolve_step_model` に頼らず切替前の候補を測れる)、`_is_json_biased` も env の追加分を読む**。生の `top_logprobs` を 1 件見て経路一致を確認する。
- **判定 4 本は学習しないので、印 (`TASK_MARKERS`) も `UNTRAINED_STEPS` の追加も要らない** (10-15 案の「s23i・i58 の `UNTRAINED_STEPS` に足す」は撤回)。ただし s24 の学習が raw 経路の最初のトークンの分布を動かす可能性は残るため、**切替前に必ず較正を取り直す**。
- **補間 `s24i` (s24 × s23i)** も切替前に較正し、`PREFIX_TRAINED_MODELS` 登録を別に行う。t の採用条件に判定は入れない。
- **切り替え手順への組み込み** (毎回): ①`probe_thresholds.json` に今のモデルと戻し先の項 ②移行先を引数に較正 ③合格線を満たす判定だけ step 上書きを変える ④全 step の実物確認。fail-closed の扱いは §6。


## 6. 戻し方

- 戻しの単位は **MT の版 + step 上書き + 付随フラグ**。切替前に MT を保存 (戻し先は版 40)。
- 旧モデルは P3 の後も **ディスクに残す** (`ollama rm` しない。`data/cloud-runs/s22|s23`・s23i・n19・`kuebiko-sft:26b` は消さない、s24_plan §10-8)。
- 付随: `AXES_FROM_BODY`・detect ML (`data/model_backups/*.bak`)・logprob のしきい値ファイル・`summarizer_rubric` の版・assign ML は **組で戻す** (片方だけ戻すと分布が合わない、s24_plan §9)。
- **判定 4 本の戻し先は版 40 の割当**: thin・claim = `kuebiko-sft:s23i`、assign・org = `gemma4:26b` (旧版の「gemma4:26b に戻す」は thin・claim について誤り)。
- **fail-closed の落とし穴**: 解決モデルの項が `probe_thresholds.json` に無いと判定が `None` を返して黙って止まる。**先に `config/models/probe_thresholds.json` へ s23i と gemma4:26b の項を入れておく** (戻し先の項がないまま切り替えない)。形は `{model: {step: {question: threshold}}}` (org は 2 問それぞれ、他は question = `main`。AUC・ECE・n・較正集の sha256・日付は `data/eval/probe_calib/<model>.report.json` に別に残す)。assign・org を s23i に移す前に、s23i の項を較正して入れる。

## 7. 件数・Opus 回数・追加コストの表 (3 本の SSoT)

計算は `scratchpad` のスクリプトで確かめた (合計は下表の行の和)。Opus 回数は推測。LE の教師 −250、n20 の監査済み −400、**10-16: 判定 4 本の教師 −1,200・較正集 400→300・判定 4 課題分の再監査と揺れ −240** を差し引いた値。

| 項目 | 学習例数 (train) | Opus 回数 | 備考 |
|---|---|---|---|
| s24 教師 要約 | 800 (valid 60 は別) | 560 | 書き直し 139 を作り直す (500 に含む) + valid 60 |
| s24 教師 軸 | 570 | 570 | 419 全件付け直し + 難例 150 + 予備 1。本番の `build_body_prompt` と同じ入力 |
| s24 教師 triage | 567 (743 − 境界 176) | 111 | replay。理由に裁定者向けの語がある 111 を理由の欄だけ書き直す (上限) |
| s24 教師 SIR 別の要点 | 285 | 285 | 全作り直し |
| s24 教師 detect | **0 (学習しない、`s24_plan.md` §13.7、決定 10-16)** | 0 | 旧 120 (新しい窓で作り直し)。L1・L2 で再監査が基準未達。255 行も引き継がない |
| s24 教師 PIR 判定 | 約 503 (境界を外す。16 本は足さない) | 0 | 本番で LLM 判定が動く 5 本と教師の 5 本が一致 (N1 #58)。旧 693・Opus 190 |
| s24 引き継ぎ (ACH 301・kind 446・pair 132・知識 QA 200) | 1,079 | 0 | ACH は複製 59・重なり 1 を除く。kind は breach 51% → 35% 以下に間引く。pair は「(要約なし)」168 行を除く (旧 300) |
| **s24 本体 小計** | **3,804** | **1,526** | 800 + 570 + 567 + 285 + 503 + 301 + 446 + 132 + 200 (detect 0) |
| s24 判定 thin / claim / assign / org | **0 (学習しない、決定 10-16)**。旧 250 / 300 / 400 / 500 | 較正集の追加: thin 上限 100 / claim 200 / assign 0 / org 0 | 旧 教師 1,200。LE も除外のまま |
| **s24 判定 小計** | **0** (旧 1,450) | **300** (較正集の追加。旧 教師 1,200 + 較正集 400 = 1,600) | |
| s24 較正集 | — | (上の 300 に含む) | 既存の正解集を流用して交差分割。正例 ≥ 50 に足す分だけ Opus |
| s24 品質の再監査と教師の揺れ | — | 360 | 6 課題 (要約・軸・triage・SIR 要点・PIR・ACH) × (Opus 再監査 30 + 揺れ 30)。判定 4 課題と detect を外した (旧 11 課題・660 → 7 課題・420)。`s24_plan.md` §13.3 |
| **s24 学習総計** | **3,804** (要約の比率 800/3,804 = **21.0%**) | **教師 2,186** (1,526 + 300 + 360) | 旧 4,282 (18.7%) / 2,556 (N1 反映前)、さらに前は 5,732 (14.0%) / 4,096 |
| s24 評価 | — | 916 | 約 800 + 軸 heldout の付け直し 116 (推測) |
| 地政学の救済の廃止判定 | — | 0 (今すぐ: run_logs の取り込みヒント照合) → 必要なら 300 (150 × 2 回) | |
| n20 教師 事象 | 600 (×3 露出 = 1,800) | 50 | 監査・書き直し済み 663 行を引き継ぎ、再監査 50 |
| n20 教師 Spotlight | 200 窓 (×3 = 600) | 200 | |
| n20 教師 状況総括 | 110 (×3 = 330) | 0 | 直した教師 118 を引き継ぎ |
| n20 教師 深掘り 選定 + 本文 | 150 + 150 = 300 (×3 = 900) | 150 + 150 = 300 | |
| **n20 本体 小計** | **3,630** (露出込み) | **550** | |
| n20 翻訳・和訳 教師 | n20a: 0 / n20b: ≤1,200 例 (露出 1 回)。収集は 1,400 (Sonnet、関門で捨てる余裕) | 監査 150 (Opus) | 翻訳比率: n20b = 1,200/4,830 = 24.8% (≤25%) |
| n20 品質の再監査と教師の揺れ | — | 300 | 再監査の追加 120 (Spotlight・総括・選定・本文 各 30。事象は上の 50) + 揺れ 180 (事象・Spotlight・総括・選定・本文・翻訳 各 30 × 2 回の追加 30)。`n20_plan.md` §4 |
| **n20 教師 総計** | n20a 3,630 / n20b 4,830 | **1,000** | 旧 700 + 300 |
| n20 評価 | — | 816 | 直接対読 416 + 翻訳 両腕 300 + 深掘り正解づくり 100 (推測) |
| n20 追加の GPU | n20a・n20b を同じ夜に学習 | — | 追加 約 $30 (計 約 $60、推測) |
| **全体** | | **教師 3,186・評価 1,732 (〜2,032 地政学込み)** | 旧 教師 3,556 (N1 反映前)・5,096 (10-15 の質の監査後)・3,555 (その前)。Spotlight の描き直しで加算候補あり (最大 +153、推測。上の総計に未算入、`n20_plan.md` §2) |

- 旧見積りとの差: **10-16 の判定の学習の撤回** = s24 train 5,732 → 4,282 (−1,450)、s24 教師 4,096 → 2,556 (−1,540 = 判定の教師 −1,200 + 較正集 400→300 の −100 + 判定 4 課題の再監査と揺れ −240)。10-15 監査では s24 教師 2,855 → 4,096 (+1,241 = 軸の全件付け直し +100、triage の理由 +111、detect 作り直し +120、PIR 判定の 16 本 +190、valid 要約 +60、再監査・揺れ +660)、n20 教師 700 → 1,000 (+300)。LE −250・n20 の監査済み −400 + 再監査 +50 は以前の改訂で反映済み。
- **10-16 N1 の反映**: detect −120 (学習しない)・PIR 16 本 −190 (前提が逆)・再監査と揺れ −60 (detect 分)・pair −168 行 (学習例のみ) → s24 train 4,282 → 3,804、s24 教師 2,556 → 2,186 (−370)、全体の教師 3,556 → 3,186。triage は教師の作り直し不要 (入力の組み立てを直すだけ、Opus 0)。計算: 要約 560 + 軸 570 + triage 111 + SIR 285 = 1,526、+ 較正集 300、+ 再監査・揺れ 6×60 = 360。
- **教師の週と評価の週を分ける**。Opus の枠: 教師 3,186 回は s24 (2,186) と n20 (1,000) を別の週に。

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
| 7 | logprob 判定 4 本の扱い | **決定 (利用者、10-16)**: s24 で学習しない。モデルごとのしきい値の自動較正のみ (`calibrate_probes.py`)。assign・org も s 系へ移す (移行中は s23i、較正し直してから)。2 本化の条件に「較正後の精度・回収が現行の本番値以上」。要点の主張の較正集は正例 ≥50 に足す (Opus 200)。摘発は本番未使用のまま |
| 8 | detect の教師 | **撤回 (10-16、`s24_plan.md` §13.7)**: 旧「新しい期間で作り直す」は、作り直した教師の再監査が 3 回とも基準未達 (L1・L2) だったため採らない。**detect は s24 で学習しない**。切替の評価で学習なしの s24 を s23i と比べ、不合格なら `step:synthesis_detect` = s23i を残す |

### 9.2 未決

- なし (s24_plan §12 の項目 1・2 は s24 内の判断として残る)。

## 補記 (10-16): dialog 4 step を s23i で動かした結果 (`data/eval/J1_dialog_report.md`、各 5 件、haiku と比較)

| step | s23i | 扱い |
|---|---|---|
| selector_proposal | 取れる記事 URL が haiku と一致 (4 ソースで Jaccard 1.00)。残る 1 ソース (wilsoncenter) は両方 0 件で取れていない (N1 #72) | s 系で可 |
| precise_search | Opus の適合度 5.94 対 haiku 5.78 で同等、所要は中央 42s 対 29s | s 系で可 (待ち時間を運用で確認) |
| assistant_chat | **回答が 5/5 で JSON に包まれ質問に答えない**。盲検 haiku 10 勝 0 敗。ツールの計画は同じ | s 系は不可。n20 の候補。n20 でも崩れるなら外部のまま (2 本化の例外) を判断 |
| pir_compile | 形は 5/5 正しい (haiku は 1 件 JSON 破損)。**10-16 に採点の誤りを直して測り直した** (`J1_dialog_report.md` 末尾): 30 日の該当 0 件は評価の使い方の誤り (LLM 判定が有効のまま id を変えた・limit=1000 で約 4 日)。LLM 判定を両腕で切り全件で数えると、**現行の PIR との Jaccard は haiku 0.50 に対し s23i 0.05** (s23i は supply_chain 0 件・geo_dprk 560 件・cyber_ops 1,379 件と大きくずれる)。標本は 5 件のみ | **s 系は不可** (形は合うが中身の質が haiku に届かない)。外部のまま。n20 に寄せる案は同じ測り直しをしてから |

`fallback:dialog` を s23i にする案は撤回 (チャットが崩れる)。予備は今は設定しない。
