# ORG-D3-JUDGE: Opus が 1 組ずつ確かめた組だけで作る別名の辞書 D3 の判定 (事前登録)

- 下書き: 2026-11-10 22:0x JST。**この文書のコミットが、D3 の実装・Opus の確認 (新しい呼び出し)・D3 の作成・
  veto の再現・Fable の判定より前**
- 前提: ORG-D-JUDGE (`org_alias_d.md`、0c5825ae) は L1 0.817 で不合格、ORG-D2-JUDGE (`org_alias_d2.md`、682fbd9e) は
  L1 0.944・L2 大敗で不合格。上限は影の判定役 (gemma4:26b の logprob、`org_identity_pairs`) の 1 組ずつの精度
  (直接の辺 0.91〜0.94) と結論した。利用者の決定 (11-10): **辞書の組を Opus で 1 組ずつ確かめ、確かめた組だけを辞書に入れる。
  測定は別の付け手 Fable 5.2 で行い、辞書を作る付け手と測る付け手を分ける**
- 本書を書く前に見たもの: 前回・前々回の result.json の集計 (層ごとの same / different / unclear の件数) と、
  T の材料の構造 (C_P = 175 組、P の成分の大きさ 2:77・3:12・4:2・5:2・6:2、伏字のキーを含む組 6、
  汚染の組 9、前回・前々回で Opus の判定のある組 166 = C_P から汚染の 9 を除いた全部)。
  **組ごとの Opus の判定 (どの組が same か) は見ていない**。Fable の判定は 1 件も無い
- 測定は本番の DB に読むだけ (read-only セッション、ホストの .venv)。Ollama を使わない (Opus・Fable だけ)。学習・調整を伴わない

## 1. 仮説

D3 (Opus が same とした組だけから作る写像) は、P (影の対を連結した写像、本番の既定) と比べて、
独立の付け手 (Fable) の目で (i) 同一とする組の適合率が十分に高く (ii) P より悪くなく
(iii) 統合の焦点の関門の拒否を目立って増やさない。向き = 非劣性。

## 2. 母集団と Opus の確認 (辞書を作る段)

- T・材料・P(T) は前回と同じ (`org_alias_d.md` §2): **T = 2026-11-10T00:00:00+09:00**、材料 = `org_identity_pairs` の
  `form = 'and_event12_org-4_v2'`・`logit >= 0`・`judged_at < T`、P(T) = `build_alias_map(材料).mapping`
- **母集団 U = C_P (P(T) の閉包の組) すべて** (175 組の見込み)。標本を抜かない。汚染の組・前回の標本を外さない
  (汚染の組を外さない理由: 前回・前々回が外したのは「影の判定役の問いを選ぶのに使った組」だからで、今回の辞書を作る
  Opus と測る Fable はその組で何も調整していない。感度として汚染の組を除いた L1 も報告する。線には使わない)
- **付け手**: claude-opus-5-5 (bridge 127.0.0.1:8010、effort high)。rubric・問い・見せるもの・見せないものは前回と同じ
  (`org_alias_d.md` §4、rubric の版 `org-alias-d-v1`、`scripts/replay/org_alias_d_judge.py` の `RUBRIC` / `QUESTION` /
  `_name_index`)。問いの文は変えない
- **再利用**: 前回・前々回の Opus の判定は裁定のキャッシュ (`data/eval/adjudication_cache.jsonl`、キー = 問い・入力・rubric・
  付け手・effort) に在る。同じ T の同じ入力を作り直し、キャッシュに当たった組はその判定を再利用する (再判定しない)。
  当たらなかった組 (汚染の 9 組の見込み。入力が変わった組があればそれも) だけを新しく呼ぶ。組ごとに
  `source` (`reused` / `fresh`) を記録し、件数を報告する
- 1 組 1 回 (失敗のときだけ 1 回まで試し直す)、再採点なし。**上限: Opus の呼び出し 200 回** (かつ ⌈1.1 × 新しく呼ぶ組⌉ + 2)
- 出力 `verified.jsonl` = 組の id・判定・確信度・source だけ (名前・キーを書かない)。sha256 を manifest に書く

## 3. D3 の作り方 (固定)

1. **辺** = U のうち Opus が `same` とした組 (確信度は問わない)。`different` / `unclear` / 未判定の組は辺にしない。
   伏字のキー (`org_alias_d.md` §2 の定義、`created_at < T`) を含む組も辺にしない (本番の伏字の関門と揃える)
2. **推移の矛盾の扱い**: 写像 (照合キー → 代表キー) は推移的な関係しか表せない。Opus は組を判定するので連結はしない。
   辺の連結成分ごとに、成分を「中のすべての組が辺である」群 (辺の上の完全部分グラフ) に分け、**覆う辺の数が最大**の
   分け方を選ぶ (D2 の §2 の 2 と同じ関数・同じ決定論の探索順。成分のキーが 12 を超えたら同じ貪欲)。
   すなわち A=B・B=C・A≠C (または A–C が unclear) の成分は、1 つの辺だけを残す分け方になる (同点は探索順で最初のもの)。
   成分ごと捨てることはしない (完全部分グラフに限るので、残る群の中の組はすべて Opus が same とした組)
