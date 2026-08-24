# 事象単位ニュース (event news) 設計書

作成: 2026-08-23。**v2 — 独立レビュー 2 系統 (実装整合性 / 失敗様式) の指摘を反映して確定。**
レビュー台帳は §15。v1 (レビュー前) からの主要変更: 合否基準の全面書き直し (検出力ゼロの
検定 4 件を除去)、dedup 破棄の裏取り算入を v2 送り、群化の連鎖制御、識別子照合の単位を
[N] 記事に限定、中国国営メディアの SSoT 追記、attribution 上限規律。

## 0. 一言でいうと

収集した記事を**その都度、事象 (event) 単位に群化**し、群ごとに LLM が 1 本の
「ニュース」を精製する。新しい記事が既存の群に加われば**決定論の判定**でニュースを
更新する。読み手は「記事の羅列」ではなく「ツールが生成し維持するニュース」を読む。

動機: 現在は 1 記事 = 1 要約の列挙。同一事象の別報道が N 本並び (冗長)、各要約は
1 記事分の文脈しか持てない (深さ不足)。**両症状は「処理単位が記事」という同一原因**。

## 1. 実測根拠 (2026-08-22〜23、read-only 試作 + E0)

| 実測 | 数字 | 含意 |
|---|---|---|
| **E0 供給量 (30 日)** | 母集団 4,486 記事 → 複数記事群 212、**独立 2 媒体以上 191 件/月**、3 媒体以上 79、official/research 含み 67 | **供給は十分** (合格線 50/月。較正格子は 3.7 件/月で死んだ — 同じ死に方はしない) |
| 群の全メンバー共有 entity | 201/212 = 95% | §5 のアイテム不変条件は現実的 |
| 最大群 | 19 記事 (単連結の連鎖は実在するが限定的) | メンバー上限 12 で 1 群のみ分割 |
| 生成試作 (26B、7 群) | 12-23 秒/群。捏造 0 / 相違検出は本物 (crate 版数の食い違い等) / 識別子破損 1/61 | 散文は成立、識別子は関門必須 |
| 独立性の欠陥 | Sputnik×2 群が「相違なし」= 裏取りに見えた | 件数を裏取りとして出すのは禁止 (§8) |
| 意味 dedup cluster tier の破棄 | 40-50 件/日 (tier=cluster を grep で分離済み。hard/intra_batch は別掲) | v2 の拡張候補 (§8b)。**v1 では裏取りに算入しない** |

⚠ 試作の群化実測 (29 群誤結合 0) は**固定窓バッチ**であり、本番のインクリメンタル
適用とは別アルゴリズム。誤結合率は移らない — E3 はリプレイの逐次適用でのみ測る。

## 2. 位置づけ: 3 層の責任分界

```
[situations 状況台帳]   分析的状態 (仮説・ACH・予測)。months 単位     ← 既存
        ↑ 供給 (一方向。状況が事象を再構成することはない)
[event_items 事象]      読ませる単位。機械が束ね、LLM が書く。days〜2週  ← 本設計
        ↑ 群化 (決定論 + 埋込)
[articles 記事]         収集の原子 = 証拠。不変                        ← 既存
```

- **一方向**: 上位層から下位層への書き戻し・フィードバックは行わない
- 生成文をパイプラインへ**再入力しない**: 埋込・dedup・entity 抽出・triage・台帳の
  証拠のいずれにも入れない
- **import 関門は allowlist (default-deny)**: `src/eventnews/` を import してよいのは
  eventnews 内部 + `scripts/` のみ (将来 UI API を追加するときに allowlist へ 1 行足す)。
  denylist 方式は還流経路 (search/assistant/digest/spotlight/pir/forecast) を
  数え漏らす — `test_burst_boundary` の allowlist 版と同型のテストで固定
