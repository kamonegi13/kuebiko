# P0-11-GOLD / P0-11-JUDGE: SIR 別の要点の文の裏付けの判定集 (事前登録)

- 作成: 2026-10-31。**この文書のコミットが P0-11-GOLD の文の選び出しより前** (台帳の `prereg_commit_by` 2026-11-02)
- 対象の判断: P0-11-JUDGE → P0-11 (SIR 別の要点の文単位の裏付けの関門を、配線と on を同じデプロイで出すか)
- 対象のコード:
  - 生成: `src/digest/pir_daily_focus.py` の `collect_pir_focus_sections` → `_generate_llm_summary`
    (`Step.PIR_DAILY_FOCUS`、fast ティア。識別子の関門 `PIR_FOCUS_FIDELITY_GATE` は本番 on)。
    `kuebiko-sft:s24ci65` は `focus_v6.is_enabled` の前方一致 (`kuebiko-sft:s24c`) に当たるため v6.2 の文面で書く
  - 関門: 同ファイルの `apply_claim_gate` → `_score_claim_sentences` (文の分割は `split_claim_sentences`、
    「該当しない」の明示文は `focus_v6.is_not_applicable` で判定の対象外、文のスコアは記事ごとの
    `probe_choice(Step.PIR_FOCUS_CLAIM_JUDGE, _claim_question(...))` の P(Yes) の最大)
  - **本番では関門は走っていない**: 旗 `PIR_FOCUS_CLAIM_GATE` は本番コンテナで 1 だが、`src/pipeline/runners.py:430` が
    `config` を渡さず、`apply_claim_gate` の冒頭 (`pir_daily_focus.py:186`) で早く return する。
    よって保存された要点は**関門を通っていない** (識別子の関門だけを通った) 文で、関門の生存者の偏りは無い
- 作成時に見たもの: 母集団の件数・字数の合計・引いた記事の照合の件数だけ (§2.3)。文の中身・語の重なりの値・
  確率・ラベルは見ていない。LLM は呼んでいない

## 1. 仮説

P0-11-GOLD は判定集を作るだけで、それ自体に仮説は無い。P0-11-JUDGE の仮説: s26 の P(Yes) に閾値を当てた関門は、
SIR 別の要点のうち引いた記事に裏付けの無い文を落とし、裏付けのある文をほとんど落とさない
(合否の線は P0-11-JUDGE が §6 に足す。本書では決めない)。

## 2. 母集団

### 2.1 単位と識別

- **朝のブリーフ** = `daily_briefs` の `slot = 'morning'` の 1 行。時点 **t = `daily_briefs.generated_at`**
- **SIR の要点** = その行の `payload` (JSON、`src/digest/brief_payload.py:_pir_section_payload`) の `pir` の配列の 1 要素。
  `summary` が要点の本文 (= `PirFocusSection.llm_summary`)、`matches` が要点を書くときに LLM に渡した記事 (上位 ≤3 件、
  `title` / `url` / `importance` / `feed` / `tier`)。**payload は `article_id` も pir の id も持たない** — SIR は配列の位置
  (`section_idx`、0 始まり) と `title` で、記事は §2.2 の照合で識別する
- **文** = `split_claim_sentences(summary)` の 1 要素 (関門と同じ「。」区切り、`sentence_idx` は 0 始まり)。
  文の id = `(brief_id, section_idx, sentence_idx)` (`brief_id` = `daily_briefs.id`)

### 2.2 選び出しの SQL

朝のブリーフと SIR の要点 (読み取りのみ):

```sql
WITH briefs AS (
  SELECT DISTINCT ON (period_label) id, period_label, generated_at, payload::jsonb AS p
    FROM daily_briefs
   WHERE slot = 'morning'
     AND generated_at >= TIMESTAMPTZ '2026-10-26 11:22:01.479298+00'  -- model_tiers 版 42 の created_at
     AND period_label <= '2026-11-12'
     AND payload IS NOT NULL
   ORDER BY period_label, generated_at DESC, id DESC   -- 同じ日に複数あれば最後の 1 行
)
SELECT b.id AS brief_id, b.period_label, b.generated_at AS t,
       s.ord - 1 AS section_idx,
       s.sec->>'title'   AS sir_title,
       s.sec->>'summary' AS focus_text,
       s.sec->'matches'  AS matches
  FROM briefs b
  CROSS JOIN LATERAL jsonb_array_elements(b.p->'pir') WITH ORDINALITY AS s(sec, ord)
 WHERE coalesce(btrim(s.sec->>'summary'), '') <> '';
```

