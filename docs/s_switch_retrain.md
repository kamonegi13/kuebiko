# S のモデル切替時の ML 一括の学び直し手順 (S-SWITCH-RETRAIN)

常駐の S (`model_registry.RESIDENT_SLOTS["S"]`、判定・分類・要約系) を切り替えるときに、S が作る特徴を
入力にしている ML を**同時に学び直し、割当の版と ML の JSON を組で切り替える**手順。
型は D1 (深刻度の軸の本文入力への切替、2026-10-27) と BACKUP-S26。`event_kind` を LLM のまま残す決定
(2026-11-03) により、S を替えるたびにこの手順が要る。場当たりで 1 本だけ替えない。

道具: `src/tools/ml_s_dependency.py` (どの ML がどの S 由来の特徴を持つかをコードから導く) と
`PYTHONPATH=. uv run python scripts/check_s_switch_ml.py [--s <新しい S>]` (一覧と学習時の S の整合。
テスト `tests/unit/test_ml_s_dependency.py` が、新しい特徴量・新しい ML JSON の見落としを落とす)。

## 1. どの ML がどの S 由来の特徴を持つか (一覧は `check_s_switch_ml.py` で再生成)

| ML (`config/models/`) | 強い S 由来 (種別・軸・確率・対の判定) | 弱い S 由来 (要約の派生) | 学習時の S の記録 | 学習スクリプト |
|---|---|---|---|---|
| detect_model.json | event_kind、severity_axes (`axis_*`) | importance・category・件数 | `axes_model`・`kinds_model` | `scripts/train_detect_ml.py` |
| severity_s3_model.json | severity_axes の問いの確率 (`p.*`)、event_kind | cat・type | `probe_model`・`kinds_model` | `scripts/train_severity_s3_model.py` |
| deep_dive_model.json | event_kind | importance・category | `kinds_model` (既定 null) | `scripts/train_deep_dive_ml.py` |
| relation_model.json | event_kind (`kinds_*`) | 被害組織・国・業種の件数 | `kinds_model` | `scripts/train_relation_model.py` |
| pair_model.json | pair_judge (`llm_same`/`llm_known`)、event_kind | cos_summary 等 | `trained_llm_models` (現在 `[]`)・`kinds_model` (種別を渡したとき) | `scripts/train_pair_model.py` |

対象外 (理由は `NOT_RETRAINED`): assign_model(.json/_probe) は gemma4:26b・s23i 固定、relevance_jp は埋込のみ、
alert_dedup は pair_model の確率 (`p_pair`) を弱い特徴にするので **pair_model を学び直したら影響を見る**。

学習時の S の欄 `kinds_model` (種別を付けた S。DB・既存ファイル・混在なら null) を 4 本 (+ pair) の JSON に足した。
`--kinds-model <S>` で明示、省略時は `--kinds` の jsonl の `model` 欄が 1 種類ならそれ。null・欄なしは `unrecorded` 警告
(合否は記録の有無でなく測定で決める)。本番の loader は欄を読まず、有無で計算は変わらない。学び直しの出力 JSON には使った S を必ず書く。

## 2. 特徴の付け直し (新しい S で、staging の表またはファイルに。本番の列は書かない)

学習データ (記事・事象・対) に付く S 由来の特徴を、新しい S で付け直す。**GPU を使う段は既存の枠の中で行う**
(軸 = GPU-D2、問いの確率 = GPU-D3。新しい枠は作らない。枠の割り当ては各枠の `gpu_basis` を見直すときに決める)。

| 特徴 | 付け直し | 出力先 (本番を触らない) |
|---|---|---|
| severity_axes (軸) | D1 の `data/mlx/axes_body_backfill.py` の型。学習集合 + 直近 14 日 | jsonl (D1 は `d1_axes_body.jsonl`) |
| 深刻さ C の問い (確率) | `scripts/train_severity_s3_model.py --model <新しい S>` が学習集合 604 件に問いを当てる | 学習時に staging JSON へ |
| event_kind (種別) | 学習集合の記事・事象の種別を新しい S で分類し直す (LLM、中央値 0.22 秒/件) | jsonl |
| pair_judge (対の判定) | 正解集の対に `llm_same`/`llm_known` を新しい S で当て直す (`pair_same_input_control.py` の型) | jsonl |

CPU だけの段 (学習・比較) は GPU 不要。

**detect の対照腕**: detect の学習 goldset 1,882 件のうち 840 件は現行の種別ファイルに無く `other` で学ばれている。
`relabel --what kind` は全件を付けるので、新 S と現行を比べると「S の変更」と「欠けの解消」が混ざる。
`relabel --s <現行 S> --what kind` (出力 `kinds_<現行S>.jsonl`) で現行 S の対照の種別を作り、`train --control-s <現行 S>` で
`detect_model.json.staging_control` (軸は DB のまま) を学ぶ。合否は新 vs 対照 (S の効果)、対照 vs 現行は参考表示。

## 3. 5 本を同時に staging へ学び直す

