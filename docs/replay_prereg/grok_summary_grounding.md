# RP-4-1: Grok 投稿の要約の裏付け関門 (GROK_SUMMARY_GROUNDING) を on にするかの事前登録

- 下書き: 2026-11-05 (JST)。**この文書の main への前のコミットが、道具の実行 (標本の抽出と Opus の呼び出し) より前**。
  期日は計画台帳の `RP-4-1.date` = 2026-11-06 (台帳の `stats.prereg` が指す先がこの文書)
- 作成時点で存在しないもの: 標本の id、Opus の判定。著者は標本を抜かず、Opus を呼んでいない。見たのは
  関門のコード (`src/pipeline/summary_grounding.py`、`briefing.py:_grounding_reasons`) と、保存済みの Grok 記事の
  件数・日別の分布 (SELECT のみ)・10-24 の測定の報告 (`data/eval/s24c_blockers_2026_10_24.md`)
- 対象の判断: `GROK_SUMMARY_GROUNDING` を shadow から **on** (違反なら 1 回作り直し、なお違反なら LLM 要約を捨てて機械変換版に落とす)
  にするか。結果は 3 つ: on (GROK-GROUNDING-ON) / shadow のまま / off (GROK-GROUNDING-STOP)
- 判定する構成: `src/pipeline/summary_grounding.py:ungrounded_reasons` を `briefing._grounding_reasons` と同じ引数で当てる
  (要約 = `summary` + `\n` + `remediation`、`body`、`title`、`exempt_years = (記事の公開年,)`)。関数は読むだけで LLM を使わない

## 1. 仮説

関門が発火した Grok 記事の要約は、発火の理由に当たる事実 (本文に無い数字・日付・矛盾する「攻撃者の記載はない」) を
実際に足している率が高い (= 発火は主に真の誤りで、on にしても正しい要約を捨てる害が小さい)。かつ発火率が
約 1 割以内で、捨てる要約が多すぎない。向きは「発火の中の誤りの率」が高いほど on に寄る。

## 2. 母集団

### 2.1 単位と時刻

- **単位 = X の投稿 1 件** (url の `/status/<id>` の id)。同じ投稿が別のレポートで別の `article_id` を持つので、
  同じ `status_id` は `created_at` が最も早い行 (同時刻なら `articles.id` が小さい行) 1 行に畳む
- **投稿の時刻** = `status_id` から求めた時刻 (`((status_id >> 22) + 1288834974657) / 1000` 秒、UTC)。
  README の `EVENT_TS_EXPR` は Grok では「レポートの時刻」になり投稿の時刻ではない (`articles.published_at` は
  同じレポートの投稿で同じ値) ため使わない。grok_link_fetch.md §2.1 の決定 D1 と同じ理由
- 対象の列: 畳んだ行の `title`、`body`、`summary`、`remediation`、`published_at` (年のみ)

### 2.2 定義

```sql
-- body_source='grok' かつ body が空でなく summary が空でない、url に /status/<id> がある
SELECT DISTINCT ON (status_id) id, article_id, url, title, body, summary, remediation,
       published_at, created_at, status_id
FROM (SELECT *, substring(url from '/status/([0-9]+)') AS status_id FROM articles
      WHERE body_source = 'grok' AND COALESCE(body, '') <> '' AND COALESCE(summary, '') <> '') t
WHERE status_id IS NOT NULL
ORDER BY status_id, created_at, id
```

窓 = 投稿の時刻が **2026-10-25 00:00 JST 以上、2026-11-04 00:00 JST 未満** (10 日間)。

### 2.3 除外して数える欄 (報告に件数を必ず書く)

| 段 | 理由 |
|---|---|
| url に `/status/<id>` が無い行 | 単位と時刻が決まらない |
| body または summary が空の行 | 関門が何も判定しない |
| 同じ投稿の 2 行目以降 | 畳み |
| 投稿の時刻が窓の外 | 窓の定義 |
| `gold:grok_link_44` の 44 投稿 | tainted (§7) |
| R18 の閉包で 44 投稿と同じ成分 | §7 |

