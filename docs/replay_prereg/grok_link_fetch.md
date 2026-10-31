# GROK-LINK-JUDGE: Grok の投稿のリンク先の本文を要約の材料に入れるか (GROK_LINK_FETCH=on) の事前登録

- 下書き: 2026-10-31 (JST)。**この文書の main へのコミットが、標本の抽出 (GROK-LINK-SAMPLE-CLOSE の後) と
  2 腕の生成 (GPU-A2) より前**。期日は計画台帳の `GROK-LINK-JUDGE.stats.prereg_commit_by` = 2026-11-06
- 作成時点で存在しないもの: 標本 40 の id、2 腕の要約、Opus の判定。著者は標本を抜かず、要約を作らず、LLM を呼んでいない。
  見たのは母集団の件数 (§2.4、SELECT のみ) と 10-24 の測定の報告 (`data/eval/grok_link_fetch_2026_10_24.md`)
- 対象の判断: GROK-LINK-ON (`GROK_LINK_FETCH` shadow → on) を出すか、GROK-LINK-OFF にするか
- 判定する構成: `src/grok/link_fetch.py`・`src/grok/link_relevance.py` (関門 `GROK_LINK_RELEVANCE_GATE`、既定 on)・
  `src/pipeline/grok_convert.py` / `src/pipeline/briefing.py:_summarize_and_build` の要約の材料の組み立て
  (`prompt_body = 投稿の本文 + build_link_section(pages)`)。関門と取得のコードは 2026-10-24 22:26 JST
  (`5425f74d`) 以降変わっていない (最初の `linked_context` の保存は 10-25 10:16 JST)
- 10-24 の測定 (44 投稿、`gold:grok_link_44`) は**関門の語の選定に使った (tainted)**。この判定には使わず、
  合格線の根拠にもしない (§6 の線は目標と害の側から決めた)

## 1. 仮説

同じ Grok の投稿について、関門を通ったリンク先の本文を材料に足した要約 (リンクあり) は、足さない要約 (リンクなし =
今の本番) より Opus の盲検で好まれ、重い誤り (捏造・誤帰属・版/日付/CVE の補完) のある要約が増えない。

## 2. 母集団

### 2.1 単位と時刻

- **単位 = X の投稿 1 件** (url の `/status/<id>` の id = `status_id`)。`articles` は同じ投稿が別のレポートで別の
  `article_id` (`grok:<レポート>:<uid>#<status_id>`) を持ち、同じ `article_id` も複数行ありうる
  (作成時点で 118 行 → `article_id` 116 → 投稿 108)。同じ `status_id` の行は `created_at` の最も早い行
  (同時刻なら `articles.id` の小さい行) 1 行に畳み、その行の `linked_context`・`body` を使う
- **投稿の時刻 = `status_id` から求めた時刻** (X の id の上位ビット: `((status_id >> 22) + 1288834974657) / 1000` 秒、UTC)
  - ⚠ `articles.published_at` は **Grok のレポートの時刻であり投稿の時刻ではない** (作成時点で投稿 108 件に対し
    `published_at` は 36 値、同じレポートの投稿は同じ値)。承認された定義 (投稿の時刻で切る、計画台帳
    `GROK-LINK-PREREG.user_approval`) の意図に合うのは id の時刻なので、こちらを使う (§9 の決定 D1、利用者承認 2026-10-31)
- **保存の時刻 = 畳んだ行の `articles.created_at`**

### 2.2 定義 (SQL)

`excl` = `gold:grok_link_44` の 44 投稿の `status_id` (`data/eval/grok_link_fetch_2026_10_24/sample.json` の各行の
`url` の `/status/<id>`。sha256 `4424d45647e528630e7178bfe0fb2966768ab62db57e9e0fcc25165bc7c4fd9e`、44 行すべてに id がある)。
実行時は file から VALUES を作って下の SQL に差し込む (列挙をこの文書に写さない)。

