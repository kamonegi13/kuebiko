# s24_plan.md 抜け漏れレビュー (2026-10-15、Opus) — 反映方針つき

反映方針: (推) = オーケストレータの推奨で反映。(判) = 利用者判断待ちとして設計書に「未決」で書く。

## 高
1. triage の物差しが v4 のまま → B10 は本番 (TRIAGE_FLAT=1) で既に 96.2% (triage_gold_flat_v1、C6)。§7.2 は triage_gold_flat_v1 (境界除く ≥95%・重い取り違え 0) + 取り込みの取りこぼし (C6 の方法) で測る。v4 は合否に使わない。(推)
2. triage の教師の作り直しの前提が消えた → build_flat_triage_prompt は基準を差し込む口がない (src/tools/triage_flat_rubric.py:95)。平たい正解での誤りは過大 4 のみ。triage は triage_flat_teacher_v1 を replay、Opus 0 回。境界の型は足さない (段 B で効果なし)。基準差し替えへの追従の評価は PIR 判定にだけ課す。(推)
3. 教師と正解が同じ裁定者・同じ定義 (自己一致、C6 で 146/150 一致) → §7 の数字は「Opus の定義の再現率」と明記。triage・抽出欄・軸の正解から 20〜30 件を答えを伏せて人が読んで確かめる。(判: 利用者が読むか)
4. 抽出欄を「本番の値」と比べる物差しは使えない (本番値の大半は s21、B4 の正解は本番値か Opus かの二択裁定 = 現状バイアス、関門後に VICTIM_COUNTRY_GATE_KEYWORD 未適用、importance/category 固定) → 抽出欄は s24 と s23i を同じ再推論・同じ関門 (関与国・被害国の語の関門の両方) で比べ、各腕の不一致を B-lite (腕の値 vs 正解) で裁定。本番値とは比べない。s23i の業種 (cyber) の不一致を教師づくりの前に B-lite で裁定し、s21 からの本当の退行か確かめる。(推)
5. AXES_FROM_BODY と s24 の順序 → ①呼び出し改修 (済 49f0524f) ②s23i + AXES_FROM_BODY=1 + detect ML 作り直し + inline 軸確認 (D1、data/eval/D1_switch_plan.md) ③これを s24 の基線にする。detect ML の作り直しは s24 で 1 回。(推)
6. 要約の基準の文の改訂 (§11 の決定 2・3) は rubric の新しい版として作り、教師は全例をその版で描画し直す。本番の rubric は s24 の切替と同時に上げる (それまで s23i は旧版)。c2a と B4 の業種の正解を新版で付け直すか判断。(推)
7. fast の全 step の表に digest_deep_dive_select / digest_deep_dive が無い (STEP_REGISTRY では FAST、TASK_MARKERS に印、UNTRAINED_STEPS に s23i/s24 の項なし) → 全 step の扱い表 (学習 / 引き継ぎ / 学習しない / 上書きで 26B) を §2 に。切替前に model_tiers の全 step 上書きを書き出して照合。UNTRAINED_STEPS に s24/s24i の deep_dive 系を足す。(推)
8. 戻し方の単位 = model_tiers の版・AXES_FROM_BODY・detect ML の成果物・logprob のしきい値・summarizer_rubric の版。組で 1 つの手順書。(推)
9. Opus 回数の見積り直し: 教師 約 1,300〜1,500 回 (pir_focus 285 全作り直し・軸 319 付け直し・detect 選び直し)、評価 約 800 回。triage の 100 回は削る。教師の週と評価の週を分ける。(推)
10. 暴走率の合否線: 広い定義 = s23i の 11/300 以下 (s22 の 7/150 は緩い)。解析失敗・時間切れ・契約外の欄も暴走に数える。runaway_set に 2 万字超の記事を足すか別に測る。差は信頼区間で判定。(推)
11. 長い例が max-seq 12500 を超えるおそれ (sft_nan の再発) → 組み立て時に学習のトークナイザで数え、上限超えは除く (切り詰めない)。検査で止める。(推)

## 中
12. guard の漏れ: p2b_items・R1b_le_gold・detect_goldset・ach_daytime_eval・event_kind 300・TTP goldset・merge_labels_opus*・A2 の関与国 70 記事・b5_inputs・triage_gold_flat_v1 を build_dataset_s24.py の guard に足す。(推)
13. detect の引き継ぎ: 150/255 が guard に当たる。title 一致は除き過ぎ。(a) 105 で続ける / (b) 新しい窓から作り直す。見出し一覧に評価記事が入るのは漏えいではない扱いを明文化。(判、推奨は (a) 105 + 見出し一覧の扱いを明文化して title 一致の除外を緩める)
14. logprob 判定 3 本 (thin_body / pir_focus_claim / alert_law_enforcement) は step 上書きで gemma4:26b に固定し、fast の切替のたびの較正をやめる案 (SFT 済みは yes/no に無情報なことがある: s23i は AUC 0.537)。(判、推奨は固定。ただし固定の前に今の s23i との判定一致・較正を測る)
15. 補間 t の選び方を事前に決める (暴走 ≤ s23i かつ退行なしの中で最大の t)、採用 t は別の半分で確認。s24 は素のモデルから学習。一時タグを SFT_TASK_PREFIX_MODELS / SFT_EVIDENCE_MODELS に入れる。(推)
16. 勧告層の confirmation 18% (本文でも要約でも 4/22) は定義のずれの疑い → 難例を作る前に不一致 18 件を読む。全軸の正解の版を .8 にそろえたことを表に。(推)
17. 関与国の教師の定義を c2d/B4 の正解と同じ文に (§4 の「行為主体と標的の国だけ」は狭すぎ。A2 で Opus は参加・署名・支援国を残すべきとした)。評価は raw と関門後の両方。(推)
18. 下流の消費者: event_kind → 群化・pair の特徴、軸 → importance_v2・inline 配信・alert、業種 canonical → forecast burst・I-infra の写像、関与国 → alert の関連性。切替前後で alert の件数と理由・digest・pair 特徴の分布を shadow で比べる。(推)
19. 要約の c2a (92 件) は Opus の揺れの範囲 → 2 回裁定か主張単位、両腕を同じ呼び出しで比べる。(推)
20. 小標本の判定規則: 対応ありの McNemar / ブートストラップ、判定が分かれた例を全件読む、区間が目標をまたぐ項目は「判定不能」。(推)

## 低
21. B4 を弱点選定と評価の両方に使っている → 狙った層の改善は B4 以外の新標本 (層別 50 件程度) でも確認。(推)
22. GPU と本番の時間帯の決まり (夜間の窓・締め切り・止めた間の後追いの確認)。(推)
23. 変えるデータが多すぎて退行時に切り分けられない → 部品の評価で退行したら、その課題の旧データに戻した学習を 1 回だけ行う予算を見込む。(推)

## 問題なしと確認
要約の教師入力 = 本番 v7 で 692/692 一致・接頭辞同じ / 例数 = 重み・replay は recipe どおり / maxLength・repetition penalty 不使用・TTP 関門維持 / 偏り対策 §6 / 翻訳・件数・群化を範囲外 (§8)。
