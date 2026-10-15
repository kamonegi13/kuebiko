# s24 (fast ティア) 学習の設計 (2026-10-15、案)

> 状態: 案。利用者の承認待ち。数字はすべて出典ファイルからの再掲。推測には「推測」と書いた。
> 関連: s23 の計画 `next_models_s22_n20.md` §9、学習要領 `training_recipe_v2.md`、目標値 `docs/accuracy_targets.md`、命名 `docs/model_tier_architecture.md` ⑪。

## 1. 目的

1. **暴走を減らし、暴走率を合否線にする**。s23 で暴走が 2.0→5.3% に増え、本番は補間版 i58 (= s23i) にした。暴走は s23 側の成分。
2. fast ティアが担う step の精度未達のうち、**教師データで直せるもの**を直す (§3)。対象 = 棚卸しの B1/B2/B4/B9/B10 + 今回足した抽出欄・軸・SIR 要点。
3. 本番の s23i と補間した `s24i` を作り、退行を実績のある側へ戻す (`model_tier_architecture.md` ⑪)。

B 番号 (10-12 の棚卸し): B1 = 要約の欠落 20.7% / B2 = 要約の事実誤り 6.5% / B4 = 関与国の適合 69.1% / B9 = S3 の適合 74.5〜75.2% / B10 = triage 93.6% (v4 基準)。

## 2. 現状 (s24 が置き換える範囲)

- 本番の割当は model_tiers 版 40 (10-12)。`fast = kuebiko-sft:s23i`、ACH (`step:synthesis_analysis`)・detect (`step:synthesis_detect`) も s23i。
- 素の 26B 系の step: `article_translate`・`actor_sync`・`triage_geo_rescue` (kuebiko-sft:26b)、`assign_evidence_judge`・`org_identity_judge` (gemma4:26b)。
- fast に解決される step (`src/tools/model_tiers.py` STEP_REGISTRY) と s23 での学習:

| step | s23 で学習 | 備考 |
|---|---|---|
| article_summary | あり (692) | 抽出欄 (関与国・被害国・被害組織・業種・アクター・TTP) もこの出力 |
| triage | あり (705) | 本番は TRIAGE_FLAT=1 (10-12) |
| severity_axes | あり (419、本文入力) | ⚠ **本番は `AXES_FROM_BODY` 未設定 = 要約から付けている** (10-15 に本番コンテナで確認)。学習と本番の入力がずれている |
| pir_llm_judge | あり (729) | |
| pir_daily_focus | あり (285、s22 から) | 10-11 に「要約を切らずに渡す」組み立てへ変更。教師は旧形 (推測、要確認) |
| event_kind / pair_judge | あり (591 / 300) | 接頭辞は system 側 |
| synthesis_analysis / synthesis_detect | あり (361 / 255、引き継ぎ) | |
| relevance_jp_cascade | なし | 自由形式の bool JSON |
| thin_body_judge / pir_focus_claim_judge / alert_law_enforcement_judge | なし | logprob 判定。しきい値がモデル依存 (薄い本文の 2 段目は s23i で未較正) |

- s23 の学習データ (`data/mlx/dataset_s23_manifest.json`): train 4,537 / valid 149。設定 1 epoch・rank 32・lr 2e-5 (s22 と同じ)。

## 3. 未達項目の分類

(a) = s24 で直せる (モデルの判断・抽出・生成の誤り) / (b) = s24 では直らない。