- **既存の同一事象機構との関係** (レビュー A M1): `status='skipped_duplicate'`
  (identity_dedup で投稿抑制された記事。articles 行 + entity あり) は**群のメンバー候補に
  含める**。`articles.dedup_key` (LLM 提供の自由文キー) は結合信号に**使わない**
  (誤結合リスク。将来検討は §8b)

## 3. 生成物と原ソースの区別 (構造要件)

事象ニュースは N→1 で**どの原文にも書かれていない統合主張**を生む。区別は構造の要件:

1. **表示原則**: 「kuebiko が生成」を明示。構成記事を必ず全件列挙 (折りたたみ可・省略不可)。
   文言は「**独立 N 媒体が報道 / 本文は上位 8 件から生成**」(読んだ件数と報道件数を分離)
2. **報じられた事実の行には必ず [N] 参照** (structured の source_index)。節名は
   「確認された事実」でなく「**報じられた事実**」(確認は原文が確認と書いた場合のみ)
3. **裏取りの語の規律**: 「裏取り」を名乗れるのは official/research tier の一致のみ
   (`source_basis._CORROBORATION_MIN_TIER` の既存規律)。news 層の広がりは
   「**独立 N 媒体 (国営 M・未分類 K)**」— 3 値表示。**未分類を 0 と見せない**
4. **エクスポート (STIX/IOC) は原文由来の entity 層のみ**。生成散文を出さない
5. **帰属確度の上限規律** (レビュー B G1): アイテムの `best_attribution_basis` を
   コードが決定論で付与 (メンバーの source tier から)。生成文はメンバー最強 basis を
   超える断定をしない — プロンプトで指示し、「確認」等の断定語はベンダ/政府確認が
   原文にある場合のみ許す
6. 将来の検索/想起でも生成物と原文を混ぜて返さない (§2 の allowlist が構造で担保)

## 4. 時刻の 5 分類と錨

| 時刻 | 定義 | 用途 |
|---|---|---|
| **first_reported_at** | 構成記事の錨時刻の最小 | ⭐ **集計・trend・重要度系の唯一の錨** |
| last_reported_at | 同・最大 | 「動いているか」判定 |
| event_date | 記事の event_date/compromise_date (あれば) | 表示のみ |
| generated_at | 現行版の生成時刻 | 監査用。錨に使わない |
| updated_at | 新ソース到着時刻 | 既読差分・**到着順表示 (「到着順」とラベル) のみ可** |

- 錨の SSoT: `_EVENT_TS_EXPR` **と `_DEDUP_ARTICLES` の両方** (articles は同一
  article_id が複数行ありうる — fan-out を畳まないと媒体数が水増しする) を
  **共有モジュール `src/storage/event_time.py` へ昇格**し、repo_synthesis と event 層の
  両方が import する (踏襲=複製 の芽を先に摘む。錨式のリテラルが 2 箇所に無いことをテストで固定)
- **候補検索の述語も錨で切る**: `article_embeddings.created_at` は DB 書込時刻
  (バックフィルで動く) — **窓判定に使わない**。articles へ join し錨時刻で切る
- 用途の二分 (レビュー A M6): 集計/trend/バースト = first_reported_at 必須。
  読み手の到着順 = updated_at 可 (ラベル付き)。**ORDER BY 関門**: 集計・重要度経路の
  SQL に independent_sources / メンバー数 / updated_at が現れないことをテストで固定

## 5. 群化規則 (v1 は学習なしの決定論)

**母集団述語** (SQL レベルで固定 — リプレイの再現性の前提):
`importance IN ('high','medium') AND status IN ('posted','skipped_duplicate')`、
埋込は url join (`article_embeddings.url = articles.url`)、article_id は畳み込み済み。

**結合エッジ**: 埋込 cos ≥ 0.70 **かつ** entity 共有 ≥ 1。
- 採用 entity: `cve` / `victim_org` (**`normalize_for_match` で正規化して比較** —
  free-form のため) / `actor` (言及層。ただし §8 のとおり裏取り計算には使わない) /
  `malware_family`。**`actor_provisional` は明示的に除外** (汚染事故の前科)。
  `campaign` は v2 検討 (§8b)