3. 大きさ 2 以上の群のキーだけを写像に入れ、群の最小キーを代表にする
- 構造上 C_D3 ⊆ U = C_P (群の中の組は辺 ⊆ U)。逆向きの組が出たら判定不能
- **pins** (ORG-D-SWITCH が D3 で出荷するときの値): `version` = `asof:2026-11-10T00:00:00+09:00;form:and_event12_org-4_v2;mode:d3;verified:<verified.jsonl の sha256>`、
  `rows` = D3 のキーの数、`hash` = 前回と同じ式 (`mapping_sha256`)。
  `scripts/build_org_alias_dict.py --as-of T --mode d3 --verified <verified.jsonl>` が同じ値を出す

## 4. Fable の判定 (測る段、盲検)

- **付け手**: claude-fable-5-2 (bridge 127.0.0.1:8010、effort high)。Opus と**同じ** rubric・問い・入力 (同じ `_name_index`)。
  見せないもの: Opus の判定・D3 / P のどちらが同一とするか・logit・層・本書・他の組
- **対象 = U すべて** (C_D3 と C_P \ C_D3 の両方。全数なので重みは 1)。順は `random.Random(20261114)` で並べ替える
- 1 組 1 回 (失敗のときだけ 1 回まで試し直す)、再採点なし。**上限: Fable の呼び出し 200 回**
- D3 の作成 (pins の確定) が Fable の最初の呼び出しより前。Fable の結果を見て D3・線・問いを変えない

## 5. 指標と合否の線 (結果を見る前に固定。L1・L3 の線は前回と同じ)

正解 = Fable の判定 (`same` → 同一が正しい / `different` → 同一は誤り / `unclear` は分母から外す)。

- **L1 (D3 の適合率)**: C_D3 の組の Fable の `same` ÷ (`same` + `different`) の点推定 ≥ 0.95 **かつ** D3 の群 (代表キー) を
  単位とするクラスタ bootstrap (2,000 回、`random.Random(20261115)`、百分位法) の 95% 区間の下限 ≥ 0.90。Wilson を併記
- **L2 (D3 は P より悪くない)**: 全数なので層 A (C_D3) で両者の正誤は同じ。不一致は C_P \ C_D3 だけに出る。
  b = C_P \ C_D3 で Fable が `same` (P 正・D3 誤)、c = C_P \ C_D3 で Fable が `different` (D3 正・P 誤)。
  McNemar の正確検定 (片側、D3 が悪い向き) で b > c かつ p < 0.05 なら不合格、かつ
  正答率の差 (D3 − P) = (c − b) ÷ (U の Fable が判定できた組) ≥ −0.02。両方を満たせば合格。
  参考 (線に使わない): 「P が同一とし Opus が `different` とした組」のうち Fable も `different` とした割合
- **L3**: 前回と同じ veto の両向きの再現 (`org_alias_d.md` §6、窓 [T − 14 日, T)) を P(T) → D3(T) で。
  「通る → 拒否」 ≤ max(2, ⌈0.02 × 窓の統合の判断の件数⌉)。「拒否 → 通る」は報告のみ (構造上 0 の見込み)
- **総合**: L1 ∧ L2 ∧ L3 で合格 (ORG-D-SWITCH を D3 で進める)。1 つでも満たさなければ不合格 (切替しない)
- **判定不能**: Fable が判定できた組 (`same` / `different`) が U の 80% 未満、C_D3 で Fable が判定できた組が 25 未満、
  U の `unclear` の割合が 0.20 超、Fable が上限で止まり未判定が U の 10% 超、Opus が上限で止まり未判定が U の 10% 超、
  C_D3 ⊄ C_P、のどれか
- **参考 (線に使わない)**: U で両者が判定した組の Opus と Fable の一致率と Cohen の κ (3 値、および same / not-same の 2 値)。
  汚染の組を除いた L1。Opus の `same` (低確信度) の件数

## 6. 調整と測定の分離・汚染の鍵

- 辞書を作る付け手 (Opus) と測る付け手 (Fable) は別のモデル。Fable の判定は D3 の作成・線に戻さない
- 判定集: `gold:org_alias_opus_verified_d3` = `data/eval/org_alias_d3_judge/verified.jsonl` (辞書の材料)、
  `gold:org_alias_fable_d3` = `data/eval/org_alias_d3_judge/fable_labels.jsonl` (測定)。path と sha256 を manifest と報告に書く
- manifest に `prereg_path`・`prereg_git_sha`・`prereg_blob_sha256`・`head_sha`・T・pins・verified の sha256・母集団の sha256 を書き、
  各段の実行前に `require_prereg` を通す
- 裁定のロック `data/eval/adjudication.lock` (待たずに失敗、塞がっていれば 5 分おきに取り直す) を取ってから呼び、
  `data/eval/adjudication_ledger.jsonl` に回数を記録する。Opus の行は `opus_calls_charged` (新しい呼び出し) と再利用の件数、
  Fable の行は `opus_calls_charged: 0`・`other_calls_charged` (fable_pilot の行の形)

## 7. 手順

1. 本書をコミット (prereg/1110)
2. feat/org-alias-asof に D3 の作り方 (`build_dict_mapping_d3`、`build_org_alias_dict.py --mode d3 --verified`) と
   判定の道具 (`scripts/replay/org_alias_d_judge.py` の `--variant d3`: 段 `confirm` (Opus) → `build` (D3・pins・veto) →
   `measure` (Fable・線)、出力 `data/eval/org_alias_d3_judge/`) をテストつきで足す
3. confirm → build → measure。`build_org_alias_dict.py --as-of T --mode d3 --verified …` の dry-run で pins が build と一致することを確かめる

## 8. 変更履歴

- 2026-11-10 下書き (D3 の実装・Opus の新しい呼び出し・D3・再現・Fable とも未実行)