| 項目 (出典) | 現状 | 分類 | 理由 |
|---|---|---|---|
| B1 要約の欠落 (accuracy_targets) | 20.7% (≤10%) | a | 残る欠落は被害の中身・件数・結論。教師の基準で変わる |
| B2 要約の事実誤り | 6.5% (≤2%) | a (一部) | 反転・所属・主体の取り違え・格上げは生成の誤り。見出しだけの記事は THIN_BODY 関門 |
| B4 関与国の適合 (B4_upstream_stageB_report) | 関門なし 62.4% / 関門後 81.8% (案 85%) | a + b | サイバー記事に国を付けるのは教師で直る。地政学の列挙過多は関門と分担 |
| B9 S3 の適合 | 本番の軸 75.2% / 正解の軸 81.1% | a + 切替 | 軸を本文から付ければ届く見込み。AXES_FROM_BODY=1 とセット |
| 深刻度の軸 impact/scope/confirmation (B4) | 71/77/77%、脆弱性・勧告層 45〜59% | a | 本文の読み違い。教師が勧告層に薄い (推測) |
| B10 triage | 93.6% (≥95%)、重い取り違え 2 | a | 業務停止の条件の無視・「ロシア」の語への反応 |
| 被害業種 (B4) | 76.8%、cyber 層 53.7% | a (要確認) | canonical 語彙への写像の問題が混ざる可能性 (推測) |
| 被害組織 (B4) | cyber 層 87%、alert 層の適合 60% | a | 発行元・機関名を被害者に入れる。被害国の誤り 81% の主因 |
| 被害国の publisher_country 型 | jp 層 8/13 | a | 発行元の国を被害国にする |
| 主題アクター・actor 欄 (B4) | cyber 層 83.7%、alert 層 68%。重複 alert 56 件でアクター一致 28 | a | 欠落が重複の取りこぼしの一因 |
| TTP の適合 | 関門後 0.70 (≥80%) | a | s23 の TTP 水増し (20 個以上の例 10 件) |
| SIR 別の要点の誤り | 27.5% (≤2%) | a (一部) + b | 記事の混同・主体の取り違えは生成の誤り。安全網は PIR_FOCUS_CLAIM_GATE |
| 翻訳の重い問題 17.1%・失敗 1,310 件 (A2) | 原文の写し ~1,070・JSON 混入 70 | b | s 系の担当外 (素の 26B)。s 系に学習させて JSON に偏り壊れた前例 |
| 規模 (件数) cyber 層 74% | | b | 本番値は importance_v2 の正規表現 (繁体字「萬」の取りこぼし) |
| 群化 85%・回収 46%・統合 80% | | b | eventnews の関門・ML・照合キー |
| alert の対象外のうち部品で説明できない 73/114 | | b | 配信の規則 (方針で後回し) |
| 台帳の割当・日本関連回収の死角・本文抽出・S3 見逃しの判定 | | b | ML・probe・取得、または正解不足 |
| 境界の多さ (triage 41/150・関与国 92/150) | | b | 基準の文の改訂 |

## 4. 範囲 (step ごと)

教師は Opus 5.5 中 (`data/mlx/adjudication_cache.py` 経由、裁定役・エフォート・基準の版を各行に記録)。Strata は **型 A (選別・整形・検査) を下作業に使う**、型 B (教師の文面の下書き) は使わない (10-12 決定、10-15 の盲検 28:2 で再確認)。教師の入力は**本番と同じ組み立て関数**で作る。件数は案。

| step | 何を | 教師 | 件数 (s23 → 案) | Opus 回数 (推測) |
|---|---|---|---|---|
| 要約 | ①反復ループの例・TTP 20 個以上の例を除く ②長さ分布を s22 に揃える (字数で切らず、選び直すか Opus に意味を保って縮めさせる) ③全例に mitre_evidence ④書き直しの残り 60 件 ⑤**抽出欄の点検**: 関与国 (サイバー記事は []・行為主体と標的の国だけ)・被害国 (発行元の国にしない)・被害組織 (被害者だけ)・業種 (canonical)・アクター (欠落させない)。Strata 型 A と規則で疑わしい例を選び Opus が直す ⑥被害の中身・件数・結論を落とさない例を新しい長い cyber 記事で足す | Opus / Strata 型 A (選別) | 692 → 約 800 | 約 400 |
| 深刻度の軸 | 本文入力 12 欄 (`axes_teacher_v4` を基に)。**脆弱性・勧告層と cyber 層の impact・scope・confirmation の難例**を足す。scope は版 .8 の定義 | Opus | 419 → 約 570 | 約 150 |
| triage | `triage_flat_teacher_v1` を基に、**教師の入力を本番の triage_rubric 版 6 (境界の型 A〜I) で作り直す**。難例 = 業務停止の条件・国名の語への反応。同じ記事 × 基準を変えた対を含める | Opus | 705 → 約 800 | 約 100 |
| SIR 別の要点 | 入力の形のずれを先に確認 (Opus 不要)。ずれていれば新しい組み立てで教師を作り直す。記事を混ぜない例・出典を 1 つにする例 | Opus (条件つき) | 285 → 285 | 0〜120 |
| PIR 判定 | `pir_teacher_v2` を引き継ぐ | — | 729 | 0 |
| ACH / detect / kind / pair / 知識 QA | 引き継ぐ (replay)。ACH は教師の修正で変わる例を数えてから決める | — | 361 / 255 / 591 / 300 / 200 | 0 |