- 頻出ガード: 窓内 12 記事超に出現する値は結合信号に使わない
- 窓: 錨時刻が既存アイテム last_reported_at から 72h 以内 (rolling)

**連鎖制御** (レビュー B D1 — 2 信号でも entity 乗り換え連鎖は起きる):
- **アイテム不変条件: 全メンバーが共有する entity が 1 つ以上存在すること**
  (参加時に検査。E0 実測で既存群の 95% が満たす — 群化率の犠牲は 5%)
- **メンバー上限 12**。超過したら新アイテム (「第 2 部」) を起こし `related_to` で
  弱リンク (E0 実測で影響は 212 群中 1 群)
- **複数アイテムに同時マッチ**: 最高 cos の 1 アイテムのみに参加 (同点は
  first_reported_at の古い方)。**自動併合はしない** (`merged_into` は手動/将来用 —
  橋渡し併合の連鎖を v1 では構造的に持たない)
- dormant アイテムへの再参加は厳条件 (entity 共有 ≥ 2 ∧ cos ≥ 0.80) のみ (第 1 部・
  第 2 部が無関係な 2 アイテムに割れるのを緩和)

**帯域制限の自覚** (レビュー A H5): 群化母集団は意味 dedup (cos ≥ 0.79/48h) の
**残余**であり、最も似ている別報道は原理的にメンバーに含まれない (タイトルと URL しか
残らない)。相違検出は [0.70, 0.79) + 48h 超の帯での測定である。dedup 閾値の変更は
群化の挙動を無言で変える — pipelines.yaml の閾値と本設計の結合を明記しておく。
**⭐群化率は目標関数にしない** (窓延長・ガード緩和・閾値低下で稼がない)。

## 6. 同一性・版管理 (アクター辞書 identity 8 原則の移植)

- **id 不変**。自動併合なし (v1)。分割 (メンバー上限超過) は新 id + related_to
- **版は履歴に残す**。上限 20 版/item、ただし **v1 (初版) は常に保持** (訂正の
  追跡可能性の根拠。20 回の更新で初報が消えては訂正履歴の意味がない)
- **既読差分はプロセ差分から作らない** (レビュー B F3 — 再生成は非決定で差分の大半が
  言い換えになる)。`event_item_versions.new_facts_json` に **決定論の差分** (追加
  entity / 追加 CVE / tier 上昇 / 新メンバー) を書き、UI はそれを表示する。
  プロセ差分は監査用
- 失効: last_reported_at から 14 日で dormant

## 7. 状態機械 (新規 / 更新 / 補強) — 判定はすべて決定論

| 状態 | 条件 | 再生成 | 通知 (cutover 後) |
|---|---|---|---|
| **new** | 初回生成 | する | する |
| **updated** | ①駆動 entity (cve / victim_org 正規化後 / actor) の新規追加 ②**独立媒体数の増加** ③**best tier の上昇** (news→research/official) ④importance 上昇 | する | 重要時のみ |
| **reinforced** | 新メンバーが加わったが ①-④ のいずれも起きない | しない (ソース欄に 1 行) | しない |
| dormant | 14 日新着なし | — | — |

- 条件②③はレビュー B G2 の反映: **単独報 → 政府/研究一次が確認、は CTI で最重要の
  状態遷移**であり、「事実が増えない」ことを理由に黙殺してはならない
- **駆動 entity から ioc_* / tool / ttp を除外** (レビュー B D3 — LLM 付与の揺れで
  「新 entity」がほぼ常に真になり、reinforced が発生しなくなる)
- **数値矛盾の検出は v1 では採用しない** (レビュー B D4 — 「約 2 万件」vs「19,800
  records」を偽の訂正にする)。`change_kind='correct'` は CVE の置換 / actor の変更のみ
- **`current_version = 0` (生成失敗) のアイテムは状態に関わらず無条件に再生成対象**
  (reinforced で永久に body 無しになる穴を塞ぐ)