```sql
WITH excl(status_id) AS (VALUES /* sample.json の 44 個の status_id */),
rows AS (
  SELECT a.id, a.article_id, a.url, a.created_at, a.published_at, a.linked_context,
         substring(a.url from '/status/([0-9]+)') AS status_id
  FROM articles a
  WHERE a.body_source = 'grok' AND a.linked_context IS NOT NULL
),
first_row AS (
  SELECT DISTINCT ON (status_id) * FROM rows
  WHERE status_id IS NOT NULL
  ORDER BY status_id, created_at, id
),
post AS (
  SELECT f.*,
    to_timestamp((((f.status_id::bigint) >> 22) + 1288834974657) / 1000.0) AS posted_at,
    EXISTS (SELECT 1 FROM jsonb_array_elements(f.linked_context::jsonb) p
            WHERE (p->>'ok')::boolean AND COALESCE(p->>'text', '') <> ''
              AND p->>'skip_reason' IS NULL)                          AS gate_ok,
    f.status_id IN (SELECT status_id FROM excl)                       AS excluded44
  FROM first_row f
)
SELECT *, (created_at > posted_at + interval '48 hours') AS recovered
FROM post
WHERE posted_at >= '2026-10-24 15:00+00'      -- 2026-10-25 00:00 JST
  AND posted_at <  '2026-11-06 15:00+00'      -- 2026-11-06 23:59:59 JST まで
  AND created_at < '2026-11-06 15:00+00'      -- 保存の上限 2026-11-06 23:59:59 JST
  AND gate_ok                                 -- 関門を通った参照先が 1 つ以上
  AND NOT excluded44;
```

- **関門を通った** = `linked_context` の要素のうち、取得成功 (`ok`)・本文あり・`skip_reason` なし (= `off_topic` でない)
  が 1 つ以上。これは本番の `build_link_section` が材料に入れる条件と同じ
- **層の印 `recovered`** = 保存が投稿の 48 時間より後 (`created_at > posted_at + 48h`)。GROK-RECOVER (11-01) の
  取り戻し分を想定。標本に残し、感度の分析で別に示す (§6.3)

### 2.3 除外して数える欄 (報告に件数を必ず書く)

| 段 | 理由 |
|---|---|
| url に `/status/<id>` が無い Grok の行 | 単位と投稿の時刻が決まらない (作成時点 0 行) |
| 同じ投稿の 2 行目以降 | §2.1 の畳み |
| 投稿の時刻が窓の外 | 窓の定義 |
| 保存が 11-06 23:59 JST より後 | 保存の上限 (窓を閉じた後の取り戻し・再取得で母集団が増えないように) |
| 関門を通った参照先が無い | 取得失敗・短すぎ・off_topic だけ、などの内訳も数える |
| `gold:grok_link_44` | tainted |
| R18 の閉包で `gold:grok_link_44` と同じ成分 | §7。抽出の**前**に外す |

### 2.4 作成時点の件数 (2026-10-31 11:30 JST、SELECT のみ)

- `linked_context` のある Grok の行 118 → 投稿 108。投稿の時刻が窓の中 106 (窓の前 = 10-24 JST の投稿が 2、
  いずれも保存は 10-25)。保存の上限の内 106。取得成功の参照先あり 88。**関門を通った参照先あり 75 = 現在の候補**
  (`gold:grok_link_44` との重なり 0、`recovered` 0、保存の遅れは最大 22.5 時間・中央値 6.6 時間)
- 投稿の時刻 (JST) 別: 10-25 8 / 10-26 6 / 10-27 3 / 10-28 7 / 10-29 29 / 10-30 15 / 10-31 (途中) 7。
  10-24〜10-28 は Grok の欠落の期間 (GROK-RECOVER が取り戻す)。窓が閉じる 11-07 には 40 を十分に超える見込み
- 候補の `body` は全件ある (122〜6,047 字、中央値 295 字)。参照先が 2 つの投稿は 2 件

## 3. 期間と事象時刻の分割