- 合計 train 約 4,900 (推測)。損失は例ごとの平均なので**課題の重み = 例数**。要約を 16% 前後に保つ (`training_recipe_v2.md` §1.2・§2.2)。
- 翻訳・logprob 判定・relevance_jp_cascade は学習しない。
- 設定は s23 と同じ (1 epoch・rank 32・lr 2e-5)。1 回に変えるのはデータだけ。
- RunPod H100 で約 3 時間 (推測)。int8 で取り込む (int4 は効果が消える)。
- 学習と本番の入力の形は、長い実例複数の prompt_eval_count で突き合わせる。

## 5. 既存の方針 (守ること)

- **暴走率を合否線にする**。主因は summary / remediation の文の反復ループで、関門では防げない。
- **教師から除く**: 文の反復を含む例・TTP 20 個以上・契約外の欄 (bluf 等)・ナビの残骸を入力にした例。s23 で書き換えた 141 件 (TTP 平均 2.7→5.7) は TTP に上限 + mitre_evidence を付けるか除く。
- **配信側の上限**: maxItems は実装済み (`src/pipeline/summary.py` `_LIST_MAX_ITEMS`)。num_predict は既定 4096、上限は s24 の長さ分布を見て決める。**文字列の maxLength は入れない** (09-26 に暴走を誘発)。**repetition penalty は使わない** (JSON の構造トークンを罰して逆効果)。
- **TTP_EVIDENCE_GATE は on のまま** (i58 の TTP は生 0.26、関門後 0.71)。
- **証拠・要約を字数で機械的に切らない**。
- **教師の契約は本番のスキーマと同じ**。学習した step だけに接頭辞 (`task_prefix.TASK_MARKERS`)。
- **関連性 (日本・SIR・注視国) は triage の学習に入れない**。決まりごとで導く。

## 6. データの偏りを作らない

1. 基準を読む課題 (triage・PIR 判定) は、同じ記事 × 基準を変えた例を混ぜる。運用の方針はラベルに焼き込まず基準の文に書く。
2. 難例は**両向き**にする (「ロシア」の語があって正しく high / 正しく low を同数程度。勧告層の軸も S3 になる / ならないを両方)。
3. 新しい記事は記事自身の category で層化する (配信先で層化しない)。
4. 学習前に領域別・配信先別のラベル分布を数え、s23 との差を表にする。
5. 評価に「基準を差し替えたら判定が追従するか」を入れる (`data/mlx/sir_geo_triage_probe.py` を型に)。

## 7. 評価

### 7.1 学習の記事と評価の記事を分ける

`build_dataset_s23.py` の guard を引き継ぎ、次を train/valid に入れない: triage_gold v2〜v4、pir_judge_gold v2・v3、severity_gold_v4、severity_random_v3、i58_severity_eval v1・v2、c2a (要約 92)、c2d (関与国 150)、c3 (SIR 要点 80)、s5_claims_gold、r1g_gold_v2、**upstream_gold_B4 の 320 記事 (評価専用)**、triage の難例の元にした v4 の誤り (`i58_triage_v4_errors.jsonl`、難例は似た型の別記事から作る)。B4 の 320 には既に学習済みの alert_quality 62 件が含まれるので、学習外の部分を別に出す。