各学習スクリプトの `--out` に `config/models/<名前>.json.staging` を渡す (本番の読み手はこの名前を読まない。
`--dry-run` で先に確認)。**5 本とも同じ新しい S の特徴で学ぶ**。1 本だけ替えると、旧 S の分布で学んだ ML
と新 S の入力が混ざる (D1 §6 の「片方だけの状態は避ける」と同じ理由)。出力 JSON の
学習時の S の欄 (`axes_model`/`probe_model`/`trained_llm_models`) を新しい S にする。
pair は `scripts/train_pair_model.py` (旧 `data/eval/train_v5.py`、CPU のみ)。付け直しの入力は各 trainer の引数で渡す:
detect = `--kinds --axes --axes-model`、severity_s3 = `--items --probe-file --max-missing --kinds`、
relation = `--kinds`、pair = `--kinds --llm-scores --llm-model`、deep_dive = 付け直しの入力なし
(本番の選抜が種別を渡さないため)。`scripts/s_switch_retrain.py train` がこれらを組んで staging へ出す。
引数を省くと従来と同一。`measure` (`uv run --with scikit-learn python scripts/s_switch_retrain.py measure --s <新 S> [--control-s <現行 S>]`)
が severity の AUC・適合・回収 (固定閾値 0.50)、relation の現行の回収、pair の cos 帯ごとの same 率のずれ、detect の AUC 差の下限と P(悪い)
を計算して `train_metrics.json` へ重ねる。手で足すのは detect の開設件数 (審判) だけ。手順の詳細は運用者ローカルの `data/eval/s_switch_retrain_procedure.md`。

## 4. 合否の関門 (staging の JSON で測る。D1 の値を基準にする)

| ML | 関門 | 出典 |
|---|---|---|
| detect | AUC が現行より有意に悪くない (補正 t の 95% 区間の下限 > -0.01、baycomp P(悪い) < 0.2)、かつ審判済みの「新だけ」の開設 >= 「現行だけ」。1 日の選抜が設計の約 6 件から大きく外れない | D1 §4 |
| severity_s3 | 分割外の AUC・適合・回収が現行 (AUC 0.916・適合 0.69・回収 0.83、閾値 0.50 据え置き) を下回らない | CLAUDE.md §7 10-28 |
| deep_dive | held-out 窓の相関が現行 (0.33) を下回らない | `deep_dive_model.json` の `holdout_corr` |
| relation | 正解集の適合率 >= 0.8 (`--min-precision`)、現行の同閾値の回収を下回らない | `train_relation_model.py` |
| pair | 同じ入力で `same` 率が cos の帯ごとに現行から大きくずれない、正解集の適合率が現行を下回らない | 10-29 版 45 の教訓 |

加えて、軸の S3 件数の増減と alert 候補の変化の見積り (`m3b_routing_replay.py --axes stored`、D1 §4-3)。
1 本でも不合格なら**5 本とも出さない** (組で切替・組で戻す)。不合格の本は旧 S の JSON を維持し、
その本が使う特徴だけを旧 S で付け直す案は採らない (分布の混在になる)。

## 5. 割当の版と ML の JSON を組で切替・組で戻す

1. 退避 (BACKUP-S26 の型): 5 本の JSON を `data/model_backups/<名前>.json.bak_<旧 S>_<日付>` へ。
   表 `article_severity_axes_bak_<名前>`/`article_importance_v2_bak_<名前>` を `CREATE TABLE … AS SELECT` で作る。
2. 軸の取り込み (D1 §5-2): 付け直した範囲の `article_severity_axes` を新しい S で置換し、同じ id の
   `article_importance_v2` を DELETE (毎時の段が再導出)。
3. 切替は一度に (安全な時間帯 = 毎時 :16-:29 / :35-:57、朝夕ブリーフ・深夜バッチの前後を避ける。CLAUDE.md §7):
   - 5 本の staging を `config/models/<名前>.json` へ (config は rw マウント。`load_*` は実行ごとに読む)
   - 割当の版を API で進める (`POST /api/v1/model-tiers`、基本の 5 ティア + 必要な `step:` 上書きを送る。CLAUDE.md §2 の切替手順)
   - 旗が要るもの (`AXES_FROM_BODY` 等) は同じ時刻に
4. 確認 (切替後 1 日): `PYTHONPATH=. uv run python scripts/check_s_switch_ml.py` が `mismatch` 0、
   `detect_ml_axes_model_mismatch` の警告が消える、`severity_axes_hourly_done`・fill-rate・S3 の日次件数が §4 の見積り内、
   `scripts/check_model_end_state.py` が緑。
5. 戻し (**片方だけ戻さない**): 割当を API の revert (`POST /api/v1/config-history/model_tiers/revert`、戻し先の版) で戻し、
   同時に 5 本の JSON を退避から戻し、軸の表を `_bak_` から戻す。割当だけ・JSON だけを戻した状態は、新 S で学んだ ML と
   旧 S の入力 (またはその逆) の組になるので避ける。

## 6. 記録と宿題

- 切替の記録: 割当の版番号・5 本の JSON の sha・退避のパス・付け直しの jsonl のパスを CLAUDE.md §2 の「現在の割当」に追記する。
- 宿題: (a) pair の学習スクリプトを `scripts/` に置く (b) (済) `kinds_model` を足した
  (c) alert_dedup は pair 学び直しの後に p_pair の分布を確認する。
- 最初の使い手: SW-S26 (S26-ML-RETRAIN が detect・severity_s3、pair・deep_dive・relation は判定して必要なら)。