### 2.4 窓を 10-25 に始める理由 (台帳の「直近 14 日」からの意図的な逸脱)

台帳の `weighting` は「直近 14 日」と書くが、関門の語・例外・発火率 10.6% (564 件) は 10-24 に Grok の過去記事で
作り調べた (`s24c_blockers_2026_10_24.md`)。10-24 以前の投稿は関門の設計を見た後の標本になり、独立でなくなる。
10-25 以降の投稿は関門のコードの確定 (10-24) より後に書かれたので、設計に使えない。これを優先する。
変更履歴 (§9) に理由を残す。件数が 14 日より少ないことは受け入れる。

## 3. 期間と事象時刻の分割

学習・調整に使う期間は無い (関門の語・閾値をこの判定の中で選ばない)。窓は 2.1 の投稿の時刻で切る。

## 4. 標本

1. 母集団の全件に関門を当て直し、**発火の全数と発火率 (発火数 / 母集団)** を数える (抽出に先立つ全数の計算。LLM なし)
2. **発火側**: 発火した投稿を `status_id` の数値の昇順に並べ、`random.Random(20261105).sample(並べた list, 30)`。
   発火が 30 未満なら全数 (その場合は区間が広い旨を報告する)
3. **非発火側 (参考)**: 発火しない投稿を同様に並べ、`random.Random("20261105:nofire").sample(…, 6)`。
   見逃し (関門が拾えない誤り) の目安を見るだけで、**判定には使わない** (n=6 では区間が広すぎる)
4. 抜いた投稿の `status_id`・`article_id`・発火の理由の種類 (number / date / absent_actor)・母集団の各段の件数を
   `sample.jsonl` と `population.json` に書く。**本文・要約は出力ファイルに書かない**
   (Opus への入力は実行時に DB から読む)
5. 重み: なし (単純無作為)。率は発火の母集団に対して。層化しない (要約の生成モデルの世代は記録欄 `created_at` の日付で
   報告に分布を併記するだけ。窓の全体が 10-24 以降の同じ構成 `kuebiko-sft:s24ci65` の下にあるため、世代の層化は不要)

## 5. Opus の問い (盲検)

- 判定者: `claude-opus-5-5` (adjudication_cache 経由、rubric の版を key に含める)
- 入力: 題・本文・**保存済みの要約と対処**。関門が発火したか・理由は見せない。発火側と非発火側を混ぜ、順序は
  `random.Random("20261105:order")` で決まる
- 照合範囲: 本番の `GROK_LINK_FETCH` は shadow で要約の材料は投稿の本文のみなので、**題 + 本文だけ** (linked_context は入れない)
- 問い: 「要約・対処に書かれた事実のうち、本文に根拠が無い、または本文と矛盾するものはあるか。数字・日付・版・CVE・
  金額・攻撃者の帰属・組織名・件数を特に確認せよ。言い換え・換算・要約として自然な省略は誤りとしない」
- 出力: `unsupported_claims`: 本文に無い/矛盾する主張の一覧 (各 `claim`・`kind` ∈ number|date|actor|other・`severity` ∈ heavy|minor)、
  `verdict` ∈ {none, minor_only, heavy}、`confidence`、`reason`
  - heavy = 本文に無い版・日付・CVE・金額・件数の補完、本文と矛盾する帰属、捏造
  - minor = 言い回しの飛躍・本文から推測できる補足
- 回数: 見積もり 30 (台帳)、**上限 39 (= 30 × 1.3)**。キャッシュに当たらない試行 (再試行を含む) を 1 回ずつ数える。
  発火側 (30) を先に判定し、使った数が 33 以下なら非発火側 6 を続ける。上限に達したら止め、再開できる形で残す

## 6. 指標と合否の線 (結果を見る前に固定)

### 6.1 指標

