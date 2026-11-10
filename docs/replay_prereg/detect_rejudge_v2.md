# DETECT-REJUDGE-2: detect の分けた形 + 書き手の直し + 回収率の 4 旗の再判定の事前登録 (新しい窓)

- 作成: 2026-11-10 21:3x JST (台帳の項目 DETECT-REJUDGE-2、利用者 11-10「GPU は今から止めてやっていいです」)。
  **この文書のコミットが、入力の凍結・閉包の計算・2 腕の実行・Opus の裁定のどれよりも前**。
  作成時点で本書の窓 (11-04〜11-10) の両腕の出力・開設・裁定は 1 件も存在しない
- 対象の判断: SW-DETECT (延期中) を再開するか = 「分けた形 (`DETECT_DECOMPOSE=1`) + 書き手の直し
  (`DETECT_WRITER_GATES=1`) + 回収率の 4 旗 (=1) + 書き手 s24ci65」の束を出せるか。束の一部だけの出荷は本書の合格の範囲外
- 前例: `docs/replay_prereg/detect_decompose_rejudge.md` (以下「前回の prereg」、8f727d4e)。**本書に書かない事柄は前回の prereg の
  とおり** (節番号は前回の prereg に揃える)。変えた所だけを本書に書く
- 判定集の id: `gold:detect_rejudge_v2` (§8)
- 道具: ブランチ `feat/detect-decompose-recall` の c211f3de (main 8968701b の上に rebase、`DETECT_GPU_PROFILE=rejudge2`)。
  実行は本書のコミットを取り込んだ同じ道具の木で行う

## 0. 作成時に見たもの (正直に書く)

本書は**前回の再判定が不合格だった後に、その結果と取りこぼしの分解を見てから**書いた。著者が見たもの:

1. 前回の prereg と前回の結果 (`data/eval/detect_rejudge/judge/summary.json`: P1 −0.24 [−0.83, +0.39]・P2 −1.0 点 [−8.6, +6.5]、
   P3〜P5 合格、開設 251/358)、台帳 SW-DETECT の diagnosis_20261110 (取りこぼし 54 の内訳)。4 旗はこの分解を見て作られた
   (09-24〜10-26 の凍結入力と前回の 2 腕の出力が調整に使われた。§7.1 でそれらを調整側に入れる)
2. 4 旗のコード (`src/assessment/claim_salvage.py`・`detect_merge_gate.py`・`detect_recall_flags.py`、`detect_decompose.py` の変更、
   `prompts/synthesis/detect_cluster_claim.j2` の input_v2 の枝)
3. DB の読み取り (出力・正誤を含まない構造の数): 本番の detect ML の sha256、model_tiers の現在値 (fast/reasoning = s24ci65、
   `step:synthesis_detect`・`step:detect_claim_write` = s23i)、`org_identity_pairs` 13,582 行・`org_alias_dict` 0 行、
   本番の `EVENTNEWS_ORG_ALIAS_SOURCE=pairs`
4. **本書の窓の記事・候補数・適格な区間の数は見ていない** (凍結の時点で初めて数える)

合否の線 P1〜P5 は前回の prereg の値をそのまま使う。

## 1. 仮説

前回と同じ (非劣性)。出荷腕は現行腕と比べて (a) 窓あたりの C − W が実質的に下がらず、(b) 誤った開設の率が上がらない。

## 2. 母集団

### 2.1 窓

- 窓 = UTC の 6 時間の区間。範囲は事象時刻 **`[2026-11-04T12:00Z, 2026-11-10T06:00Z)` の 23 区間**
  (= 11-04 21:00 JST 〜 11-10 15:00 JST)
  - 始まり: detect_decompose (DETECT-JUDGE) の窓 `[10-27T00Z, 11-04T00Z)` (書き手の直しの調整に使用) との間に 12 時間の空き (前回の prereg と同じ型)
  - 終わり: 作成時刻 (11-10 12:3xZ) の約 6.5 時間前。直近の区間は取り込み・triage の遅れで記事が揃っていない恐れがあるため、
    閉じてから 6 時間以上たった区間までにした。P0-18 のデプロイ (11-10 21:19 JST = 12:19Z、`gold:n20_eval_inputs` の始まり) より前
- 窓の入力・as-of の台帳の再構成 (§2.3) は前回の prereg のとおり (`as_of_strict`)

### 2.2 窓の除外

前回の prereg の (1)〜(5) のとおり。

### 2.3 区間の開始時点の台帳