引いた記事の照合 (`matches` の 1 件ごと。`:t` は上の `t`):

```sql
SELECT article_id, title, summary, importance, feed_title, created_at, body_fetched_at
  FROM articles
 WHERE url = :url
   AND title = :title
   AND lower(coalesce(importance, '')) = :importance
   AND coalesce(feed_title, '') = :feed
   AND status = 'posted' AND duplicate_of IS NULL          -- evaluator._load_posted_rows と同じ
   AND created_at <= :t AND created_at >= :t - interval '48 hours';
```

- 照合の結果の `article_id` が**ちょうど 1 種類**なら、その記事 (同じ article_id の複数行は `created_at` の最新の 1 行) を
  引いた記事とする。0 種類・2 種類以上の記事が 1 件でもある SIR の要点は、文ごと母集団から外す
- 窓 48 時間: evaluator の `since` は実行開始の時刻から 24 時間で、`generated_at` (保存の時刻) より早い。
  余白を取り、一意性で絞る

### 2.3 判断と除外 (除いた数は層ごとではなく理由ごとに報告する)

1. **始まりを 10-26 ではなく model_tiers 版 42 の保存の時刻 (2026-10-26 11:22 UTC = 20:22 JST) にした**。
   台帳は「10-26 (fast の s24ci65 への切替) 以降」と書くが、10-26 の朝のブリーフ (id 273、生成 10-25 22:57 UTC) は
   切替の前で、s23i が従来の文面 (v6.2 でない) で書いた。生成のモデルと文面を 1 つに揃えるため、最初は 10-27 の朝
   (id 275)。この区間の版 42〜46 の間 `fast` は `kuebiko-sft:s24ci65` のまま、`step:pir_daily_focus` の上書きは無い
2. **終わりは period_label 2026-11-12 (JST の日付) で固定**。P0-11-GOLD の実行日 (11-13) を前提にし、実行が遅れても
   終わりは動かさない。実行が 11-13 より前なら実行しない (窓が埋まっていない)
3. 同じ `period_label` の朝のブリーフが複数あれば最後の 1 行だけ (Web が見せる版)。外した行の数を報告する
4. 要点が空の SIR (`summary` が空 = 生成の失敗・識別子の関門で要点なし) は文を持たないので数えない (件数だけ報告)
5. `focus_v6.is_not_applicable(sentence)` が真の文を外す (関門も判定しない「本 SIR には該当しない」の文。事実の主張でない)
6. 引いた記事が一意に決まらない SIR の要点を外す (§2.2)
7. **時点 t からの書き換え**: `articles.title` / `summary` は再取得の後の再処理で上書きされうる
   (`repo_articles._REPROCESS_UPDATABLE_COLUMNS`)。版の記録は無い。照合の `title = :title` (payload の見出しは時点 t の値) と、
   引いた記事のどれかで `body_fetched_at > t` (t の後に本文を取り直した) の SIR の要点を外す。
   これで捕まらない summary だけの書き換えは測れない (既知の限界)
8. 語の重なりを計算できない文 (§3 の `B(N(s))` が空 = 正規化後 2 字未満) を外す
9. 汚染 (§7)

作成時点 (10-31 の朝まで、5 本: id 275/277/279/281/283) の件数: SIR の要点 86 (要点の字数 合計 95,556、平均 約 1,111 字)、
文 1,264、うち該当しないの文 40。引いた記事 225 件のうち 219 件が一意に照合、6 件 (10-31 の朝、2 つの SIR の要点。同じ url・
見出し・出典の Grok の行が article_id 違いで 2 つずつある) が一意でない → 外す文 19。適格な文 1,205 (汚染と 8 を除く前)。
7 に当たる記事は 0 件。11-12 まで (17 本) で約 4,000 文の見込み

### 2.4 既知の偏り

- 11-01 の朝は、取り直した Grok の記事 (10-24〜28 の投稿) が取り込みの時刻で「直近 24h」として載りうる (台帳 Grok の取り直しの
  user_approved)。外さない (本番の朝のブリーフに実際に出た文なので)。`period_label` を行に残し、報告で分けられるようにする