### 7.2 合否線 (s23i を基線に、同じ夜に測る)

1. **暴走率** (`data/mlx/runaway_probe.py` 300 件): 狭い定義 (文の反復・2,500 字超・mitre の重複) は s23i の 1.0% 以下、広い定義 (summary >2,000 字 / remediation >1,000 字 / TTP ≥20) は s22 の 7/150 以下 (s23i の広い定義は未測定 → 最初に測る)。**超えたら他の数字によらず不合格**。
2. **部品ごとに Opus の正解で測り直す** (学習外、s23i 比で退行なし + 狙った項目の改善):

| 部品 | 正解集 | 合格の目安 |
|---|---|---|
| 要約の誤り・欠落 | c2a (`measure_C2a.py` / `_b.py`) | 欠落 20.7% から下がる・誤り 6.5% 以下 |
| 要約の盲検 | `scripts/eval_goldset.py` + 20 件を読む | s23i に負けない |
| 抽出欄 | B4 の 320 (決定的照合 → 不一致だけ B-lite) | 関与国の適合・業種 (cyber)・被害組織・アクター・被害国が s23i を上回る、回収 80% 以上 |
| 関与国 | c2d 150 | 適合が上がり回収 80% 以上 |
| 深刻度の軸 | B4 層別 + i58_severity_eval (severity_gold_v4 は調整用で参考) | 各欄 80%・平均 85% 以上、勧告層の 3 欄が上がる |
| 深刻さ S3 | severity v4 (適合) / severity_random_v3 (見逃し) | 適合 80% 以上・境界除く見逃し 0〜2%。AXES_FROM_BODY=1 で測る |
| triage | triage_gold_v4 (本番プロンプト版 6) | 境界除き 95% 以上・重い取り違え 2 以下 |
| PIR 判定 | pir_judge_gold_v2・v3 | 境界除き 100% を保つ |
| SIR 別の要点 | c3 (`measure_c3.py`、2 回裁定) | 27.5% から下がる |
| ATT&CK | ttp_goldset (関門後) / CTIBench | 関門後 0.71 以上 |
| kind / pair / ACH / detect | eval_event_kind 300 / eval_pair_vs_frozen / ach_daytime_eval / detect_goldset | 差が誤差内 |

3. 補間 `s24i` (s24 × s23i) も同じ一式。t は一時タグ (i25 / i50 / i65) で試す (s23 では t ≥ 0.65 で暴走増)。採用した 1 本だけを s24i に改名。

## 8. s24 で扱わないものと担当

| 項目 | 担当 |
|---|---|
| 翻訳の重い問題・失敗 1,310 件 | 翻訳の経路 (素の 26B)。原文が写る原因の調査 + TRANSLATE_FIDELITY_GATE (ident) |
| 件数の抽出 (萬・千万) | 規則 (`importance_v2.stated_victim_count`) |
| 関与国の地政学層の残り | INVOLVED_COUNTRY_GATE (10-15 on)。規則 3 のしきい値・協力語の例外は 10-15 に総当たりで現行を超えず (`data/eval/C1_icg_tune_report.md`) → 残りは s24 の抽出欄の点検に寄せる |
| 群化 85%・統合 80%・重複 alert | eventnews の関門 (CVE 不一致・照合キー・伏字 G2)・ALERT_DEDUP_EVENT。記事対の CVE 判定 (G1) は 10-15 に効果 0 で不採用 (`data/eval/C3_merge_gates_report.md`) |
| alert の対象外の残り 73/114 | 配信の規則 (上流が先の方針で後回し) |
| 台帳の割当・見立ての反転 | ML + probe・保留の仕組み (測り直し要) |
| 本文の取得・日本関連の死角・S3 見逃しの判定 | 取得・取り込み ML・正解づくり (無作為 400 件以上) |
| 境界の多さ | 基準の文の改訂 |

## 9. リスクと対策

