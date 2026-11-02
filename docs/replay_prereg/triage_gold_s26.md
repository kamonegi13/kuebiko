# TRIAGE-GOLD-S26: s26 の triage の判定集 (gold:triage_fresh_s26) の事前登録

- 作成: 2026-11-01。**この文書のコミットが TRIAGE-GOLD-S26 の記事の選び出しより前**。作成時点で
  標本・Opus のラベルは 1 件も存在しない
- 対象の判断: EVAL-S26 の triage の行 (s26 の平たい triage の判定)。本書は**判定集を作るだけ**で、
  合否の線は EVAL-S26 の prereg が持つ
- 作成時に見たもの: 母集団の候補の件数と重要度の分布だけ (§2)。記事の中身・ラベルは見ていない

## 1. 仮説

判定集を作る項目なので検定する仮説は無い。目的は、s25 の調整に使った `gold:triage_flat_v1` (tainted) の
代わりに、s26 の学習・調整と記事・事象で交わらない新しい triage の正解を 150 件そろえること。

## 2. 母集団

- 表 `articles` (同じ `article_id` の複数行は `created_at` の最新の 1 行に畳む)
- 事象時刻 (`src/storage/event_time.py` の `EVENT_TS_EXPR`) が **2026-10-27 00:00 JST 以上、2026-11-01 00:00 JST 未満**
  (窓は作成時点で閉じている)
- 状態: `status = 'posted'` (分析専用 `analysis_only`・投稿直前の重複 `duplicate_of` を含む。
  本番の triage を通って保存された記事)
- 本文または RSS 概要があり、§5 の入力 (本文プレビュー) が空でない
- Grok の記事 (`grok:` で始まる id) も含める (本番では通常記事と同じく triage される)
- 除外して数える欄: (1) 汚染 (§7)、(2) 入力が空、(3) 同じ記事の重複行 (畳む)
- 作成時点の候補 (10-27 以降の `created_at`、事象時刻で切る前の目安): posted 1,254 記事
  (保存された importance は high 177 / medium 751 / low 328)

### 2.1 既知の偏り (生存者)

- triage が low にして落とした記事 (`triage_rejections`、10-27 以降 901 件) は本文が保存されず
  (本文つきは 1 件)、同じ入力で裁定できないので母集団に入らない。**この判定集は「本番の triage を
  通った記事」の分布**で、今の本番が誤って落とした記事は含まない
- そのため EVAL-S26 では、この集合で測れるのは (a) s26 が正解 high/medium を low に落とす重い取り違え、
  (b) 過大・過小の率 であり、「今の本番の取りこぼし」は測れない。EVAL-S26 の報告に明記する
- 窓の中でモデルの割当が変わっている (10-26 版 42 から fast = s24ci65)。保存された importance は使わない
  (Opus に見せない・層にしない)

## 3. 期間と事象時刻の分割

- s25 の調整に使った `gold:triage_flat_v1` は 10-08 の rubric で付けた goldset_v2 の記事で、窓が重ならない。
  事象でまたがる記事は §7 の閉包で外す
- s26 の教師づくり (S26-TEACHER、11-02 開始) は**この判定集の閉包を除外の鍵に足す** (逆向きの汚染を防ぐ)。
  標本の固定を S26-TEACHER の教師の選び出しより前に行い、manifest の sha256 を S26-TEACHER の guard に渡す

## 4. 標本と重み

- 単純無作為 150 件 (層なし・重みなし)。母集団の分布のまま測る
- 抽出: 適格な記事 (汚染を除いた後) を `article_id` の昇順に並べ、`random.Random(20261102)` で shuffle し、先頭から 150 件
- 適格が 150 未満なら全件 (件数を報告。窓は広げない)
- 選び出しは決定論。標本の `article_id`・`body_sha256` (本文は書かない)・入力プレビューの sha256 を
  `data/eval/triage_fresh_s26/sample.jsonl` (gitignore) に書き、manifest に sha256 を残す

## 5. 正解 (Opus、盲検)

- 裁定者: Opus (`data/mlx/adjudication_cache.py` の `adjudicate`、rubric 名 `triage-flat-gold-s26-<RUBRIC_VERSION>`)
- prompt: 本番の `src/tools/triage_flat_rubric.build_flat_triage_prompt(feed, title, body_preview)` に、
  C6 (`data/mlx/c6_flat_gold.py`) と同じ裁定者向けの追記 (平たい基準の文だけで判定・関連性を使わない・
  `criterion`・`boundary`・`reason`) を足したもの。作成時点の `RUBRIC_VERSION` = `2026-10-08.8-flat`。
  実行時の値を manifest に書き、違えば実行しない