- importance は max 集約のまま。**下降しない縮退は既知** — UI で「初報時 high」と
  時制を付ける (v1 は評価レポートに注記)
- **事前登録**: リプレイでの reinforced 比率 ≥ 30% を期待値とする。下回れば
  updated 判定は装飾 (準トートロジー) — §13 E4′

## 8. 独立媒体数

- **媒体同一性のキーは `source_basis` の skey 規約 (feed_url 優先) を import** —
  feed_title は UI で改名でき、URL ホストはアグリゲータで媒体を表さない。
  新しい正規化関数を書かない
- 国営判定: `classify_source_tier` (既存 SSoT)。**着手時に
  `config/sources/source_reliability.yaml` の state_media へ中国国営
  (globaltimes.cn / xinhuanet.com / news.cn / cgtn.com / chinadaily.com.cn / ecns.cn)
  を追記する** (レビュー B F1 — 現状ロシア系のみで、中国国営 3 社の同時展開が
  「独立 3 媒体 (国営 0)」と表示される。SSoT への追記であり複製辞書ではない)
- 表示は 3 値: 「独立 N 媒体 (国営 M・**未分類 K**)」。列は
  `state_media_count NOT NULL DEFAULT 0` + `unclassified_sources NOT NULL DEFAULT 0`
  (NULL が 0 と読まれる二重の嘘を型で防ぐ)
- 算出 SQL は畳み込み済み派生表 (§4) の上で行う

### 8b. dedup 破棄の裏取り算入 — **v1 では実装しない** (両レビュー一致の NO-GO)

cluster tier 破棄 (40-50 件/日) を裏取りに数える案は以下の理由で**分離・後置**:
- 破棄には通信社再掲が混ざり、**近重複から独立性を製造する** (Reuters 1 本が
  「独立 4 媒体」になる)。2026-06-10 に実害付きで棄却された推論の再導入
- 新規テーブルに過去データが無く、リプレイで検証不能 — 未検証のまま点火される
- v1 でやるのは**記録の蓄積のみ**: `dedup_semantic_skips` (tier / feed_title /
  feed_url / matched_kind 列つき) に追記。**書込は orchestrator の既存
  `not dry_run` ブロック** (filters.py は record を返すだけ — dry-run 汚染と
  署名破壊を避ける。レビュー A H3)。retention 90 日 (dedup と連動)
- **2 週間蓄積後に別評価** (算入するとしても cluster tier のみ・独立性検査つき)。
  この期限を過ぎて評価しないなら、テーブルは write-only として削除する

## 9. 生成と識別子関門

- LLM: Step `EVENT_NEWS` (fast tier、think=False、temperature 0.2)。ハードコード禁止
- プロンプト: `prompts/eventnews/refine.j2` (直接組み立て。composer 非統合)。
  **`file_catalog.PROMPT_CATALOG` へ登録 + 網羅テストの件数更新が必須** (レビュー A M3)
- **出力は structured** (レビュー A M9 — 散文の行パースは脆い):
  `headline / bluf / facts: list[{text, source_index: int = 0}] / discrepancies /
  unknowns`。**`int | None` は使わない — 0 を未指定の番兵に** (2026-08-22 の確立解)。
  散文は表示層で組む
- **[N] 関門は facts のみに適用**。discrepancies / unknowns は [N] を要求しない
  (レビュー B E1 —「初期侵入経路はどの媒体も特定していない」という**不在の主張**は
  原理的に [N] を付けられず、関門が最も価値ある節を殺す)。識別子関門は全節に適用