- 学習・調整に使う期間は無い (閾値・語・プロンプトをこの判定の中で選ばない)。窓は**投稿の時刻** (§2.1) で切る。
  README の `EVENT_TS_EXPR` (公開時刻 = Grok ではレポートの時刻) ではなく投稿の時刻を使う理由は §2.1 と D1
- 生成するモデル `kuebiko-sft:s24ci65` は 10-24 に作られており、10-25 以降の投稿は学習に入りえない

## 4. 標本

- **抽出の時刻**: GROK-LINK-SAMPLE-CLOSE (2026-11-07 00:00 JST) の後、かつ 2 腕の生成 (GPU-A2) の前
- **手順**:
  1. §2.2 の SQL で候補を得る (使い捨てのコンテナで本番の DB を読む。`docker compose run --rm --no-deps -T kuebiko ...`)
  2. R18 (§7) で `gold:grok_link_44` と同じ成分の投稿を外す
  3. 残りを `status_id` の**数値の昇順**に並べ、`random.Random(20261107).sample(並べた list, 40)` で 40 件を抜く
     (Python 3.12 の `random`。並べ方と種を変えない)
  4. 抜いた 40 の `status_id`・畳んだ行の `article_id`・`recovered`・候補の総数と §2.3 の各段の件数を
     `data/eval/grok_link_judge_1107/sample.json` に書き、sha256 を manifest に残す。これが `gold:grok_link_fresh_1107`
     の標本 (§8)。**この file のコミットは生成の前**
- **重み**: なし (単純無作為)。`recovered` の印は抽出に使わない (層化しない)
- 候補 (R18 の後) が 40 未満なら抽出しない (§6.4 の無効)

## 5. 2 腕の生成 (GPU-A2、道具は GPU-TOOLS-GROKLINK)

### 5.1 腕

| 腕 | 材料 (`prompt_body`) | 本番での意味 |
|---|---|---|
| なし | 畳んだ行の `body` (投稿の本文 + 引用 + 参考 URL。本番が要約に渡した `tweet_body` そのもの) | 今の本番 (`GROK_LINK_FETCH=shadow`) |
| あり | 同じ `body` + `build_link_section(pages)` | `GROK_LINK_FETCH=on` |

- `pages` = 畳んだ行の `linked_context` を `LinkedPage` に戻したもの (**取り直さない**。保存された参照先の本文と
  `skip_reason` をそのまま使う)。腕の違いは `link_text` の有無だけ
- 関門の一貫の確認: 抜いた 40 件について、保存された `pages` に今のコードの `apply_relevance_gate(投稿の本文, pages)`
  を当て直し、`skip_reason` が保存と一致することを生成の前に確かめる (一致しなければ §6.4 の無効)
- プロンプト: 本番の `dispatch._load_template()` (DB の `summarizer_rubric` から合成した版、失敗時は legacy .j2) を
  `article` = 題を `NO_TITLE_SENTINEL` にした記事 (`has_title=False`、Grok の本番と同じ)、`body = prompt_body` で
  `render` したもの。テンプレートが参照する記事の欄は題だけ。合成した版の `summarizer_rubric` の版番号 (作成時点 7) と
  描画前のテンプレートの sha256 を manifest に残す
- 呼び出し: `_summarize_without_runaway` (暴走時の作り直し 1 回を含む本番の関数) → `_ensure_grounded_summary`
  (`GROK_SUMMARY_GROUNDING=shadow` に固定。照合範囲はその腕の `prompt_body`。違反は記録だけで出力を変えない)
- **モデル**: `build_llm_for(Step.ARTICLE_SUMMARY, config)` (fast ティア、task prefix `[task: summary]` を含む本番の client)。
  解決先は `kuebiko-sft:s24ci65`、digest `56d89a8cee6e6a15aa8b83fb1f264691c251f1ce61edb389a7b12c6d59c5f315`
  (作成時点の `model_tiers` 版 46 と Ollama `/api/tags`) に**固定**。生成の最初と最後に `/api/tags` を読み、
  解決先の名前と digest を manifest に残す。温度は本番の既定 (0.3)・`think=False`