- **発火率** = 発火数 / 母集団 (全数。区間は Wilson 95% を併記)
- **発火の精度 P** = 発火側の標本のうち Opus の `verdict` が heavy または minor_only (= 本文に根拠の無い主張が実際にある) の率。
  Wilson 95% 区間。**主指標**
- **重い誤りの精度 P_heavy** = `verdict == heavy` の率 (従)
- **理由別**: 発火の理由の種類 (number / date / absent_actor) ごとの P (n が小さければ件数のみ)
- **一致の確認**: 関門が拾った具体の値 (理由のトークン) が Opus の `unsupported_claims` に出る率 (参考)
- **非発火側 (参考)**: 6 件のうち Opus が heavy と判定した件数 (見逃しの目安。推測はしない)

### 6.2 合否の線 (3 つの結論)

CLAUDE.md・台帳に数値の線が無いため、ここで決める。害は「発火した要約の LLM 版が機械変換版に落ちる」ことなので、
発火の大半が真の誤りであり、発火が多すぎないことを求める。

- **on**: 発火率 ≤ 15% **かつ** P の点推定 ≥ 0.70 **かつ** P の Wilson 95% 区間の下限 ≥ 0.50
- **off** (GROK-GROUNDING-STOP): P の点推定 < 0.50、または P の区間の上限 < 0.60
- **それ以外 (shadow のまま)**: on の条件を満たさず off でもない場合 (例: 発火率 > 15%、区間が 0.50 をまたぐ)
- 発火が 20 件未満で全数を判定した場合は、区間が広いため on にしない (shadow か off のみ)
- P_heavy は結論の線に使わない (on にした後に捨てる要約の重さの目安として報告する)

### 6.3 無効の条件

- 母集団が 100 未満 (発火率の推定が成り立たない): 判定を保留して報告する
- Opus の失敗・拒否が標本の 20% を超える: 結論を出さず再開する
- prereg より前に抽出か Opus を呼んだ: 無効

## 7. 汚染 (R18)

- tainted: `gold:grok_link_44` (関門の語の選定に使った 44 投稿、`data/eval/grok_link_fetch_2026_10_24/sample.json`)。
  窓 (10-25 以降) はこの 44 と時刻が重ならないが、R18 の閉包を念のため当てる
- 閉包の辺 = 最新の事象の根 (`event_item_members` → `event_items.merged_into` を辿った根) が同じ / 本文から正規表現で取った
  CVE を共有 / 要約埋込 (`summary_embeddings`) のコサイン 0.80 以上。`scripts/replay/_closure.py:build_components` を使い、
  44 投稿と同じ成分に入った母集団の投稿は抽出の**前**に外し、件数を報告する。埋込が無い記事は cos の辺に入れず、件数を報告する
- 被害組織の辺は Grok の記事に組織の列が無いため使わない (報告に明記)
- 閉包の版 (`R18_VERSION`)・上限 (`max_component_share`)・件数を manifest に残す
- 関門の設計 (10-24) で見た 4 件の重い誤りの投稿は id が文書に残っておらず、窓 (10-25 以降) が時刻で分けるので除外の操作はしない

## 8. 判定集の同一性

判定の結果 (`judgments.jsonl`) は本項目 (RP-4-1) が作る新しい集合で、学習・調整に使わない。
`path` = `data/eval/rp4_1_grok_grounding/judgments.jsonl`、sha256 と行数は manifest に残す。
作成者 = RP-4-1。tainted の台帳には載せず、`gold:grok_grounding_fresh_1105` として登録を台帳側で行う。

## 9. 変更履歴

| 日付 | 項目 | 変更前 | 変更後 | 理由 | 結果を見た後か |
|---|---|---|---|---|---|
| 2026-11-05 | 窓 | 直近 14 日 (台帳の weighting) | 投稿 10-25 00:00 JST〜11-04 00:00 JST | §2.4: 関門の設計 (10-24) の後の投稿だけにして独立を守る | いいえ (作成時) |
