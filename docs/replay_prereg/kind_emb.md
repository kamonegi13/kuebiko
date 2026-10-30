# RP-5-1-GOLD / RP-5-1: 種別 (event_kind) の判定集と埋込の分類器の事前登録

- 作成: 2026-10-31 (JST)。**この文書のコミットが判定集の抜き出し・Opus のラベルより前**。
  作成時点で判定集 (`gold:event_kind_fresh`) の記事は抜いておらず、ラベルは 1 件も無い
- 本書は 2 つの項目が使う: **RP-5-1-GOLD** (判定集を作る、§1〜§8 はここで固定) と **RP-5-1**
  (E の分類器を判定する。§10 に台帳の判定の設計を写す。RP-5-1 は E を判定集に当てる前に §10 を詳しくし、
  変更履歴に残してコミットする)
- 著者が見たもの: `article_kinds` の model × kind の件数、10-01 以降の S の付与の種別ごとの件数、
  `data/eval/kind_roundup_2026_10_29.md`

## 1. 仮説 (RP-5-1-GOLD)

判定集を作るだけで仮説は無い。目的: 10-01 以降の記事で、S の付与と E の分類器の種別を、どちらの出力も
見ない Opus の正解で比べられるようにする。roundup の回収 ≥ 95% を Wilson の下限で見るため roundup を厚く取る。

## 2. 母集団

- 記事: 事象時刻 (`EVENT_TS_EXPR`) が 2026-10-01 00:00 JST 以上・2026-10-31 00:00 JST 未満、
  `article_kinds` の `model` が S 系 (`kuebiko-sft:s` で始まる) の行がある記事。同じ article_id は最新の行に畳む
- 層 = S の付与の種別 (advisory / breach / exploitation / stats / roundup / other)
- 除外して数える:
  1. `gold:event_kind_455` (`data/eval/event_kind.json`) の記事と、その記事と事象の根を共有する記事
  2. `kind_roundup_src.patch` の調整が読んだ記事 (`data/eval/kind_roundup_2026_10_29/candidates.json` の全記事)
  3. 種別の教師 (`data/mlx/teacher/event_kind.jsonl`・`event_kind_kind_v2.jsonl`) の記事 (S の付与を学ばせた記事)
  4. R18 (§7) で 1〜3 と同じ閉包に入る記事
  5. 本文が空の記事 (Opus に渡す入力が作れない)
- `roundup_guard` などモデル不明の行は母集団に入れない (数を報告)

## 3. 期間と分割

- 判定集の期間は 10-01〜10-30 (事象時刻)。学習・調整に使った集合 (§2 の 1〜3) は R18 の閉包ごと外す
- E の分類器 (KIND-EMB) の学習は `article_kinds` から判定集の記事 id と事象の根を除く (RP-5-1 の汚染の鍵、台帳)

## 4. 標本と重み

- 計 100 件: **roundup 40**、他の 5 層は各 12 (advisory・breach・exploitation・stats・other)
- 抜き方: 層ごとに適格な記事を article_id の昇順に並べ `random.Random(20261031)` で shuffle し、
  **1 つの R18 の成分から 1 記事まで**で先頭から取る
- roundup の適格な記事が 40 に足りないとき: S の付与が roundup でない記事のうち
  `src/cti/roundup_detector.roundup_basis(見出し)` が `title` の記事を、同じ手順で別の層 `roundup_title` として
  足りない数だけ取る (決定論)。足りても使わない
- 重み (RP-5-1 用): 層の適格な数 / 抜いた数。manifest に層ごとの母集団と抜いた数を書く

## 5. ラベル (Opus 盲検、1 件 1 回、計 100 回)

- 入力は**見出し・本文の冒頭 1,200 字・出典 (媒体名)** だけ。S の付与・E の出力・層・roundup_detector の判定は
  見せない。記事の順は乱数で入れ替え、1 回に 25 件まで
- 種別の定義は 10-29 の束の定義 (`data/eval/kind_roundup_src.patch` の SYSTEM の 6 種別と境界の行、逐語):
  - advisory: ベンダ/当局の勧告・修正の告知 (1 社の 1 回の告知に CVE が複数載るもの、1 社の製品だけの KEV 追加を含む)
  - exploitation: 特定の脆弱性が悪用されている・攻撃が進行しているという報告
  - breach: 特定組織の被害・侵害・流出の公表や掲載
  - stats: 露出台数・集計・調査統計の報告
  - roundup: 別々の事案・別々の製品やベンダーを並べた記事 (週報・日報・ダイジェスト・ニュースレター、
    複数ベンダーの KEV 追加、月例の更新の一覧)
  - 1 社の 1 回の告知・リリースに CVE が数件載るだけの記事、1 つの事案に複数の脆弱性が絡む記事、
    同じ事案の続報は roundup にしない (1 社の月例の更新でも別々の製品の修正を多数並べたものは roundup)
  - other: 上のどれでもない
- 出力: 種別 1 つ + 確信 (high / low) + 理由 1 文。判定集には種別と確信だけを残す (理由は別ファイル、data/ の中)

## 6. 合否の線

- RP-5-1-GOLD は判定集を作るだけで合否は無い。**作成の完了条件**: 100 件すべてにラベル、層ごとの母集団・抜いた数・
  除外の数を manifest に記録、判定集の sha256 を記録
- RP-5-1 の線は §10

## 7. 汚染の鍵 (R18)

- README の閉包 (最新の事象の根 ∪ CVE・被害組織の共有 ∪ 要約埋込 cos ≥ 0.80、`scripts/replay/_closure.py`)。
  対象 = 母集団の記事 ∪ §2 の 1〜3 の記事。ハブの切り方: CVE・被害組織が `relation_features.FOCUS_CVE_MAX` を
  超える記事はその欄を鍵にしない。`max_component_share = 0.30`

## 8. 判定集の同一性

| id | path | sha256 | 行 | 作成 |
|---|---|---|---|---|
| `gold:event_kind_fresh` | `data/eval/rp5_1_gold_2026_10_31/gold.jsonl` | 作成時に manifest | 100 | RP-5-1-GOLD (Opus) |
| 判定に使わない `gold:event_kind_455` | `data/eval/event_kind.json` | — | 455 | tainted |

## 9. 変更履歴

(結果を見る前の変更も、ここに日時・項目・前後・理由を書く)

## 10. RP-5-1 の判定 (台帳の stats の写し。RP-5-1 が E を当てる前に詳しくする)

- 重み: 層の抜き出し率の逆数で母集団の正解率。種別ごとの macro-F1・roundup の回収は層の中の数で重みなし
- 対の検定: 同じ記事で E の分類器と `article_kinds` の S の付与の正誤の不一致に McNemar の正確検定。区間は事象の根の
  cluster bootstrap
- 合否に入る量: E の正解率 (重みつき)・roundup の回収 ≥ 95% (Wilson の下限を併記)