- 文は「。」の機械的な分割で、1 文に複数の事実がある・指示語 (同社・同グループ) が前の文を指すことがある。関門と同じ単位にそろえるため変えない

## 3. 語の重なりと 3 つの帯

- 正規化 `N` = `src/assessment/evidence_verify.py:normalize_for_match` (NFKC・casefold・空白と記号を落とす)
- 2 字の組 `B(x)` = `src/eventnews/pair_features.py:_bigrams` (空白を落として連続 2 字の集合)。`N` の後に当てる
- 記事 k の照合の文字列 `A_k` = 記事の `title` + "\n" + `summary` (§2.2 で照合した行の値。関門の `_claim_article_block` が渡す
  事実の欄。重要度と出典名は入れない)
- **語の重なり** `O(s) = max_k |B(N(s)) ∩ B(N(A_k))| / |B(N(s))|` (文の 2 字の組のうち、引いた記事のどれか 1 件にある割合の最大)。
  1 件ごとの最大にするのは、関門のスコア (記事ごとの P(Yes) の最大) と S5 の定義 (事実が「いずれか 1 つの記事に」ある) に合わせるため。
  複数の記事を混ぜた文は 1 件あたりの重なりが下がる
- **帯の境界は母集団の三分位**: 適格な文 (§2.3 の 1〜9 を除いた後) を `O` の昇順に並べ、件数 N のとき
  `q1 = O[floor(N/3)]`、`q2 = O[floor(2N/3)]` (0 始まりの添字)。低 = `O < q1`、中 = `q1 ≤ O < q2`、高 = `O ≥ q2`。
  - 判断: 数値の固定の境界にしない。作成時点で `O` の分布を見ていない (見ると結果に近い量を見ることになる) ため、
    固定値だと帯が空・極端に偏る恐れがある。三分位は分布を見ずに決定論で決まり、各帯に約 N/3 (約 1,300) 文が入る
  - `q1`・`q2` の値と各帯の件数を manifest に残す

## 4. 標本と重み

- 層: §3 の帯、件数 **低 45 / 中 30 / 高 15** (計 90。低い帯を厚く = 裏付けの無い文が多いと見込む帯)
- 抽出: 各帯の適格な文を文の id `(brief_id, section_idx, sentence_idx)` の整数の昇順に並べ、`random.Random(20261113)` の
  1 つの乱数の列で低 → 中 → 高の順に帯ごとに shuffle し、先頭から件数分
- 不足のとき (1 回だけ): ある帯の適格な文が件数に届かなければ全件を取り、不足分を語の重なりが 1 つ低い帯へ足す
  (低の不足は中へ)。計 90 を超えない
- 文の数の上限 (同じ SIR の要点・同じ朝からの件数) は置かない。重みと区間はブリーフ単位の cluster で扱う (§6)
- 重み: 帯 h の適格な文の数 N_h / 標本の数 n_h (抜き出しの確率の逆数)。率は §2 の母集団 (10-27〜11-12 の朝) へ戻した値
- 選び出しは決定論で、**GPU-D1 (s26 の採点) の前に固定**する。標本の文の id・帯・`O`・重み・`period_label`・引いた記事の
  `article_id` と `sha256(title + "\n" + summary)` を `data/eval/pir_focus_fresh_1104/sample.jsonl` (gitignore) に書き、
  manifest に sha256 を残す
- **置き換え**: Opus の呼び出しが 3 回失敗した文は、同じ帯の shuffle の列の次の文で置き換える (列の順に。置き換えた数を報告)

## 5. 正解 (Opus、盲検)

- 付け手: `claude-opus-5-5`、effort medium、温度 0、新しい文脈 (S5 の `data/mlx/measure_S5.py:ground` と同じ設定)
- **渡すもの**: 引いた記事 (時点 t に要点を書いた LLM が見た記事だけ、1〜3 件) と、判定対象の文 1 つ。
  記事の整形は関門の `_claim_article_block` と同じ (`[記事N] [importance] title (feed_title)` + `要約: summary`、要約は切らない)。
  記事は §2.2 で照合した行の値 (`sample.jsonl` の sha256 と一致することを渡す前に確かめ、違えば渡さず置き換え扱い)
