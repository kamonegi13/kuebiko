# GEO-RESCUE-JUDGED: 地政学の救済を意味の判定 2 問で絞る (flat → pir → Q1 ∧ Q2) の事前登録

- 作成: 2026-10-30。**この文書のコミットが標本の抽出より前**。作成時点で新しい標本・入力・正解・
  判定の出力は 1 件も存在しない
- 対象のコード: branch `geo-rescue-judged` commit `2538fb85` (親 `f3e54bfa` = geo-rescue-narrow)
  - `src/pipeline/geo_rescue_judge.py`: 意味の判定 2 問 (logprob、`decision_probe.probe_choice`)
  - `src/pipeline/filters.py`: `GEO_RESCUE_PROMPT=judged` (既定は `flat` のまま = 本番の挙動は不変)
  - `src/tools/model_tiers.py`: `Step.GEO_RESCUE_JUDGE` (`STEP_INHERITS` で `step:triage_geo_rescue` を継承)
  - `scripts/measure_geo_rescue_prompt.py`: 1 回の実行で flat / pir / Q1・Q2 の P(Yes) / 最終判定を id ごとに出す
- 前の測定:
  - GEO-RESCUE-MEASURE (`docs/replay_prereg/geo_rescue_measure.md`): pir 単独は不合格
    (正解 yes 10/10 を救うが、正解 no 32/49 = 65% を誤って救う)
  - GEO-RESCUE-NARROW (探索のみ、未登録): 決定論の条件 (国・戦域・SIR の語・購読ソースの分類) では
    yes と no を分けられなかった。購読ソースの分類で絞る案も誤救済の予想 39% で線 (≤ 20%) を大きく超える。
    残った 32 件の誤救済は (i) 新しい事実の無い論評・発言 と (ii) 注視国の関わらない軍事・防衛の話
    (他国の基地・部隊、他国の装備の整備、遠い地域の紛争) にほぼ半々に分かれた。本設計の Q1 / Q2 はそれぞれに当てる

## 1. 設計 (変えない)

```
judged_rescued = flat_rescued OR (pir_rescued AND Q1 AND Q2)
```

- 救済 = triage の判定が medium 以上 (high でも medium に抑える、従来どおり)。flat / pir は同じ救済モデル・think=False
- Q1・Q2 は救済と同じモデル (`Step.GEO_RESCUE_JUDGE` → `step:triage_geo_rescue` の割当 = `kuebiko-sft:26b`、
  実行時にスクリプトが両方の解決先を照合する) で、生成せず 1 回の前向き計算で P(Yes) を読む
- 選択肢は `("Yes", "No")`、**しきい値は両問とも固定の 0.5** (P(Yes) ≥ 0.5 で Yes)。`config/models/probe_thresholds.json`
  は較正済みの判定の表で、この判定は較正していないため表には入れない。**新しい標本でしきい値を調整しない**
- 判定が読めない (`None` = 通信失敗・選択肢が見つからない) 問いは No 扱い (救わない = flat の判定に戻る側)
- 本番は pir が救った記事にだけ 2 問を当てる。測定は全件に当てる (分析のため)。同じ入力・同じモデル・温度 0 なので
  pir が救った記事の判定は同じ
- 購読ソースの分類 (folder ≠ thinktank) の条件は使わない (判定が置き換える)

### 1.1 判定の入力と問い (原文)

入力は `タイトル: {題}\n\n本文:\n{triage と同じ本文プレビュー}\n\n{問い}` (`judge_prompt`)。本文プレビューは
`ArticleTriage._triage_content` = `body_text` (無ければ RSS 概要) を HTML 除去して冒頭 1,500 字。
`decision_probe` が末尾に `1語だけで答えてください (Yes または No)。` を足す。

- **Q1** (`Q_NEW_FACTS`):
  > 質問: この記事は、新しい事実、または行為者 (国家・政府・軍・組織・指導者) の行動の報告を含むか。記事の種類 (ニュース・分析・論説) ではなく中身で判断する。分析記事でも、現在の出来事について新しい事実や評価を加えていれば Yes と答える。新しい事実の無い論評・一般論・発言の紹介だけなら No と答える。
- **Q2** (`Q_WATCHED_STATE`):
  > 質問: この記事が報じているのは、注視国 (中国・ロシア・北朝鮮・イラン) の行動か、または日本や同盟国・同志国が注視国に向けて行った決定 (支援・制裁・戦略・演習など) か。注視国が関わらない軍事・防衛・紛争・内政の話なら No と答える。

(書式の正は `judge_prompt`)

## 2. 母集団

- 表 `triage_rejections`、`'2026-10-12' <= ts < '2026-10-30T00:00:00'`、`importance = 'low'`、`reason` が
  `src/pipeline/filters.py:_GEO_REASON` に当たる行
