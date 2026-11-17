# 組織名の別名の判定 (org_identity) の s26 切替 — 部品と手順

2 旗 (どちらも既定 0 = 本番の挙動は不変)。切替 (SW-S26) で 1 にする。

| 旗 | 効果 |
|---|---|
| `ORG_IDENTITY_SFT_JUDGE=1` | `judge_name_pair` が SFT の判定役を拒否せず、org_identity を学習した系統 (`task_prefix.STEP_TRAINED_ONLY_FAMILIES` の `kuebiko-sft:s26`、前方一致) に限り通す |
| `ORG_IDENTITY_ORG_QUESTION_V2=1` | org の問いの「同じ組織」の定義と、両問いの注意書きの最後の文 (`_CAUTION_V2`) を Opus の基準 (`org_identity.OPUS_SAME_ORG_CRITERIA` = rubric org-alias-d-v1 と同じ。運営会社とサービス名・親会社と子会社を足す) にする。教師のラベルの基準と問いの文面を揃える |

課題の印 `[task: org_identity]` は `TASK_MARKERS` に足し、`STEP_TRAINED_ONLY_FAMILIES` で s26 系統に限る。
`decision_probe` の SFT 経路 (raw) が user ターンの先頭へ付ける (学習の入力と同じ形)。
**s26 を model_registry へ登録する (task_prefix=True、前方一致) まで `prefix_for(Step.ORG_IDENTITY_JUDGE, ...)` は空** = 今と同一。
(登録前は s26 は「SFT でないモデル」として扱われ、従来どおり印なしの chat 経路になる。割り当てる前に登録すること。)

## SW-S26 のときの手順

1. `src/tools/model_registry.py` の `_CAPABILITIES` に s26 を登録 (前方一致、`task_prefix=True`)。
2. `config/models/probe_thresholds.json` に s26 の行を足す (PROBE-ROWS-S26 で。**今は足さない**)。
   事前登録どおり event / org とも 0:
   ```json
   "kuebiko-sft:s26": {
     "assign_evidence_judge": {"main": "<s26 で測った値>"},
     "org_identity_judge": {"event_logit": 0.0, "org_logit": 0.0},
     "pir_focus_claim_judge": {"main": "<同>"},
     "thin_body_judge": {"main": "<同>"}
   }
   ```
   4 step の行が揃っていないと `missing_entries` の検査が落ちる。行が無いと `effective_threshold` は None で
   org の判定は何も畳まない (fail-closed)。
3. API で `step:org_identity_judge` に s26 を割り当てる (CLAUDE.md §2 の切替手順)。
4. `.env` に `ORG_IDENTITY_SFT_JUDGE=1` と `ORG_IDENTITY_ORG_QUESTION_V2=1` を足して kuebiko を再作成
   (`docker compose up -d --build kuebiko readonly`)。**片方だけ 1 にしない** (学習した問いの文面と食い違う)。
5. 学習の入力は旗 1 の文面へ置き換えたものを使う: `data/mlx/s26_teacher/rewrite_org_question.py`
   (出力 `org_identity_s26_v2q.jsonl`。本番の関数で組み直して全行を照合する。評価の gold の入力は変えない)。

戻し: 両旗を 0、`step:org_identity_judge` を `gemma4:26b` に戻す。
