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

**推論側 (本番) で S 由来の特徴がどこから来るか** (11-10 にコードで確認):

| ML | 推論の呼び手 | 種別 | 軸・確率・対の判定 |
|---|---|---|---|
| detect | `assessment/stateful.py` → `detect_ml.build_detect_articles` | `article_kinds` (キャッシュ) | `article_severity_axes` (毎時の段 = `Step.SEVERITY_AXES`) |
| severity_s3 | `ui/services/severity_c_shadow_job.py` | `article_kinds` | 9 問をその場で `decision_probe` (`Step.SEVERITY_AXES` の割当) |
| deep_dive | `digest/deep_dive_selector.py:487` | 渡さない (`kinds={}` = 全て other) | なし |
| relation | `eventnews/relation_features.py` | `article_kinds` | なし |
| pair | `eventnews_hourly_job.py` / `pair_shadow.py` | `article_kinds` | `pair_judge` (`step:pair_judge`=s23i 上書き、`EVENTNEWS_PAIR_LLM`) |

⚠ `article_kinds` は `INSERT OR IGNORE` のキャッシュで、**一度付いた種別は S を替えても書き換わらない**。
11-10 の DB は s21 4,779・s24ci65 4,081・s23i 2,995・s17 1,091・roundup_guard 819・gemma4:26b 809 ほかの混在。
切替後も 60 日窓の古い記事 (relation・pair の対、detect の候補) は旧 S の種別で推論に入る。
学び直した ML は新 S の種別で学ぶので、**推論の入力は切替直後ほど旧 S が多い**。
同じく `article_severity_axes` も s24ci65 7,296・s21 2,664・s17 1,772・s23i 349 の混在 (detect は
`detect_ml_axes_model_mismatch` で警告する)。切替時に直近の窓を付け直すかは §6 の判断事項。

学習時の S の欄 `kinds_model` (種別を付けた S。DB・既存ファイル・混在なら null) を 4 本 (+ pair) の JSON に足した。
`--kinds-model <S>` で明示、省略時は `--kinds` の jsonl の `model` 欄が 1 種類ならそれ。null・欄なしは `unrecorded` 警告
(合否は記録の有無でなく測定で決める)。本番の loader は欄を読まず、有無で計算は変わらない。学び直しの出力 JSON には使った S を必ず書く。

## 2. 特徴の付け直し (新しい S で、staging の表またはファイルに。本番の列は書かない)

学習データ (記事・事象・対) に付く S 由来の特徴を、新しい S で付け直す。**GPU を使う段は既存の枠の中で行う**
(軸 = GPU-D2、問いの確率 = GPU-D3。新しい枠は作らない。枠の割り当ては各枠の `gpu_basis` を見直すときに決める)。

| 特徴 | 付け直し | 出力先 (本番を触らない) |
|---|---|---|
| severity_axes (軸) | `s_switch_retrain.py relabel --what axes` (本番の `severity_axes.classify_axes` を本文入力 = `AXES_FROM_BODY=1` で。detect の goldset + held-out 2,416 件、本文の無い記事は飛ばす) | `data/eval/s_switch_staging/axes_<S>.jsonl` |
| 深刻さ C の問い (確率) | `relabel --what probes` (本番の `severity_c.probe_questions` に `model=<新しい S>` を渡す。学習集合 604 件 × 9 問、`items.jsonl` に本文込みで DB 不要) | `data/eval/sevc_v5/probe_<S>.jsonl` |
| event_kind (種別) | `relabel --what kind` (`event_kind.classify`、3,232 記事、中央値 0.22 秒/件) | `kinds_<S>.jsonl` |
| pair_judge (対の判定) | `relabel --what pair [--pair-model M]` (`pair_judge.judge_pair`、365 対) | `pair_llm_<S>.jsonl` |

4 つとも再開可 (済みの記事は飛ばす)・失敗は書かない (次回に再試行)。試走は
`--limit N --max-seconds S --out-dir <scratch>` で短く切る (本番の時間に Ollama を長く占有しない)。
`decision_probe.probe_choice` / `severity_c.probe_questions` の `model` 引数はこの道具のためのもの
(本番の呼び手は渡さない = 割当から解決。denylist の検証は同じに掛かる)。