- **識別子関門 (2 段)**:
  1. 前段: CVE/IP/domain/hash は **`ioc_extractor` を再利用** (refang 込み)、
     version/CVSS は新規 regex、アクター名は辞書 (`actor_normalizer`) 経由で
     構成記事から決定論抽出し、「使ってよい識別子一覧」としてプロンプトに渡す
  2. 後段: 生成文の識別子を**語境界を見る照合**で検証。
     **`normalize_for_match` は使わない** (ピリオド・ハイフンを落とし部分文字列 `in`
     で照合する = `UNC70 ⊂ UNC7005` をそのまま再現する。レビュー A H4)。
     新規純関数 `src/tools/identifier_match.py` (NFKC + casefold + defang 正規化 +
     トークン境界)。**照合単位は source_index が指す 1 記事** (全メンバー和集合との
     照合は cross-member 転植 — 別記事の CVE が別製品に付く — を素通しする。
     レビュー B C1、evidence_verify の「全断片が同一 haystack」と同じ論理)。
     不一致は一意候補への解決を試み、解決不能なら**識別子だけ「(原文参照)」に置換して
     行は残す**
- source_index が 0 または範囲外の facts 行は落とす。**落下行数・置換数は版に永続化**
  (`dropped_lines` / `repaired_ids`) — 消費者は §13 E2′ の評価レポート (v1) と
  週次監査 (cutover 時)
- 照合不能 (本文 purge 済) は保持。verified_at を版に記録。再検証はしない
- 生成失敗は fail-open (§7 の version=0 再試行)
- **mask_text は適用しない**: 入力は公開記事のみで機密の混入経路が無く、
  mask_text はメールアドレス (CTI では一級 IOC) を潰す (レビュー A L1)。理由ごと記録
- プロンプトへ渡すメンバーは上限 8 件 (tier・時刻で選抜、「他 N 件」明示)

## 10. 重要度と PIR 背骨の非干渉 (CLAUDE.md §7)

- importance = 構成記事の最大値 (集約のみ)。**群サイズ・媒体数を importance に
  反映しない**。媒体数は §8 の 3 値表示として独立
- 単独初報 (独立 1 媒体・未裏取り) は I&W として最重要でありうる — 「1 媒体のみ」の
  明示表示を必ず持ち、**多媒体アイテムに並び順で埋もれない** (§4 の ORDER BY 関門)
- event_items は triage / routing / 記事選抜 / synthesis 証拠から読まれない
  (§2 の allowlist 関門が構造で担保)

## 11. データモデル (dual-backend: `schema_sql.py` の `_SCHEMA_SQL` + `pg_schema.py`)

```sql
event_items          id TEXT PK / origin TEXT NOT NULL ('live'|'replay')   -- リプレイ行の本番混入防止
                     / first_reported_at / last_reported_at
                     / status ('new'|'updated'|'reinforced'|'dormant') / change_kind ('add'|'correct'|NULL)
                     / current_version INTEGER NOT NULL DEFAULT 0
                     / merged_into TEXT NULL / related_to TEXT NULL
                     / importance / best_source_tier TEXT
                     / independent_sources INTEGER NOT NULL DEFAULT 0
                     / state_media_count INTEGER NOT NULL DEFAULT 0
                     / unclassified_sources INTEGER NOT NULL DEFAULT 0
                     / created_at / updated_at
event_item_versions  item_id / version / generated_at / model / prompt_version
                     / headline / body_json TEXT   -- structured 出力 (散文は表示層で組む)
                     / new_facts_json TEXT         -- 決定論の版差分 (§6)
                     / verified_at / dropped_lines INTEGER / repaired_ids INTEGER
                     / PK(item_id, version)
event_item_members   item_id / article_id / joined_at
                     / contributed_new_facts INTEGER NOT NULL DEFAULT 0   -- BOOL は使わない (dual-backend 規律)
                     / join_signal TEXT / PK(item_id, article_id)
dedup_semantic_skips skipped_url / skipped_title / skipped_host / feed_title / feed_url
                     / tier TEXT NOT NULL / matched_kind TEXT / matched_key TEXT / ts
```

- PG の追記位置は既存実践に従う (新規テーブル + index はファイル末尾へ)
- SQL は可搬形 (bare GROUP BY 禁止 / ROW_NUMBER のみ)
- retention: versions は 20/item (**v1 は常に保持**)。skips 90 日。items は purge しない