- **渡さないもの**: 生成のモデル名、関門の判定・確率、識別子の関門の結果、帯・`O`、SIR の名前と説明、同じ要点の他の文、
  ブリーフの日付、記事の本文 (生成も関門も本文を見ていない)。文の順は `random.Random(20261114)` で並べ替え、伏せた連番で渡す
- 問いと欄: S5 の裁定の文面 (`measure_S5.py` の `INSTRUCT`、`rubric_version = s5_v1_20261011_claim_level`) を**変えずに**使う。
  S5 (= 関門の閾値の較正に使った正解) と同じ定義にして、判定の数字が較正と比べられるようにする:
  - `supported`: 文の事実が**すべて** いずれか 1 つの記事に書かれていれば true。複数の記事の事実を混ぜた文・どの記事にも無い事実
    (捏造・断定の格上げ) を含む文は false
  - `evidence_article`: true のときの根拠の記事番号 (1 つ)。false または複数の記事に分かれるときは null
  - `boundary`: 言い換え・要約の範囲内かで判断が割れるときは true
  - `reason`: 記事のどの記述に基づくか
- 派生のラベル (P0-11-JUDGE が使う): **裏付けあり** = `supported ∧ ¬boundary` / **裏付けなし** = `¬supported ∧ ¬boundary` /
  **境界** = `boundary`
- 1 文 1 回 (Opus 90 回)。付け直し (κ) は台帳の Opus の枠 (90) に無いので行わない — 正解の安定性は測れない (既知の限界)
- 裁定は `data/eval/adjudication_ledger.jsonl` に記録 (R23)。正解集の id は `gold:pir_focus_fresh_1104`。学習・調整に使わない

## 6. P0-11-JUDGE が判定集をどう使うか (台帳に書かれた分だけ)

- 腕: s26 の P(Yes) を GPU-D1 で 90 文に当てる (本番の `_claim_question` と `_claim_article_block`、`probe_choice`、
  `Step.PIR_FOCUS_CLAIM_JUDGE`、記事ごとの最大)。閾値は PROBE-ROWS-S26 が判定集の**外**で決めた s26 の行の値
  (`effective_threshold`)。この判定集で閾値を選ばない
- 指標 (台帳 P0-11-JUDGE の `paired_test`): 関門の絶対の率 — 発火の適合 (落とした文のうち裏付けなし)・発火しない文の中の
  裏付けなしの率。率は §4 の重みで母集団へ戻す。Wilson の区間と、**朝のブリーフ (`brief_id`) 単位の cluster bootstrap
  2,000 回**の区間を出す (同じブリーフの文は相関する)
- 境界の文は主の指標から外し、境界を裏付けあり / なしに倒した 2 通りの感度を出す
- **合否の線・床 (裏付けなしの件数がいくつ未満なら判定不能か) は本書では決めない**。P0-11-JUDGE が GPU-D1 の採点より前に、
  本書の §6 に追記し (変更履歴に日時と理由)、そのコミットを採点の前に置く。追記が無ければ P0-11-JUDGE は率を報告するだけで
  P0-11 を出さない

## 7. 汚染の鍵 (R18 + 台帳の追加)

- 学習・調整側の記事:
  - `gold:s5_claims_gold` (`data/eval/s5_claims_gold.jsonl`) の 300 主張の 80 key (`date:pir_id`) が引いた記事 =
    `data/eval/c3_items.json` の同じ key の `matches[].article_id` (197 記事)
  - 関門の閾値の較正の集合: `data/eval/probe_calib/pir_focus_claim_judge.jsonl` (280 行、194 記事) と
    `data/eval/probe_calib/extra/pir_focus_claim_extra.jsonl` (150 行、139 記事) の `matches[].article_id`
  - s26 が `pir_daily_focus` か `pir_focus_claim_judge` を学習する場合はその学習の記事 (P0-11-JUDGE の時点で足し、件数を報告)
- 閉包: README の鍵 (最新の事象の根 ∪ CVE・被害組織の共有 ∪ 要約埋込 cos ≥ 0.80、`scripts/replay/_closure.py`)。
  ハブの切り方は THIN と同じ (CVE・被害組織が `relation_features.FOCUS_CVE_MAX` = 3 を超える記事はその欄を鍵にしない)。
  閉包の範囲 = 母集団の引いた記事 ∪ 上の学習・調整側の記事 ∪ それらと CVE・被害組織を共有する記事。`max_component_share = 0.30`