前回の prereg のとおり。**足すもの**: 被害組織の別名の写像 (`load_org_alias` が引く `org_identity_pairs` の同一の組と
`org_alias_dict`) を凍結の時点の値で `inputs.json` に残し、2 腕の実行で本番と同じ経路 (`EVENTNEWS_ORG_ALIAS_SOURCE`) で読ませる
(前回までは凍結の代役が別名を返せず、空の写像で照合していた。今回は合流の関門 `DETECT_MERGE_CONFLICT_GATE` が別名を使うため)。
区間の開始時点ではなく凍結の時点の値 (逸脱、manifest の deviations に残す)

## 3. 期間と事象時刻の分割

- 学習・調整に使った期間: 前回の prereg §3 に加え、**4 旗の調整 = 前回の再判定の窓 `[09-24T00Z, 10-26T12Z)` の凍結入力と 2 腕の出力**。
  本書の窓はどれとも事象時刻で重ならない
- 本書の中で閾値・プロンプト・モデル・関門・旗を動かさない。動かす必要が出たら不合格扱いにして調整役へ戻す

## 4. 標本と重み

前回の prereg §4 と同じ方法で、**乱数種を 20261110** に変える (窓の抽出・腕の順序 `Random(20261110 + 窓の通し番号 * 10 + run)`・
単位の抽出・2 回目の裁定・bootstrap のすべて)。

- **同じ UTC 日からの上限を 3 → 4** に変える (結果を見る前の決定。理由: 範囲が 23 区間・UTC 日は 7 つ (11-04 は 2 区間、
  11-10 は 1 区間) で、上限 3 では最大 2 + 5×3 + 1 = 18 窓にしかならず目標 20 に届かない。上限 4 なら最大 23)
- 目標 20 窓。適格な窓が 20 未満ならあるだけ使い、**18 未満なら判定不能** (前回と同じ)
- 各窓 × 各腕を 3 回 (120 単位)

## 5. 対の検定

### 5.1 両腕

| | 現行腕 | 出荷腕 |
|---|---|---|
| `DETECT_DECOMPOSE` | `shadow` | `1` |
| `DETECT_WRITER_GATES` | `0` | `1` |
| `DETECT_CLAIM_SALVAGE` | `0` | `1` |
| `DETECT_WRITER_INPUT_V2` | `0` | `1` |
| `DETECT_WRITER_CAP_REFILL` | `0` | `1` |
| `DETECT_MERGE_CONFLICT_GATE` | `0` | `1` |
| `step:synthesis_detect` / `step:detect_claim_write` | `kuebiko-sft:s23i` (本番の今の上書き) | S = 上書きを外した先 (`kuebiko-sft:s24ci65` であることを実行前に確かめる。違えば実行しない) |
| その他のモデル割当・旗 | 本番の値のまま (両腕で同一、manifest に版を残す) | 同左 |
| 採用する開設 | `variant=current` | `variant=decompose` |

- 4 旗の値は道具のプロファイル (`rejudge2`) に固定し、`DETECT_ARM_FLAGS` で渡す値が固定と食い違えば中止する
- 実行は使い捨てのコンテナ (`docker compose run --rm --no-deps -T`、image は本番の kuebiko:latest = release-1110)。
  `src/` の全体・`prompts/synthesis/detect_cluster_claim.j2`・`scripts/gpu_window/` を道具の木 (§0 の c211f3de + 本書) から読み取り専用で重ねる。
  本番の毎時の LLM ジョブを止めて走らせる。評価を本番コンテナの中で回さない
- その他は前回の prereg §5.1 のとおり (フォールバックは P4 で数え出荷腕の値に入れない)

### 5.2 裁定 / 5.3 指標と検定

前回の prereg §5.2・§5.3 のとおり (Q1〜Q3・盲検・単位の合成・Wilcoxon・符号検定・窓の cluster bootstrap 2,000 回)。
裁定者は `claude-opus-5-5` (bridge 経由)、rubric 名は `detect-rejudge-v2` / 2 回目 `detect-rejudge-v2-p2`。乱数種は 20261110。

## 6. 合否の線 (前回と同じ値。すべて満たして合格)

P1〜P5 と判定不能の条件は前回の prereg §6 のとおり:

- P1: d_w の平均の bootstrap 95% 区間の下限 ≥ −0.5、かつ Wilcoxon で出荷腕が有意に悪くない
- P2: 誤開設率の差 (出荷 − 現行) の 95% 区間の上限 ≤ +5 点
- P3: 出荷腕の裏付けの関門で止めた claim / (開いた + 止めた) ≤ 0.20 (分母 30 未満は判定不能)。忠実さで止めた数・削って救った数
  (`n_fidelity_blocked`・`n_salvaged`) は参考に出す (判定に使わない)