## 12. v1 の範囲 (shadow) と非範囲

**v1**: 群化 + 生成 + 状態機械 + 識別子関門 + 独立媒体数 (3 値) + 版管理を
モジュール + スクリプトで実装。**本番配信・スケジューラには接続しない**。
評価はリプレイ (過去 10-14 日を日次逐次適用。origin='replay') + 実 LLM 生成 +
識別子全数検査 + **現行 per-article 要約との並置**。リプレイ窓の上限は 90 日
(embeddings の retention 連動)。

**v1 でやらない**: UI タブ / Discord (初報のみ・生成物明示、の方針だけ固定) /
JobDef 登録 / 既読管理 (new_facts_json の器だけ) / ML 群化 / §8b の裏取り算入 /
数値矛盾検出。

## 13. 事前固定する合否基準 (v2 — 検出力のある形へ全面書き直し)

v1 の E1/E2/E5 は「関門のコードが仕様どおり動いた」しか測れない同語反復だった
(両レビューが独立に指摘)。**関門をすり抜けた誤りと、関門の副作用の両側**を測る:

| # | 基準 | 合格線 |
|---|---|---|
| E0 | 供給量 (独立 2 媒体以上の群/月) | **✅ 済み: 191 件/月** (合格線 50) |
| E1′ | 関門**前**の識別子破損率 / **注入試験** (故意に壊した識別子 + cross-member 転植を関門が捕捉する率) | 破損率の実測記録 + 捕捉 100% |
| E1″ | 識別子の置換率 (置換だらけ = 読めない本文でも「破損 0」は合格してしまう) | ≤ 10% |
| E2′ | facts の落下行率 + **落下行の全数目視で「落として妥当」の率** | ≤ 15% / ≥ 80% |
| E3 | リプレイ**逐次適用**の最終群を全数目視: 誤結合 0 **かつ** 分割 (同一事象が別アイテムに割れた) 件数の記録 **かつ** 矛盾主張の統合丸め (「悪用未確認」が「確認」に反転する型) の検査 | 誤結合 0。分割率と丸めは初回測定で基準化 |
| E4 | **対照付き判定**: 同日同記事の【現行 per-article 要約列】vs【事象ニュース】を並置し利用者が判定 | 利用者判定 |
| E4′ | reinforced 比率 — **既に 2 媒体以上あるアイテムへの合流に限る** (2 件目の合流は構造上必ず first_corroboration=updated であり、全 join 比率は群サイズ分布から天井 28% — 当初の「全体 ≥30%」は到達不能な検定だった。2026-08-23 リプレイで判明し定義を修正) | ≥ 50% |
| E5′ | 合成テスト: 同一国の国営 3 媒体の群が「国営 3」表示 / 未分類 K が 0 と表示されない | pass |
| E6 | 費用 | ≤ 5 分/日 (26B) |

**失格条件**: 束ねた本文が個別要約より読みにくい (E4 利用者判定) / 更新が追えない /
誤結合が出る (2 信号規則は緩めない) / **群化率・独立媒体数は目標関数にしない**。

**期限** (2R-H7 の教訓 — 期限の無いシャドーは「シャドーのまま忘れる」が既定平衡):
v1 評価レポート提出から **4 週以内**に cutover / 凍結 / 撤退を判断する。
判断材料が無ければ凍結 = §8b のテーブルも削除する。

## 14. 既知のリスク台帳