- 除外: (1) GEO-RESCUE-MEASURE の標本 196 id (`data/eval/geo_rescue_measure_2026_10_30/sample.jsonl`)、
  (2) 同じ article_id が 2 行以上ある id、(3) 保持の確認の集合 (§6) の id
- 作成時点の件数 (件数のみ確認、標本は抜いていない): 1,179 行 / 1,177 id − 196 − 重複 2 − 保持の集合と重なる 1 = **978 id**
- 既知の偏り (生存者の偏り): 本番の flat 救済をくぐって low のまま残った記事だけが表に入る。flat の replay の
  救済率はこの母集団ではほぼ 0 になる。flat が救った記事の保持は §6 で別に測る

## 3. 標本

- 978 id を `triage_rejections.id` の昇順に並べ、`random.Random(20261105).shuffle(order)`
- その順に入力 (§4) を取り、**入力の取れた記事を先頭から 300 件**集める (主標本)。入力の取れない記事は飛ばし、
  理由 (`no_input` / `body_short_no_summary`) とホストの区分を `skipped.jsonl` に残す (件数を報告)
- 追加の決まり (判定の出力ではなく**正解の件数だけ**で決める、1 回だけ): 主標本の正解の yes < 30 なら、主標本の続きから
  入力の取れた記事を 150 件足し、その 150 件にも同じ手順で正解を付ける。追加後も yes < 30 なら無効 (§5)
- 層別はしない。抽出の実装は `data/eval/geo_rescue_judged_2026_10_30/draw_sample.py` (gitignore、sha256 を manifest に記録)

## 4. 入力 (本文)

- 本番の triage は thin feed で本文を先行抽出し `body_text` の冒頭 1,500 字で判定する。落選した記事は本文が
  保存されていないため、標本の url から取り直す: `ContentExtractor.extract(url)` (本番の `_prefetch_thin_bodies`
  と同じ経路・UA)、同時 3 件・1 件ごとに 1 秒の間隔。`check_extracted_identity` は記録のみ (棄却しない、本番と同じ)
- **敵性ホストは本文を取りに行かない**: host が `.cn` / `.ru` / `.su`、または `news-pravda.com` / `sputnikglobe.com` /
  `sputniknews.com` / `rt.com` / `rt-english.com` / `globaltimes.cn` (の下位ドメイン) の記事は RSS 概要だけを使う
- RSS 概要: 母集団の feed_url を 1 feed 1 回だけ取り直し (`direct_rss_source._fetch_one_feed`、本番の収集と同じ経路)、
  記事 id (`rss:<entry id>`) で一致した entry の概要
- 入力の優先: 本文 (200 字以上) → RSS 概要 → どちらも無ければ標本に入れない (§3 で飛ばす)。行ごとに入力の種類
  (`body` / `summary`) とホストの区分 (`hostile` / `other`) を残し、指標を区分別にも報告する (判定には使わない)
- 敵性ホストの記事は RSS が新しい entry しか持たないため多くが飛ばされる (標本で過少)。報告で件数を明記する
- 本文・概要は `data/eval/geo_rescue_judged_2026_10_30/` (gitignore) にだけ置く。コミット・ログに出さない

## 5. 正解 (Opus、盲検)

- 付け手: claude-opus-5-5 (この文書と同じエージェント)。**flat / pir / Q1 / Q2 の出力が 1 件も存在しない時点で**
  付け、`gold.jsonl` の sha256 を manifest に記録してから判定を流す
- 見せるもの: 伏せ字の連番、題、§4 の入力の冒頭 1,500 字 (モデルと同じ入力)。並び順は `Random(20261106)`。
  **見せないもの**: 購読ソースの名前・folder・url・ホストの区分、triage の理由、前の測定の正解と判定、探索の結果
- **rescue = yes** (いずれか):
  - (A) 注視国 (CN/RU/KP/IR) の安全保障に関わる行動・決定・合意: 軍事行動・演習、制裁、武器・技術の移転、影響工作、
    注視国どうしの連携、日本・同盟国・台湾への圧力。首脳外交もそうした中身を持てば (どの国でも) 数える
  - (B) 注視国の明示的な威嚇・最後通告・報復の予告 (発言だけでも)
  - (C) 日本・同盟国の、注視国に向けた決定: 支援・制裁・戦略・演習
  - (D) サイバーと地政学の連関: 国家のサイバー戦略、影響工作、重要インフラへの物理・ハイブリッドの脅威
  - 分析記事も、そうした現在の出来事に新しい事実・評価を加えていれば yes
- **rescue = no**: 発言・評価・非難だけ (B を除く) / 経済・貿易・文化の外交 / 新しい事実の無い論評 /
  注視国の関わらない軍事・防衛産業・基地・部隊の話 / 注視国の関わらない遠い紛争・内政