- **多課題の干渉**: 例数がそのまま勾配の配分。要約を増やしすぎない。分類・抽出は数百例で飽和 (`multitask_interference.md` §3・§6)。
- **忘却**: 引き継ぐ課題を replay として混ぜ、評価で確かめる (`catastrophic_forgetting.md`)。退行は補間で戻す。ルータ方式・アダプタ単体はこの構成で使えない (`lora_composition.md`)。
- **未学習の自由文 step が切替で壊れる**: 切替前に fast が担う**全 step の実物の出力**を見る (§2 の全行)。relevance_jp_cascade、logprob 判定 3 本 (しきい値を較正し直す: 薄い本文 = p2b・要点の主張 = S5 の分割・摘発 = R1b_le_gold)、SIR 別の要点 (`data/mlx/focus_same_run.sh`)。
- **下流の ML が軸の供給元に依存**: s24 + AXES_FROM_BODY=1 に切り替えたら detect ML を作り直し digest で比べる。配信の inline の軸 (ROUTING_V2_INLINE_AXES) も同じモデル。
- **許可リストの更新漏れ**: `task_prefix.PREFIX_TRAINED_MODELS`・`summary.EVIDENCE_TRAINED_MODELS` にタグを足す。`import_interp.sh` の a/b を直す。
- **Opus の枠**: 教師約 770 回 + 評価約 600 回で週枠の 1〜3 割 (推測)。教師と評価は週を分ける。
- **物差しの版**: 正解の版と本番のプロンプトの版をそろえる。'none' と null をそろえる。

## 10. 手順と概算

1. **事前 (Opus なし、1〜2 日)**: s23i の基線 (広い定義の暴走率・B4 の学習外)、要約の入力の形と pir_focus の組み立てのずれの確認、Strata 型 A + 規則で教師候補の選別 (反復・TTP ≥20・長さの外れ・mitre_evidence 欠け・抽出欄の定義違反)、領域別の分布表。
2. **基準の文の確認 (半日)**: triage_rubric 版 6・軸 版 .8・関与国の定義・業種の canonical・要約の基準 (結論・被害の中身)。基準を変えたら正解集の付け直しが要るかも判断。
3. **教師 (Opus 約 770 回、2〜3 日)**: 要約の点検・書き直し → 軸の難例 → triage の難例 → (条件つき) 要点。
4. **データの組み立て**: `build_dataset_s24.py` を s23 版から (guard を拡張)。最後に `audit_sft_dataset.py`。
5. **学習**: RunPod H100 約 3 時間 (推測)、int8 で取り込み、prompt_eval_count で入力の形を突き合わせる。
6. **評価 (夜間、本番停止)**: `eval_s23.sh` を元に。補間の t を振る。続けて Opus の測り直し (約 600 回)。
7. **切替 (利用者の承認後)**: 全 step の実物確認・logprob 判定の較正・AXES_FROM_BODY=1・detect ML の作り直し。戻し先は model_tiers 版 40 (s23i)。`data/cloud-runs/s22|s23` と s23i は消さない。

## 付記: 既存の s24 計画に無く、10-15 に組み込んだもの

1. 要約の抽出欄 (業種・被害組織・アクター・被害国の発行元の混同) の教師の点検 (B4: cyber 層の業種 54%・alert 層のアクター 68%)
2. 深刻度の軸の難例を、脆弱性・勧告層の impact・scope・confirmation (45〜59%) に絞って追加
3. triage の教師の入力を、本番の triage_rubric 版 6 (境界の型 A〜I) で作り直す
4. SIR 別の要点の教師の入力の形の確認と作り直し
5. 本番の軸が要約から付いていること (AXES_FROM_BODY 未設定) と、学習が本文入力であることのずれを解消し、切替とセットにする
6. B4 の 320 記事・triage v4 の誤りの記事を guard に加え、B4 の学習外を別に出す
7. 切替時に logprob 判定 3 本と relevance_jp_cascade を較正し直し、detect ML を作り直す
8. 長さの分布は字数で切らず、選び直すか意味を保って縮めて揃える
9. 難例を両向きにして偏りを作らない (国名の語・勧告層)