**同じ S での付け直しは元の値を完全には再現しない** (11-10 試走、s24ci65 で 2 件ずつ): 軸は 7 欄中 1 欄が
変わる記事が 1 件、確率は 1 件で最大 0.30 (`harm_confirmed` 0.59→0.29)、もう 1 件は最大 0.02。
したがって新 S の付け直しを DB / 既存ファイルの現行の値と比べると、S の効果に付け直しの揺れが混ざる。
対照腕 (現行の S で同じく付け直す) と比べること。

CPU だけの段 (学習・比較) は GPU 不要。

**detect の対照腕**: detect の学習 goldset 1,882 件のうち 840 件は現行の種別ファイルに無く `other` で学ばれている。
`relabel --what kind` は全件を付けるので、新 S と現行を比べると「S の変更」と「欠けの解消」が混ざる。
`relabel --s <現行 S> --what kind` (出力 `kinds_<現行S>.jsonl`) で現行 S の対照の種別を作り、`train --control-s <現行 S>` で
`detect_model.json.staging_control` を学ぶ。`relabel --s <現行 S> --what axes` の出力 (`axes_<現行S>.jsonl`) があれば
対照の軸もそれを使う (無ければ DB の軸のままで、警告を出す — 軸の付け直しの揺れが新 vs 対照に混ざる)。
合否は新 vs 対照 (S の効果)、対照 vs 現行は参考表示。

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
| severity_s3 | 分割外の AUC・適合・回収 (閾値 0.50 据え置き) が、**同じ手順で測った現行** (`measure` が現行の `probe_model` の確率で再計算する `current`。11-10: AUC 0.9158・適合 0.686・回収 0.831) を下回らない。付け直しの種別が学習 604 件を覆わない (`kinds_missing` > 0) なら未測定 | CLAUDE.md §7 10-28 (定数 0.916/0.69/0.83 は丸めた値で、現行そのものが適合 0.686 < 0.69 で落ちるため基準にしない) |
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

## 6. 記録・試走の結果・判断が要る点

- 試走 (11-10、s24ci65 = 現行の S で各 2 件、`--out-dir` を scratch に): 確率 2 件 8 秒・軸 2 件 4 秒 (どちらも
  Ollama に既に載っていた s24ci65)。上の「再現しない」の数字はこの試走から。
- 11-07 の通し (`data/eval/s_switch_dryrun_1107/`) の種別ファイルは 1,576 件だけで、深刻さ C の学習 604 件のうち 514 件を
  覆っていなかった。`measure` の severity (0.912 / 0.670 / 0.8125) が現行 (0.9158 / 0.686 / 0.831) を下回ったのはこのため
  (同じ S でも種別が空になって分布が変わる)。`kinds_missing` で未測定に倒すようにした。
- 判断が要る点: (a) 推論側の `article_kinds` / `article_severity_axes` の混在 (§1) を切替時に直近の窓だけ新 S で
  付け直すか (本番の表への書き込み = 切替の操作の一部、利用者の許可が要る) (b) 深刻さ C の同じ S の再試行の揺れ
  (最大 0.30) の大きさを 604 件で測るか (GPU 5,436 回 ≒ 対照腕。測らないと新 S の差が揺れの範囲か言えない)。
  現行の S の `probe_<S>.jsonl` は学習に使った 10-28 のファイルそのもので、`relabel --s <現行 S> --what probes` は
  全件済みとして何もしない。同じ S の再試行は `--out-dir` で別の場所に出す。

## 7. 記録と宿題

- 切替の記録: 割当の版番号・5 本の JSON の sha・退避のパス・付け直しの jsonl のパスを CLAUDE.md §2 の「現在の割当」に追記する。
- 宿題: (a) pair の学習スクリプトを `scripts/` に置く (b) (済) `kinds_model` を足した
  (c) alert_dedup は pair 学び直しの後に p_pair の分布を確認する。
- 最初の使い手: SW-S26 (S26-ML-RETRAIN が detect・severity_s3、pair・deep_dive・relation は判定して必要なら)。