- **回**: 各投稿 × 各腕 × 2 回 = 160 要約。順序は (回 1 の 40 投稿 × 2 腕) → (回 2 …)、投稿の中で腕の順は
  `sha256(f"{status_id}:{回}:gen")` の偶奇で入れ替える (同じ温度・同じ client の連続呼び出しの順の偏りを避ける)
- **失敗**: 1 つの要約 (投稿 × 腕 × 回) は最大 2 回まで試す (例外・schema 不一致。暴走の作り直しは本番の関数の内側で数えない)。
  2 回とも失敗したらその要約は欠け
- 出力: `status_id`・腕・回・要約の欄 (`title_ja`・`summary`・`analyst_note`・`remediation`)・裏付けの違反の理由・
  所要秒・モデル名と digest。本文は書き出さない (GPU-TOOLS-GROKLINK の宣言)
- 本番は PSTOP-GPU-A2 で止めた間に、使い捨てのコンテナで回す (CLAUDE.md §7)

## 6. 判定 (Opus の盲検) と検定

### 6.1 Opus に見せるもの

- 1 回の判定 = 1 つの (投稿, 回) の 2 要約。計 40 × 2 = 80 回 (台帳の `opus: 80`)。各回は新しい文脈
  (`data/mlx/adjudication_cache.adjudicate`、`data/eval/adjudication_ledger.jsonl` に記録、同時に走らせる裁定は 1 本 = R23)
- **材料**: 投稿の本文 (`body`) と、**関門を通ったリンク先の本文** (あり腕に入れたものと同じ `pages`、url と本文)。
  両腕とも同じ材料に照らす
- **要約 X / Y**: 各要約の 見出し (`title_ja`)・要約・分析メモ・対処 だけ。腕の名前・モデル名・回・裏付けの違反・所要秒は見せない
- **X / Y の割当**: `sha256(f"{status_id}:{回}:20261108")` の整数の偶奇で、奇数ならあり腕を X にする (乱数種 20261108)
- **ルーブリック**: 10-24 の `data/eval/grok_link_fetch_2026_10_24/judge.py` の `RUBRIC` の文を変えずに使う
  (10-24 の結果と同じ物差しで比べるため。D5)。要点:
  - 要約の主張は、材料 (投稿 + リンク先) で裏付けられるものだけが正しい
  - **事実の誤り** = 材料と食い違う、または材料のどこにも無い断定 (数値・組織名・帰属・時期・手口・対処の誤りや補完)
  - **重い誤り (heavy)** = 読者が誤った判断をしうる誤り (存在しない版番号・日付・帰属・被害規模・手口の捏造)。
    言い回しの揺れは heavy にしない
  - 長さ自体は評価しない
- **ラベル** (schema は 10-24 の `Judgment` と同じ):
  - 好み 3 段: `X` / `Y` / `tie` (+ 理由 1 文) → あり腕から見て **+1 (ありが良い) / 0 (tie) / −1 (なしが良い)** に戻す
  - 重い誤り: 要約ごとに yes / no (`x_heavy` / `y_heavy`)
  - 報告のみ: 誤りの一覧・抜けの一覧・崩れ (yes/no)
- 判定の失敗 (応答なし・schema 不一致) は新しい文脈で 1 回だけやり直す

### 6.2 投稿単位の値

- 好み: `s_i = 回 1 の値 + 回 2 の値` ∈ {−2, …, +2}。片方の回が欠けた投稿は残った回の値 × 2 (欠けの件数を報告)
- 重い誤り: 腕ごとに「2 回のうち 1 回でも heavy」= その腕の heavy あり (片方の回が欠けた投稿は残った回だけで決める)

### 6.3 検定と合否の線 (結果を見る前に固定。台帳に数値の線が無いので調整役が提案し利用者が承認 2026-10-31、§9)

台帳の検定 (`GROK-LINK-JUDGE.stats.paired_test`) に従う:

- **主検定 — 符号検定**: `s_i > 0` の投稿数 vs `s_i < 0` の投稿数 (`s_i = 0` を除く) の正確な二項検定、**片側**
  (H1: ありが良い)
- **併記 — Wilcoxon の符号順位検定**: `s_i` (順序の値) に対して、0 を除く (`zero_method="wilcox"`)、片側。報告のみ
- **重い誤り — McNemar の正確検定** (`scripts/replay/_stats.mcnemar_exact`): b = なしだけ heavy の投稿数、
  c = ありだけ heavy の投稿数。両側の p を報告
- **投稿単位の bootstrap**: 投稿を復元抽出 10,000 回 (`random.Random(20261109)`)、95% パーセンタイル区間を
  `mean(s_i)` と「heavy ありの率の差 (あり − なし)」に付ける

**合否 (すべて満たして合格 = GROK-LINK-JUDGE は on)**:

1. **好み**: 符号検定の片側 p < 0.05 **かつ** `mean(s_i)` の bootstrap 95% 区間の下限 > 0
2. **重い誤りが増えない**: c ≤ b
3. **害の型が残っていない**: 両方の回でなしが良い (`s_i = −2`) 投稿が 40 件中 4 件以下 (10%)

1〜3 のどれかを満たさなければ off (GROK-LINK-OFF)。§6.4 の無効なら判定しない (on でも off でもない)。

**報告のみ (合否に使わない)**: `recovered` の層を除いた 1〜3 (結論が変わるなら明記)・`recovered` の層だけの値・
回 1 と回 2 の好みの一致率・裏付けの違反 (`GROK_SUMMARY_GROUNDING` の理由) の件数を腕別に・所要秒の中央値の差・
参照先の数 (1 / 2) 別・投稿の言語 (日本語が主か) 別・誤りと抜けと崩れの件数・b と c の McNemar の p

### 6.4 無効 (判定しない。直して新しい標本で prereg からやり直す)

- 候補 (R18 で外した後) が 40 未満
- 抜いた 40 件で、保存された `skip_reason` と今のコードの関門の当て直しが 1 件でも食い違う
- 生成のモデルの解決先が `kuebiko-sft:s24ci65` でない、または digest が §5.1 の値と違う (最初・最後の確認のどちらかで)
- 160 要約の生成が 1 つの HEAD・1 つのテンプレート (sha256) で行われていない
- 生成の欠けが 160 のうち 8 (5%) を超える、または 2 回とも欠けた投稿が 4 (10%) を超える
- 判定の失敗 (やり直しの後) が 80 のうち 4 (5%) を超える
- この文書のコミットが標本の抽出または生成の開始より後 (`scripts/replay/_gate.require_prereg` が通らない)
- この文書のコミットの後に、§5.1 の生成の経路 (`build_link_section`・`link_relevance.py`・`_summarize_and_build` の
  材料の組み立て・`_summarize_without_runaway`) か §6.1 のルーブリックを変えた

## 7. 汚染の鍵 (R18)

- 学習・調整側 = `gold:grok_link_44` の 44 投稿 (`sample.json` の `article_id`)。判定側 = §2.2 の候補
- README の閉包 (最新の事象の根 ∪ CVE・被害組織の共有 ∪ 要約埋込 cos ≥ 0.80、`scripts/replay/_closure.py`、
  `R18_VERSION` を manifest に)。候補の各投稿は畳んだ行の `article_id` で閉包に入れる
- ハブの切り方: CVE・被害組織が `relation_features.FOCUS_CVE_MAX` (= 3) を超える記事はその欄を鍵にしない。
  最大成分の占有率の上限 `max_component_share = 0.30` (RP-2 と同じ)
- 学習側と同じ成分に入る候補は**抽出の前に**外す (§4 の 2)。件数を報告する
- 生成のモデル `s24ci65` の学習集合と 10-25 以降の投稿は時期で交わらない (§3)

## 8. 判定集の同一性