| リスク | 対処 |
|---|---|
| 生成時刻を集計が拾う | §4 不変条件 + 集計は articles のみを読む + ORDER BY 関門 |
| 関門が黙って事実行を落とす | dropped_lines/repaired_ids を版に永続化 + E2′ + cutover 時に週次監査へ登録 (3 点セット) |
| dedup_semantic_skips の書込失敗 | orchestrator 側 fail-open + 日次で skip イベント数と行数を突合 (cutover 時) |
| Grok テンプレ類似の誤結合 | 2 信号 + 共通 entity 不変条件。E3 で確認、駄目なら Grok 除外 (dedup と同じ前例) |
| 本文 purge 後の再検証不能 | 書込時検証のみ、verified_at 記録 |
| cos 0.70 は既存較正では「同トピック」の域 (followup=0.88 が「同 incident」) | 共通 entity 不変条件 + E3 の丸め観点 + §3-5 の断定上限。それでも誤結合が出れば 0.88 側へ寄せる |
| importance が下降しない | 既知の縮退として受容。UI で時制表示 (§7) |
| 同型経路の規律移植漏れ | 時刻錨 = event_time.py を import (複製検出テスト) / 識別子関門 = identifier_match.py の単独所有 / narrative 列挙テストへの追加は positive/negative パラメタを分離してから (レビュー A M4) |

## 14b. 決定記録 (2026-08-23、実測に基づく利用者判断)

| 決定 | 内容 | 根拠 |
|---|---|---|
| **モデルは narrative tier (31B)** | fast(26B) から変更 | 同一入力・同一プロンプトの A/B で **26B は 5 事象中 3 件の日本語破損** (中国語・ラオ文字の混入、「2,300 件」→「2,30 封」)、**31B は 0 件**。内容精度も 31B が上 (被害 9 社の件数内訳、pinyin スラング "dajiba" 等の帰属証拠まで拾う)。費用は 65 秒/回 × 0.25 回/時 = **16 秒/時**で毎時運用に収まる |
| **カタログに `proper_noun` を載せない** | 検出専用にする | 26B が「使ってよい識別子」を**企業名リストと誤解して羅列**した (`McDonald、TCS、BEC、AI、CEO、PDF、SECURITY…`)。カタログは実値を提示するので、緩い型を入れると誤用される |
| **単独記事は生成しない (案 A)** | 既存の per-article 要約を同じ枠で表示 | N→1 の統合価値が N=1 では無い / 2 回目の LLM は破損の機会だけ増やす (今日の破損はすべて 2 回目の生成で発生)。単独記事に必要なのは文章でなく**枠**(「1 媒体のみ・未裏取り」の明示) で、これは LLM 無しで出せる。全件生成 (26B) は +21 分/日で可能だが不採用 |
| **生成・更新は毎時** (収集サイクルと同期) | 日次でなく毎時 | 利用者要件。生成頻度は 0.25 回/時 (10 日で 59 回) なので費用は問題にならない |
| **中国国営メディアの state_media 追加を残す** | 事象ニュース外へも波及することを承知で採用 | 中華系 APT を主敵とする本ツールで Global Times が `news` 扱いだったのは欠陥。`classify_source_tier` を呼ぶ 10 モジュール (synthesis / spotlight / 朝ブリーフ / PIR daily focus / 裏取り計算ほか) の挙動が変わる。直近 30 日の新規該当は GlobalTimes 1 feed |

**毎時運用に伴う実装要件** (v1 では未実装だったもの): runner はリプレイ専用でメモリ上に
全アイテムを再構築していた。毎時運用では ①窓内の live アイテムを repo から復元
②前回以降の記事のみを候補にする ③二重メンバー化の防止、が要る。リプレイと本番で
同一コードを使う構造は維持する (初期状態を引数で渡す形にする)。

## 15. 独立レビュー台帳 (2026-08-23、2 系統並行)

**レビュー 1 (実装整合性、opus)**: 条件付き GO。HIGH 6 件 — H1 `_DEDUP_ARTICLES` 欠落
→ §4 反映 / H2 skips スキーマの tier・媒体欠落 → §11 / H3 「additive」不成立
(dry-run 汚染) → §8b / H4 共有モジュール指定の実現不可能性 (`normalize_for_match` は
識別子照合に不適) → §9 / H5 群化帯域制限の不記載 → §5 / H6 E1/E2 検出力ゼロ → §13。
MED 11 件 (M1 dedup_key 関係 → §2、M2 母集団述語 → §5、M3 PROMPT_CATALOG → §9、
M5 state_media_count write-only → §8 で SSoT 追記により解消、M6 錨と到着順の緊張 →
§4 二分、M7 結合 entity の語彙規律 → §5、M8 dual-backend 型 → §11、M9 structured 化 →
§9、M10 join 経路と embeddings.created_at の罠 → §4/§5、M11 retention → §8b)。