- 入力: `feed` = 購読ソースの名前、`title`、`body_preview` = 本番と同じ作り方
  (`ArticleTriage._triage_content` と同じく、本文があれば本文・無ければ RSS 概要の HTML を剥がし、先頭 1,500 字)。
  保存された本文から作るので、取り込み時に triage が見た入力と違いうる (本文の再取得など)。
  違いは測れないので報告に注記する
- 見せないもの: 保存された importance・配信先・モデルの判定・他の記事
- 出力: `importance` (high / medium / low)、`criterion`、`boundary` (bool)、`reason` (監査用、判定には使わない)
- 1 記事 1 回 (Opus 150 回)。裁定は `data/eval/adjudication_ledger.jsonl` に記録 (R23)
- 失敗 (上限・拒否) の記事は 1 回だけ再試行し、なお失敗なら欠けとして件数を報告する (別の記事で埋めない)

## 6. 判定 (この項目では行わない)

- 合否の線・指標・検定は EVAL-S26 の prereg が決める。この判定集で閾値・プロンプトの調整をしてはならない
- 参考の床: 境界を除いた正解 high + medium が 40 件未満なら、EVAL-S26 の重い取り違えの率は区間が広すぎるため
  「判定不能」の扱いを EVAL-S26 の prereg に書くこと (本書は件数を報告するだけ)

## 7. 汚染の鍵 (R18)

- 学習・調整側:
  - `gold:triage_flat_v1` (`data/eval/triage_gold_flat_v1.jsonl`) と、その元の `data/eval/triage_goldset_v2.json`
  - triage の教師 (`data/eval/triage_flat_teacher_v1.jsonl`・`triage_flat_teacher_v2*.jsonl`・`triage_flat_teacher_items.json`)
  - 旧 triage の正解集 (`triage_gold_v2`〜`v4`・`triage_gold_v3_r`・`triage_goldset.json`・`triage_boundary_ab_v1`・`triage_prod_boundary_v1`)
  - s24・s25 の学習集合 (RP-9 の `exclusion_keys.json` の guard と同じ源)
- RP-9 の凍結集 (`data/eval/rp9_frozen/frozen_articles.jsonl`) は判定の側だが、同じ記事を 2 つの判定集に
  入れない (EVAL-S26 で集合ごとの結果が独立になるように)。凍結集の閉包も除外する
- 閉包: README の鍵 (最新の事象の根 ∪ CVE・被害組織の共有 ∪ 要約埋込 cos ≥ 0.80、`scripts/replay/_closure.py`)。
  ハブの切り方は RP-9 と同じ (`relation_features.FOCUS_CVE_MAX` = 3 を超える記事はその欄を鍵にしない)。
  `max_component_share = 0.30`
- 母集団の記事のうち学習・調整側・凍結集と同じ成分に入る記事は**選び出しの前に除き**、除いた件数を源ごとに報告する

## 8. 判定集の同一性

| id | path | sha256 | 行 | 作成 |
|---|---|---|---|---|
| `gold:triage_fresh_s26` | `data/eval/triage_fresh_s26/gold.jsonl` | (作成時に manifest と台帳へ) | ≤150 | TRIAGE-GOLD-S26 |

- 判定にしない集合: `gold:triage_flat_v1` (`data/eval/triage_gold_flat_v1.jsonl`)、§7 の各集合

## 9. 実行の前提

- 選び出しと裁定のスクリプトは `require_prereg("triage_gold_s26", …)` を通す
- 実行は使い捨てのコンテナ (`docker compose run --rm --no-deps -T kuebiko python …`)。本番の表に書かない
- manifest に: `prereg_git_sha`・`prereg_blob_sha256`・HEAD・母集団の件数・除外の件数 (源ごと)・乱数種・
  R18 の版と閉包の占有率・`RUBRIC_VERSION`・標本と正解の sha256・正解の分布 (high / medium / low・境界)

## 10. 変更履歴

- 2026-11-01: 作成
- 2026-11-01 (実行の記録、線・母集団・標本の変更ではない): §2・§5 の「無ければ RSS 概要」は満たせなかった。`articles` に RSS の概要 HTML は保存されておらず (`summary` は LLM の要約)、入力は保存された本文から `strip_html(body)[:1500]` で作った。本文が空の 6 記事は「入力が空」として除外 (§2 の除外 (2))。母集団 1,190 記事 (重複行 50 を畳む) → 入力あり 1,184 → 汚染 (R18.1、最大成分 21.1%) 372 を除き適格 812 → 標本 150。正解: high 13 / medium 38 / low 99、境界 39。境界を除いた high + medium は 24 件で §6 の床 (40) を下回る — EVAL-S26 の prereg で扱いを決める (この集合は変えない)