- 文は、引いた記事の**どれか 1 つ**が学習・調整側と同じ成分に入れば汚染として外す (引いた記事は SIR の要点で共通なので、実質は
  要点ごと外れる)。**選び出しの前に**外し、外した文と要点の数を報告する
- **台帳の追加の鍵「s5 の 300 主張と同じ朝のブリーフの文を除く」**: s5 の key の日付と同じ `period_label` の朝の文を外す。
  s5 の key の日付は 2026-05-13〜2026-10-09 で、母集団 (10-27〜) と重ならない — この鍵で外れる文は 0 になる見込み
  (0 でも適用し件数を報告する)。なお s5 の 300 主張は本番の朝のブリーフの文ではなく、C3 の date×SIR の入力を i58 で
  作り直した候補 (`c3c_candidates.jsonl` / `c3_candidates.jsonl`) の文。独立の実体は上の閉包が担う

## 8. 判定集の同一性

| id | path | sha256 | 行 | 作成 |
|---|---|---|---|---|
| `gold:pir_focus_fresh_1104` | `data/eval/pir_focus_fresh_1104/gold.jsonl` | (作成時に manifest と台帳へ) | 90 | P0-11-GOLD |

- 判定にしない集合: `gold:s5_claims_gold` (`data/eval/s5_claims_gold.jsonl`、sha256
  `979aea48b2893f8a19bb20e0b721e82635005a29835fc00bb58d1ffad1fb6fc0`、300 行)、較正の集合
  (`data/eval/probe_calib/pir_focus_claim_judge.jsonl` sha256 `3c2cbf9dd57486267588a4ff488047e0755259980dc6c6230a38ac3a69186f4d`、
  `data/eval/probe_calib/extra/pir_focus_claim_extra.jsonl` sha256 `022600dd0e2c983cb933a6b38c2705cc04297122173369c16bb20c020d680d2c`)
- 正解集の id の「1104」は名前だけで日付の意味を持たない (台帳の注)

## 9. 無効 (判定集を作り直す) と窓の打ち切り

**窓の打ち切り** (無効ではない — 終わりを早め、件数を報告する):
- 窓の中で `model_tiers` の `fast` または `step:pir_daily_focus` が `kuebiko-sft:s24ci65` から変わった、`PIR_FOCUS_PROMPT_V6` を
  本番で明示した、または `config/prompts/pir_daily_focus_rubric_v6_2.yaml` が変わった → 終わりを変わる前の最後の朝にする
- 窓の中で関門が本番で効き始めた (P0-11 の配線が出た。ログ `daily_focus_claim_gate_fired` / `_dropped_sentences` が出る) →
  終わりを効き始める前の最後の朝にする (それ以後の文は関門の生存者)

**無効** (この判定集を使わない):
- この文書のコミットが選び出しの実行の HEAD の祖先でない、または実行より後 (`require_prereg("pir_focus_claim", …)`)
- 打ち切りの後の適格な文が 270 未満 (= 各帯 90 未満。三分位の帯に件数の 2 倍が入らない)
- 置き換え (§4) が 9 文 (10%) を超える
- 選び出しの後に、§2〜§5 (母集団・語の重なり・帯・件数・乱数種・Opus に渡すもの・問い) を変えた
- 選び出しの前または途中で、標本の文の関門の確率・Opus のラベルを誰かが見た (この判定集は盲検の前提を失う)

## 10. 実行の前提

- 選び出しのスクリプトは `require_prereg("pir_focus_claim", …)` を通す。DB は読み取りだけ (`scripts/replay/_sqlguard.py`)
- manifest に: `prereg_git_sha`・`prereg_blob_sha256`・HEAD・朝のブリーフの id の一覧・§2.3 の理由ごとの除外の数・`q1`/`q2`・
  帯ごとの N_h と n_h・乱数種 (20261113 / 20261114)・R18 の版と閉包の占有率・標本の sha256

## 11. 変更履歴

(結果を見る前の変更も、ここに日時・項目・前後・理由を書く。P0-11-JUDGE の合否の線もここに追記する。
結果を見た後の変更は探索的扱い — README)

- 2026-10-31 作成
