# 事前登録: s26r 切替後の importance の low への寄りは較正か誤りか (段 1)

- 登録: 2026-11-20 (結果を見る前にコミットする。腕の生成と採点はこのコミットの後)
- 発端: 11-19 の通し監査。11-17 の s26r 切替の後、要約の importance の low 率が 24% → 39%
  (Grok・分析専用を除く、同じ feed でも同方向)。切替の評価は本番と違う基準 (v8r7) で測られ、
  合格線は high の回収と適合だけで medium / low の境を見ていなかった。
- 設計: `data/design/pipeline_improvements_2026_11_20/D4.md` §P1-5 (a) と点検の修正 (PLAN.md §1)。

## 材料

- 正解: `data/eval/rp9_imp_s26/gold.jsonl` (Opus 5.5 effort high、150 件、low 72 / medium 53 /
  high 25、`boundary_self_report` 78 件、基準は `s26-imp-v8r7`)。
- 入力: `data/eval/baseline_arms_1112/inputs/summary.jsonl` (150 件、本番の要約プロンプト v8r4 で
  組み立て済み・凍結。正解と鍵が 150/150 一致)。
- 腕 A (既存): `arms/s24ci65_base/summary.jsonl` (kuebiko-sft:s24ci65 × v8r4)。
- 腕 B (新規): `arms/s26r_v8r4/summary.jsonl` (kuebiko-sft:s26r × v8r4、同じ入力・同じ設定)。
  生成は `tools/run_arm.sh run s26r_v8r4 kuebiko-sft:s26r summary` (使い捨てコンテナ、本番の
  外、GPU の空き帯)。

## 指標 (B と A を同じ記事で比べる)

- 主: 正解が medium または low の記事での正解率 (予測の importance == 正解)。**全 150 件のうち
  正解 {medium, low} の 125 件**で計算。境界 (`boundary_self_report`) は別の層として同じ指標を
  並べて報告する (判定には使わない)。
- 誤りの向き: M = 正解 medium → 予測 low、L = 正解 low → 予測 medium。
- 重い見逃し H = 正解 high → 予測 low (25 件の中)。
- 参考: 各腕の予測の low 率 (本番で見えた寄りが同じ入力でも再現するか)。
- 検定: 正解 {medium, low} の記事で、A だけ正解 / B だけ正解の件数の McNemar の正確検定 (両側)。

## 線 (この登録で固定する)

- **受容**: 正解率_B ≥ 正解率_A − 0.03、かつ H_B ≤ H_A、かつ H_B ≤ 1。
- **誤り**: McNemar p < 0.05 で B が劣る、または H_B ≥ 2。
- **判定不能**: 上のどちらでもない → 段 2 (D4 の設計: 新しい標本・両方向の付け直し・Opus 盲検)。
- 但し書き (先に確かめる): 正解の基準 v8r7 と本番の v8r4 の importance 節で medium / low の境の
  文が違う場合、段 1 は「参考」に下げ、判定は段 2 に回す。この確認の結果は採点の前に記録する。

## 対処

- 受容: 切替後の分布を新しい基線とする (見張りの基線を更新)。
- 誤り: 利用者判断 (要約だけ戻す / 全体を戻す / 基準の補正)。`MODEL_RESIDENT_ENFORCE=1` の下では
  要約だけの戻しは保存が拒否されるので、旗を一時的に外す必要がある。
- 判定不能: 段 2。

## 但し書きの確認 (2026-11-21 04:3x、採点の前に記録)

- 正解の基準 `s26-imp-v8r7` (`data/eval/S24A_rubric_v8r7.json`) と本番の v8r4
  (`config/prompts/summarizer_rubric_v8r4.yaml`) の importance 節を比べた。**medium / low の境の文が違う**:
  v8r4 は「脅威としての actionability」(low = 一般的な動向・論説・解説・長期観察・具体的脅威含意の薄い記事)、
  v8r7 は「平たい深刻さ」(S1〜S3、medium / low に項目の一覧。例: v8r7 は「新しい手口を含む脅威の分析・
  国家系の活動の分析」を medium、「悪用も PoC もない定例の修正」を low に明記)。
- 登録の但し書きどおり、**段 1 は参考に下げ、判定は段 2 に回す**。腕 B は 150 件中 134 件で 40 分の timeout に
  当たった (03:45〜04:25)。残り 16 件は空き帯で再開して揃え、参考の表として採点する。