- P4: 分けた形が適用された (窓, run) の割合 ≥ 90%
- P5: 出荷腕の開設数の合計 / 現行腕 ≤ 1.5
- 判定不能: 窓 18 未満 / P3 の分母 30 未満 / P4 < 90% / 裁定できた単位 < 抽出の 80% / 閉包の段がどれも最大成分 < 0.30 を満たさない /
  R18 で除いた単位が U の 50% 超 / §9 の前提を満たさない。判定不能は合格ではない
- 1 つでも不合格なら不合格。本書の中で線・母集団を変えて再判定しない
- 参考 (判定に使わない): 前回との比較として、開設の比・窓平均の C・W、忠実さで止まった数・削って救った数を出す

## 7. 汚染の鍵 (R18)

### 7.1 学習・調整側の記事

前回の prereg §7.1 (学習側の 4 ファイル + 教師 + 10-27〜11-04 の判定の 2 腕の出力) に**足す**:

- 前回の再判定の 2 腕の出力 `data/eval/detect_rejudge/arms_20261110T044350Z.jsonl` が開いた全単位の記事 (両腕・全 run)
- 前回の再判定の凍結入力 `data/eval/detect_rejudge/inputs.json` の全窓の候補 (`llm_input_ids`) — 4 旗の調整の材料

### 7.2 閉包

前回の prereg §7.2 の段 L0〜L4 と選び方 (最初に最大成分 < 0.30 を満たした段) のまま。凍結の直後・2 腕の前に `closure_plan.json` を固定する。
候補の汚染が 50% を超えたら GPU 枠を使わずに調整役へ戻す。除いた単位は層ごとに数だけ報告し、裁定しない。

## 8. 判定集の同一性

| id | path | sha256 | 行 | 作成 |
|---|---|---|---|---|
| `gold:detect_rejudge_v2` | `data/eval/detect_rejudge_v2/judge/verdicts.jsonl` (+ `verdicts_pass2.jsonl`) | 裁定の完了時に manifest へ | 抽出した単位 (≤ 260) + 2 回目 40 | DETECT-REJUDGE-2 |

- 判定にしてはならない集合: 前回の prereg §8 のもの + 前回の再判定の裁定 `data/eval/detect_rejudge/judge/` (出力先が同じなら中止)
- manifest に残すもの: 前回の prereg §8 のとおり。コードの sha256 の対象に `claim_fidelity.py`・**`claim_salvage.py`・`detect_merge_gate.py`・
  `detect_recall_flags.py`** を含める (`CODE_FILES`、証明書 `prereg_attestation.json` と 2 腕の manifest の両方)

## 9. 実行の前提 (満たさなければ実行しない)

- 本書のコミットが、凍結・閉包・実行の HEAD の祖先 (`require_prereg("detect_rejudge_v2", …)`)
- `inputs.json` と `closure_plan.json` が 2 腕の実行より前に固定され、sha256 が manifest にある
- detect ML の sha256 = `9a0a644f0e8b6a52f3440da317dc06c59de8c0555b2a87996ed2869c075a8ff0` (前回と同じ版)
- 証明書のコードの sha256 = 使い捨てのコンテナの中の写し (src・プロンプト・道具を重ねた状態)。実行の直前に取り直す
- 本番の毎時の LLM ジョブが止まっていること (pstop)。直前の連続生成の再測定 (verify_sustained: ≥45 tok/s・AC・電池が減らない) に
  落ちたら枠を開かない
- 生成の締め切り 05:15 JST、本番の復帰の hard stop 05:45 JST (06:30 の朝のブリーフの前)。締め切りで単位が欠けたら、
  欠けた単位は ok でない run として扱い、P4 と窓の数 (§6) で判定不能になりうる (欠けを補わない)

## 10. 既知の偏り

前回の prereg §10 の 1・3・4 のとおり。2 (深刻度の軸の旧モデル) は本書の窓 (10-27 の切替より後) には当たらない見込み (窓ごとの付け直し済みの
割合を manifest に残す)。**足すもの**: 別名の写像は凍結の時点の値 (§2.3)。窓が直近なので、窓の後の記事が台帳の証拠・事象の根に入っている
割合が前回より小さい見込み (as-of の再構成はそれらを除く)。

## 11. 見積もり

- **Opus**: 1 回目 ≤ 260 + 2 回目 40 = 約 300 回。**上限は前回の実回数 300 の 1.2 倍 = 360 回** (キャッシュ当たりは数えない・失敗は数える)
- **GPU**: 120 単位。前回の実測 45 分 (11-10 13:43〜14:28)。書き手の上限の補充 (+3 回まで) と要約を足した入力で書き手の呼び出しが
  増えるため 1.5 倍を見込み約 70 分、打ち切りは締め切り 05:15

## 12. 変更履歴

- 2026-11-10: 初版 (凍結・閉包・2 腕・裁定のどれより前)
