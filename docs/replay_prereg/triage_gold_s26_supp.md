# TRIAGE-GOLD-S26-SUPP: s26 の triage の判定集の補い (gold:triage_fresh_s26_supp) の事前登録

- 作成: 2026-11-01。**この文書のコミットが補いの記事の選び出しより前**
- 経緯: 本体 (`docs/replay_prereg/triage_gold_s26.md`、`gold:triage_fresh_s26`) の 150 件で、
  境界を除いた正解 high + medium が 24 件となり、本体 §6 の床 (40) を下回った。EVAL-S26 の重い取り違え
  (正解 high/medium を low に落とす率) を判定できる件数にするため、利用者の判断 (2026-11-01「補いの集合を足す」)
  で重要度の層の補いを足す
- 追加を決めるときに見たもの: 本体の正解の**分布だけ** (high 13 / medium 38 / low 99、境界 39)。
  s26 はまだ存在せず、モデルの判定は 1 件も見ていない。本体の 150 件・正解・線は変えない

## 1. 仮説

判定集を作る項目なので仮説は無い。合否の線は EVAL-S26 の prereg が持つ。

## 2. 母集団

- 本体 §2 と同じ定義・同じ窓 (事象時刻 2026-10-27 00:00 JST 以上、2026-11-01 00:00 JST 未満、`status='posted'`)、
  同じ除外 (入力が空・汚染 = 本体 §7 の閉包)。本体の選び出しと同じ手順で作った**適格 812 記事**から、
  本体の標本 150 記事を除いた残り (662 記事の見込み)
- 適格の集合は本体の `build.py` の手順を同じ HEAD 相当で再計算し、件数が本体の manifest
  (`eligible` 812) と一致することを確かめる。一致しなければ実行しない (報告して止まる)

## 3. 層

- 層 = 選び出しの時点で保存されている `articles.importance` (high / medium / low)。本番の triage と要約の後の値
- 層の値は Opus に見せない

## 4. 標本と重み

- 補い: 層 high から **40 件**、層 medium から **40 件** (計 80)。層 low からは取らない
- 抜き方: 層ごとに残りの適格な記事を `article_id` の昇順に並べ、`random.Random(20261103)` で
  (high → medium の順に、同じ乱数の列で) shuffle し、先頭から 40 件。層の残りが 40 未満なら全件
- 本体と補いを合わせた集合を、**層別の無作為標本**として扱う: 層 h の重み = 層 h の適格な記事の数 N_h /
  合わせた集合のうち層 h の記事の数 n_h。本体は単純無作為なので、層の中では本体 ∪ 補い も無作為の部分集合になる
- EVAL-S26 は、率を母集団 (適格 812) へ戻すときにこの重みを使う。層の N_h・n_h を manifest に書く
- 本体だけの率 (重みなし) も並べて報告できるように、各行に `origin` (base / supp) を残す

## 5. 正解 (Opus、盲検)

- 本体 §5 と同じ: 同じ裁定者、同じ rubric 名 (`triage-flat-gold-s26-<RUBRIC_VERSION>`)、同じ prompt と入力
  (本体の実行の記録どおり、保存された本文から `strip_html(body)[:1500]`)。見せないもの・出力の欄も同じ
- 1 記事 1 回 (Opus 80 回)。失敗は 1 回だけ再試行し、なお失敗なら欠けとして報告 (別の記事で埋めない)
- 裁定は `data/eval/adjudication_ledger.jsonl` に記録

## 6. 床の確認

- 本体 ∪ 補い で、境界を除いた正解 high + medium の件数を数えて報告する。40 未満なら EVAL-S26 の prereg で
  判定不能の扱いを決める (この項目では標本を足し直さない)

## 7. 汚染の鍵 (R18)

- 本体 §7 と同じ閉包 (本体の manifest の版) を使う。補いの記事は本体の適格の集合から取るので、追加の除外は無い
- S26-TEACHER の guard には、本体と補いの両方の記事の閉包を足す

## 8. 判定集の同一性

| id | path | sha256 | 行 | 作成 |
|---|---|---|---|---|
| `gold:triage_fresh_s26_supp` | `data/eval/triage_fresh_s26/supp_gold.jsonl` | (作成時に manifest と台帳へ) | ≤80 | TRIAGE-GOLD-S26-SUPP |

- 本体 `gold:triage_fresh_s26` (`data/eval/triage_fresh_s26/gold.jsonl`、sha256 `e5446d60eb32f365bfafeeef2dcda20df0baffbe04835bf40333ee1bd77737d4`) は変えない

## 9. 実行の前提

- スクリプトは `require_prereg("triage_gold_s26_supp", …)` を通す。実行は使い捨てのコンテナ、本番の表に書かない
- manifest (`data/eval/triage_fresh_s26/supp_manifest.json`) に: prereg の sha・HEAD・適格の件数 (812 との一致)・
  層ごとの N_h・本体の層ごとの件数・補いの件数・乱数種・標本と正解の sha256・正解の分布・本体 ∪ 補い の床の件数

## 10. 変更履歴

- 2026-11-01: 作成
- 2026-11-01 (実行の記録、線・母集団・標本の変更ではない): 適格の再計算は 812 で本体と一致、本体の 150 も乱数種 20261102 で再現。層 N_h = high 37 / medium 493 / low 282、本体の層の件数 7 / 91 / 52。層 high の残りが 30 で 40 に届かず §4 の決まりどおり全件 → 補いは 70 (high 30・medium 40)。正解 (補い): high 23 / medium 32 / low 15、境界 30。本体 ∪ 補い の境界を除く high + medium = 51 (床 40 を超えた)。出力の検査 (`require_prereg` の out_dir) は本体の出力が本書より前のため外した (本体の出力は変えていない、sha256 は不変)