| id | path | sha256 | 行 | 作成 |
|---|---|---|---|---|
| `gold:grok_link_fresh_1107` | `data/eval/grok_link_judge_1107/sample.json` (標本) と同じ dir の `judgments.jsonl` (Opus の判定) | 抽出時に manifest へ | 40 (判定 80) | GROK-LINK-JUDGE |

- 判定にしない集合: `gold:grok_link_44` (`data/eval/grok_link_fetch_2026_10_24/`、tainted)
- この標本と判定は、判定の後に関門の語・材料の見出し・プロンプトの調整に使ったら tainted にする (台帳の `gold_sets` を更新)
- manifest に: `prereg_path`・`prereg_git_sha`・`prereg_blob_sha256`・HEAD・標本の sha256・`summarizer_rubric` の版と
  テンプレートの sha256・モデル名と digest (最初・最後)・`model_tiers` の版・`GROK_LINK_RELEVANCE_GATE` と
  `GROK_SUMMARY_GROUNDING` の値・R18 の版と占有率・§2.3 の各段の件数

## 9. 決定 (調整役の提案。**承認済み** の印のあるものは 2026-10-31 に利用者が承認してから封をした)

| # | 決定 | 理由 | 確認 |
|---|---|---|---|
| D1 | 投稿の時刻 = `status_id` の時刻 (`published_at` ではない) | `published_at` はレポートの時刻 (§2.1)。承認された「投稿の時刻で切る」の意図に合うのは id の時刻。`published_at` で切ると 10-24 JST の投稿がレポートの時刻次第で窓に入る | **承認済み (利用者 2026-10-31)** — 承認文の「投稿の published_at」を status id の時刻に読み替える |
| D2 | 単位 = `status_id`、最も早い保存の行に畳む | 同じ投稿が別のレポートで別の `article_id` になる (108 投稿 / 118 行) | — |
| D3 | 乱数種: 抽出 20261107・X/Y 20261108・bootstrap 20261109 | 日付の型 (他の prereg と同じ) | — |
| D4 | 1 回の判定 = (投稿, 回) の 2 要約、投稿単位で `s_i` に足す | 台帳の Opus 80 回 = 40 × 2 回。検定の単位は投稿 (回どうしは独立でない) | — |
| D5 | ルーブリックは 10-24 の文のまま。審判にもリンク先を見せる | 物差しを揃える。審判がリンク先を見ないと、あり腕の正しい事実を「材料に無い」と誤りに数える。代わりに、抜けはあり腕に構造的に有利なので合否に使わない (報告のみ) | — |
| D6 | 合否 1: 符号検定 片側 p < 0.05 かつ bootstrap 下限 > 0 | 台帳に線が無い。片側は判断が「on にする価値があるか」の一方向のため | **承認済み (利用者 2026-10-31)** |
| D7 | 合否 2: 重い誤りは c ≤ b (増えない) | 主は好み。重い誤りは害の確認として非劣性で置く (n = 40 で減少の有意を必須にすると、好みで勝っても落ちうる) | **承認済み (利用者 2026-10-31)** |
| D8 | 合否 3: `s_i = −2` が 4/40 以下 | 10-24 に見た害の型 (情報の無いリンク先) が関門の後にも残っていないかの歯止め。10-24 の調整に使った率を線に使わない (10% は目安の切りの良い値) | **承認済み (利用者 2026-10-31)** |
| D9 | `recovered` は合否の母集団に含め、除いた結果は報告のみ | 利用者の承認 (層の印で残し感度の分析で別に示す) | — |
| D10 | 生成のモデルは s24ci65 の digest に固定し、本番の割当が変わっていたら無効 | 台帳 GPU-A2 の「今の本番の s24ci65」。s26 での再確認は EVAL-S26 の Grok の行 | — |
| D11 | 無効の線: 生成の欠け 5%・2 回欠けの投稿 10%・判定の失敗 5% | 欠けが片方の腕に偏ると好みが歪む。160 のうち 8 を超える欠けは道具かモデルの不具合 | **承認済み (利用者 2026-10-31)** |

## 10. 変更履歴

- 2026-10-31 下書き (標本・要約・判定は未作成)