**レビュー 2 (失敗様式、opus)**: 条件付き GO。§8 の裏取り算入のみ NO-GO → §8b で分離。
A1-A5 検定の書き直し → §13 / B1-B3 Goodhart → §8b・§5 / C1-C6 自己ループ → §9・§4・
§2 / D1-D8 状態機械の縁 → §5・§7 / E1-E4 静かな失敗 → §9・§14 / F1-F5 読み手への嘘 →
§8・§6・§3 / G1-G4 tradecraft → §3・§7・§14 / H 供給測定 → §1 E0 で実施 (合格)。

**採用しなかった指摘**: なし (全 HIGH を反映。MED の一部は v2 送りを明記 — §8b・
数値矛盾・campaign 信号)。

**レビューの総括所見**: 「較正格子と失敗の型が違う (フィードバックループが無い) ので
同じ死に方はしない。ただし N→1 統合は『統合した結果が良く見える』方向へ本質的に
バイアスする — E3/E4 の目視判定は設計者自身が単独で行わない」→ E4 は利用者判定
(対照付き) として固定した。

## 15. 本番不発の記録 (2026-08-24)

毎時運用に移行した初日、**生成が 1 件も出ないまま毎時 succeeded を返し続けた**。
利用者の「更新されていないのでは」という指摘が唯一の検知手段だった。

### 15.1 二つの故障

| # | 症状 | 原因 | 対処 |
|---|---|---|---|
| A | 既存アイテムの復元経路だけが落ちる (`expected 11, got 8`) | PG は行を dict で返すため、別名の無い `COALESCE(...)` 4 列が同一キーへ潰れる | 全列に別名。読み出しを位置からキー名へ |
| B | 合流が一度も起きない | 結合信号 entity を `article_entities.created_at >= 6h` で絞っていた。窓内の既存メンバーは最大 72h 前なので **entity が常に空** → 「共有 entity ≥1」が永久に不成立 | entity は **article_id で引く**。頻出ガードの分母のみ窓内コーパスで数える |

A は初回だけ成功して見えた (復元対象がゼロだったため)。B は最後まで例外を出さない。

### 15.2 教訓

1. **評価と本番で入力の組み立てが分かれていれば、`process_candidates` を共有しても
   挙動は一致しない**。リプレイは entity を時刻で絞らず全件引いていたため、
   評価では合流し本番でだけ起きなかった。共有すべきは処理だけでなく **取得** である。
2. **`article_entities.created_at` は行を書いた時刻であって事象時刻ではない**
   (バックフィル・再抽出は過去記事へ当日の日付を書く)。2026-08-22 の時刻錨と同型。
3. **「毎回 succeeded」は「機能している」ではない**。出力ゼロのまま成功し続ける経路は
   外形監視では検出できない → 製品鮮度 dead-man
   (`source_health._PRODUCT_FRESHNESS_LIMITS`) に事象ニュースを登録し、
   2 日ゼロで日次 heartbeat に ⚠️ を出す。

### 15.3 実測 (2026-08-24 時点、直近 72h)

| 指標 | 値 |
|---|---|
| 候補 (high/medium × posted/skipped_duplicate) | 320 |
| うち埋込あり | 220 (欠落 100 のうち 98 は X の投稿 = 記事ではないため除外は正しい) |
| うち結合信号 entity あり | 156 |
| 結合条件 (cos ≥0.70 かつ 共有 entity ≥1) を満たす組 | 11 |

期待収量は **3-4 件/日**。dead-man の閾値 2 日はこの実測に基づく。