- 各行に理由 1 文と `confident` (true/false)。主指標は全件 (confident を問わない)、confident だけの値は報告のみ
- **κ**: 付け終えた後、**別の文脈の Opus** (正解を見ていない新しいエージェント) が `Random(20261107)` で選んだ 30 件を
  同じ盲検の入力・同じ定義で付け直し、一致率と Cohen の κ を報告する (線には使わない。一致率 < 80% なら
  報告に「正解の不安定」と明記)
- **`gold:geo_rescue_opus60` (GEO-RESCUE-MEASURE の正解 60 件) は探索に使ったため汚染されている。この判定には使わない**
  (その 196 id は母集団から除いてある)

## 6. 保持の確認 (flat が本番で救った記事を失わないか)

- 集合 R: 2026-10-12 以降に本番の flat 救済が救った記事 151 件
  (`data/eval/geo_rescue_narrow_2026_10_30/retention_set_reconstructed.jsonl`、run_logs の `triage_decision` と
  `triage_shadow` の突き合わせで復元した id のみ。復元の手順と漏れ (12 回分) は GEO-RESCUE-NARROW の下書き §6 のとおり)
- 入力: `articles.body` (同じ article_id の最初の行、題は `original_title` 無ければ `title`)。本文 200 字未満の記事は
  R から外して件数を報告 (作成時点で 142 件が 200 字以上)
- R の全件に flat / pir / Q1 / Q2 を当て、保持率 = `judged_rescued` の件数 / |R| (本文のある記事)
- 線: ≥ 90% (§7 の 5)。flat 単独の再救済率も報告する (judged ⊇ flat なので、欠けは replay の再現性 = 入力の差
  (本番は概要で救った記事もある) と非決定性の大きさ)

## 7. 合否の線 (結果を見る前に固定、前の測定と同じ)

主標本 (+追加) の正解のある行で、**すべて**を満たせば合格:

1. 回収: `recall_judged ≥ 0.70` かつ `recall_judged ≥ recall_pir − 20 pt` (正解 yes の行での救済率)
2. 主検定: 正解 yes の行で judged と flat の救済の不一致に McNemar exact 両側 `p < 0.05`、かつ judged だけが
   救った件数 > flat だけが救った件数
3. 誤救済: judged の誤救済率 (正解 no の行での救済率) `≤ 20%`
4. 総量: judged の標本全体の救済率 `≤ 50%`
5. 保持: §6 の保持率 `≥ 90%`

**無効** (判定しない): flat・pir の判定の失敗 (`error`) か判定の読めない (`judge_failed`) がどれか 1 つでも標本の 5% 超 /
追加後も正解 yes < 30 / 正解 no < 100 / R の本文のある記事 < 100 / 正解のある標本の id が判定の出力に欠ける /
manifest の sha256 (標本・入力・正解・この文書) が判定の前後で変わる / 救済か判定のモデルが `kuebiko-sft:26b` でない
(スクリプトが止める) / 本番コンテナの中で回した。

**報告のみ** (線に使わない): flat 単独・pir 単独・pir ∧ Q1・pir ∧ Q2 の同じ指標、Wilson 95% 区間、入力別・
ホストの区分別の値、confident だけの値、本番での判定の呼び出し数 (= flat が救わず pir が救った件数)、飛ばした記事の内訳。

集計は `data/eval/geo_rescue_judged_2026_10_30/analyze.py` (decisions.jsonl・gold.jsonl・sample.jsonl から verdict を出す)。

## 8. 汚染の鍵

- 新しい標本は前の測定の 196 id と保持の集合に重ならない (除外で保証)。GEO-RESCUE-NARROW の探索はその 196 件だけを見た
- 問い・しきい値・式・線は探索の標本 (196 件) の結果を見て設計したが、**新しい標本では一切変えない**。変えるなら
  この文書の変更履歴に書き、標本の抽出より前であること
- 正解の付け手は問いの文言を書いた者と同じ (同じエージェント)。盲検 (モデルの出力が存在しない時点・ソースを伏せる) と
  別の文脈の κ で抑える。限界として報告に書く

## 9. 実行の注意

- GPU を使う。本番稼働中・本番コンテナの中では流さない (CLAUDE.md §7)。使い捨てコンテナで、本番のジョブを止めて流す
  (手順は `run_cmd.txt`)。DB は読み取りのみ (スクリプトの既定 `PGOPTIONS=-c default_transaction_read_only=on`、
  config_store の DDL を飛ばして DB の割当を読む)。`LLM_USAGE_LOCAL=0`
- 判定の出力 (`decisions.jsonl`) は id・判定・確率だけ (本文を含まない)

## 10. 変更履歴

- 2026-10-30 初版 (標本・入力・正解・判定はいずれも存在しない)
