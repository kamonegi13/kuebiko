# アクター行動史 (actor_observed_profile) + identity ライフサイクル設計

**Status**: Phase1 + Phase2 実装済 (2026-07-26)。設計議論の経緯はメモリ
`actor_dictionary_design_2026_07_26` を参照。

## Phase2 (F1/F3/F5/F6、2026-07-26 実装)

- **F1 PIR 辞書補完**: PIR「攻撃主体」入力に辞書 datalist 補完 + 解決チェック表示
  (`PirEditPage.tsx`)。evaluator の双方向 word-boundary 照合のクライアント近似で、
  解決されない名前 (タイトル一致のみに縮退する) を警告色表示。**評価ロジックは不変**。
- **F3 報道由来 alias 収穫** (`src/cti/news_alias_harvest.py`): aka/also known as/別名 の
  併記構文から既知アクターの未収録別名を検出し `news_alias` 提案として人承認キューへ
  (mitre-actor-sync に週次相乗り)。防御 5 重: ①併記構文限定 ②直前 80 字に辞書アクター
  ③**帰属妥当性ガード** (アクター名〜マーカー間に別の大文字語 = 未知主体の可能性が
  あれば帰属しない。実データの Lyceum/マルウェア列挙誤帰属の対策) ④一般語 SSoT 除外
  ⑤既知名除外。承認時は alias 追加+有界再帰属が発火。dedup_key は status 問わず
  再提案抑止。**identity 昇格は人承認のみ・収集還流はここまで** (自動ソース提案は再建しない)。
- **F5 alias 使用統計** (`actor_alias_usage` テーブル): 取込時の本文照合で発火した
  名前を記事単位 PK で記録 (run 横断重複は二重計上しない)。配線 =
  `registry.matched_names_for` → briefing metadata → persistence。辞書詳細の
  「名前の照合実績」に累計表示 (死に alias の整理判断材料)。purge 対象外。
- **F6 死にフィールド整理**: ambiguous/context_cues を UI 編集可能化し、一般語ゲート
  (generic_alias_words SSoT) を **保存時検証に昇格** (07-21 再発ループの UI 側防御)。
  消費者ゼロの origin は編集面から撤去 (yaml の既存値は温存・loader は無視)。

## 目的

辞書 (`config/actor_aliases.yaml`) は identity の永久器だが、**行動史は蒸留されて
いなかった** — 脅威アクターページは 90 日窓のビューであり、retention (body/embedding
90 日 purge) の外に「このアクターは何をしてきたか」の永久記録が存在しなかった。
本設計は観測 (subject 記事) → 知識 (月次期間行) の**決定論射影** (LLM 不使用) で
これを埋める。weekly 深掘り再設計 (2026-07-20) の「選定は取込時判断の決定論射影」
原則の適用。

## 表示の時間軸棲み分け (確定)

- **恒久史 (月次×永年)** = アクター辞書詳細 (`ObservedHistory`) — 月次タイムライン +
  関連情勢台帳 (F2 逆引き)
- **直近 90 日精密観測** = 脅威アクターページ (従来どおり)。sparkline 埋込カード
  (`MonthlyHistoryStrip`) + 辞書カードへの深リンクで往復
- 「ID 一つ・レンズ二つ」原則の時間軸拡張。観測統計を辞書 yaml に焼き込まない /
  知識を脅威ページで編集させない、は不変

## identity ライフサイクル 8 原則 (2026-07-26 確定)

1. **id は不透明な永久キー** — rename 全面禁止。呼称変更は表示層で吸収
2. **merge = 辞書 yaml の redirect 墓標** — `status: merged / merged_into / merged_at /
   merge_note / moved_aliases`。alias は継承先へ物理移動 (墓標は alias 0 件が不変条件)。
   identity SSoT は yaml (人所有層)、DB に identity を書かない (3 層統治の相互不可侵)
3. **解決は `resolve_actor_id()` 一本** — 保存データ (記事行・月次行・anchors) を読む
   経路はこの seam を通す。チェーン追従+循環ガード。redirect 0 件の間は恒等関数
4. **保存済み行は不改変** — merge で過去行の id を書き換えない。合算は表示時のみ
5. **月次行は射影 — 三分法**: ①定常ジョブは当月+前月のみ / ②上流訂正時の窓内再蒸留は
   正当 (行は判断記録ではない) / ③再計算不能域の行は「当時の identity モデル下の観測
   記録」として立つ (観測≠世界の時間版)
6. **split は前向き fork のみ** — 後ろ向き分割は原理的に不可能 (集計が記事帰属を破壊済み
   + body 90d purge)。分化判明時は新 id を起こし、旧 id は傘の墓標として史を保持
7. **運用則: 同一性が不確かなら別 id で開始し、確証後に merge** — merge 安価 / split
   不能の非対称性からの帰結
8. **merge 実行 UI は delayed** — seam (loader 許容 + resolver + guard test) のみ実装済。
   最初の merge は辞書の構造化編集で機能する

## スキーマ

`actor_observed_profile(actor_id, month PK)` — month は **'YYYY-MM' (JST 境界、不変)**。
列: subject_articles (distinct article_id) / distinct_sources (distinct feed_url —
ransomware.live 型の量的支配を史の上で区別) / sectors・countries・malware・ttps・
campaigns (counts dict、PG=JSONB) / japan_targeted (japan_relevance SSoT) / kev_hits。
SQLite `schema_sql.py` + `pg_schema.py` 両定義 (parity test 対象)。purge 対象外 = 永久。

## 蒸留 (F7)

- 純ロジック: `src/cti/actor_observed_history.py` (`distill_month` — article_id dedup
  = run 横断重複の GROUP BY 教訓を内蔵)
- ジョブ: `actor-history-distill` (bespoke、月曜 04:20 JST、LLM 不使用・軽量)。
  service = `src/ui/services/actor_history_distill.py`
- **定常 = 当月+前月を再蒸留** (週次実行のため月末尾の数日が翌月初回まで未集計になる
  取りこぼしを前月再蒸留で回収)。月単位の**全置換** (stale 行を残さない)
- **初回 = テーブル空検知で 2026-07 (SUBJECT_EPOCH_MONTH) から全月 backfill**。
  主題判定層が 2026-07-17 稼働のため**それ以前は原理的に遡及不能**

## 承認時有界再帰属

新アクター承認 (`/actors/sync/proposals/{id}/approve` 全 3 種別) と手動編集での名称追加
(`POST /actors/{id}`) の時点で、**body 現存 (≤90 日) かつ新名称に LIKE 一致する記事のみ**
word-boundary 照合 → 言及 entity 付与 + **title 層のみ**の主題再判定 → 影響月を再蒸留。
LLM 補完層は取込時出力が失われており再現不能 (保守側)。既存 subject 判定は上書きせず
id 追加のみ。実装 = `src/ui/services/actor_reattribution.py`。失敗しても承認は成立。

## API / UI

- `GET /api/v1/actors/{id}/history` — 月次行 (redirect 表示時合算) + epoch からの
  ゼロ埋め series + 観測≠世界 note
- `GET /api/v1/actors/{id}/situations` — F2 逆引き (anchors `actor:<id>`、旧 id 込み)
- situation 深リンク: `situationHref()` → `/app/intel/synthesis#situation=<id>`
  (filters が ledger ビュー強制、LedgerView が該当カードを展開 — C-lite の着地点)

## 判定入力の永続化と retention (D1-D5、2026-07-26 深掘り)

辞書 = 永続蓄積+行動分析の器 / 脅威ページ = 現在、の一線を「集計の永続」から
**「判定入力の永続 = 再導出可能性」**へ深化させた再設計。4 層パイプライン:

```
層1 判定入力 (一次観測・永久) : title / LLM生出力 / 言及entity / victim列 / category
層2 判定 (導出値)            : subject_actor_ids = f(層1, 現在の辞書) — 辞書進化で再導出可
層3 集計 (決定論射影)        : actor_observed_profile 月次行 — 再蒸留可能
層4 表示                     : 辞書詳細の行動史 + 月行 drill-down (証拠開示)
```

- **D1 LLM 生入力の永続化**: articles.llm_primary_actor_raw / llm_primary_confidence
  (summarizer の routing_flags 出力、辞書解決前)。fill-rate 週次監査に登録 — 主題 311 件が
  全て title 層で **LLM 層寄与ゼロ**と判明しており (2026-07-26 実測)、層の死活を常設監視
  して枯れの原因 (供給/確度/ゲート) を切り分ける。保存開始以前の出力は真に喪失
  (provisional entity 69 記事が部分代替)。
- **D3 承認時再帰属の三パス化** (`actor_reattribution.py`): ①title 全期間 (title は永久メタ
  — 主題遡及の無制限化の本体) ②LLM 出力全期間 (保存済み raw を現在の辞書で再解決。
  confidence high/medium のみ。本文現存なら言及 word-boundary 検証、**purge 済みなら
  raw と名前の完全一致時のみ付与** — 転記の証拠とみなす保守則) ③body 窓 (言及 entity)。
- **D3+ epoch 拡張**: title 層は全史適用可能なため全 status の未評価記事へ title 層を
  backfill し (2026-07-26 実施: 18,951 件走査 / 392 件付与)、SUBJECT_EPOCH_MONTH を
  収集開始月 **2026-04** に拡張 (旧 2026-07)。2026-07-17 以前の月は title 層のみの基底
  (実測上 LLM 層寄与ゼロのため実質同一基底)。
  ※`scripts/backfill_subject_actors.py` は posted 限定 (PIR 用途) — epoch 拡張は全 status。
- **供給スイープ (恒久)**: ransomware.live 等の直接取込経路は briefing 永続化 (取込時の
  主題判定点) を通らず subject 未評価のまま残る — 実測で qilin の 2026-07 が 31→137 件に
  跳ねた正体。週次 actor-history-distill が蒸留前に **title 層スイープ** (直近 45 日の
  未評価記事へ決定論判定) を実行し、この経路の取りこぼしを恒久的に塞ぐ
  (`title_layer_sweep`)。
- **D4 本文 retention = GC-root 方式**: 永続記録から参照される記事の本文は purge しない
  (「推論の可視化が最終保証」の系)。root 5 種 = ①主題あり ②状況台帳の証拠採用
  ③日本標的 ④importance=high ⑤アクター言及/provisional。実測 25.3% / 年 ~250MB
  (和訳込み)。判定は purge 時点・記事単位 (run 横断の全行を見る)。実装 =
  `repo_dedup.purge_article_bodies_older_than`。
- **D5 月行 drill-down**: GET `/actors/{id}/history/{month}/articles` — 月次カウントの
  中身をライブ照会 (article_id を月次行に焼き込まない: 死にリンクの永久固定と二重管理を
  避ける)。蒸留は週次のため件数が一時的にズレうる — UI が注記で正直に示す。
- **articles メタ行 (title/url/summary/判定入力) は purge しない — 設計保証**。本文 90 日
  retention はメタに及ばない。将来メタ retention が必要になった場合は、主題記事の参照
  3 列 (article_id/title/url) を永続テーブルへ freeze してから消す (freeze-on-purge 方針)。

## 取込ゲート統一 + 証拠等級 (R1-R3、2026-07-26)

実機検証で発見した取りこぼし (slug 不一致 160 件 / 辞書外グループの提案不在 / 生まれつき
0% 被覆) の本質は 3 つ — ①分析が特定経路の副作用で不変条件が構造強制されていない
②帰属の証拠等級が未モデル化で最強証拠 (source 断言) に経路がない ③生まれつき欠けた
供給は急落監査に映らない。対処:

- **R1 取込ゲートの構造強制**: `determine_subject_actors` を全経路が通る単一判定点にし、
  ransomware_ingest もこれ経由に (add_article 書込点は 2 箇所のまま)。`tests/unit/
  test_article_write_seam.py` が「add_article 呼出 = allowlist のみ・各 writer は主題判定を
  経由」を恒久検査 — 次の source が迂回した瞬間 CI が落ちる (クラスを閉じる)。
- **R2 証拠等級 source > title > llm**: SOURCE_FEED (構造的断言) を第 0 層に追加。辞書に
  `source_slugs` 名前空間キーを新設 (mitre_group と同思想。prose 照合と別レイヤ = 一般語
  衝突する "akira" 等を構造化ソースの完全キー照合でのみ解決し、find/find_all は汚さない)。
  ransomware.live は解決グループを source 断言で主題直書き・未知は actor_provisional
  (人承認へ回す = raw 'actor' 生書き廃止 = recall layer の収穫網に乗る)。
- **R3 経路別供給監査**: アクター言及ありなのに主題被覆 < 5% の取込経路を週次 WARN
  (急落でなく絶対欠落を検知 = ransomware.live が 5 週沈黙した穴)。
- **遡及修復** (`scripts/repair_source_attribution.py`、実施済): source 断言遡及 255 件 +
  辞書外 actor entity → provisional 移行 648 件 + 未知グループ 106 種を提案キューへ (58 件)。
  実測改善: The Gentlemen 0→188 件 / akira 0→49 件 / ransomware 経路の主題被覆 0%→58%。
- **教訓**: 「分析がメタデータとして記事に残るか」は結論については杞憂だったが、その下に
  ①棄却された生入力の揮発 (D1) ②最強証拠 (source 断言) の未モデル化 (R2) ③経路不変条件が
  慣習でしか守られていない (R1) が実在した。「遡及不能」は本文 retention でなく判定入力
  未保存の帰結だった (title/LLM 生出力/source 断言は全期間遡及可能)。

## 本文主題型の回収 = 主題 focused 分類器 (2026-07-26)

「タイトルに名前がない本文主題記事」(LLM 補完層の担当) が主題化されていなかった問題の
根治。診断 (`scripts/diag_llm_primary_actor.py`) で summarizer の primary_actor_id が本文
主題型 cyber 記事で **0/25 (0%) 産出** と実証 — intent/editorial_stance/diamond/event_date
と同じ「過負荷 summarizer の末尾フィールド枯死」。ゆえに再解析 backfill は無意味 (枯れた
抽出器) で、実証済みパターン (focused 単機能分類器) への移設が本丸だった。

- **`src/cti/subject_actor_classifier.py`**: 単機能 LLM 分類器。**候補を本文言及集合に限定**
  し辞書ゲート+言及所属の二重ゲートをプロンプト構造に内蔵 (LLM は候補外を返せない =
  誤帰属が構造的に不可能・最悪の失敗が候補内の選び間違いに限定)。迷ったら none。
- **配線** (briefing.py `_maybe_classify_subject_actor`): 曖昧クラス (title にアクター名なし・
  本文に言及あり) に限って呼ぶ (~16 件/日 = summarizer の 3%)。結果は routing_flags.
  primary_actor_id を上書きし既存の determine_subject_actors 層 2 経路がそのまま拾う
  (単一判定点 R1 保持・D1 で永続化)。SUBJECT_ACTOR_LLM=0 で無効化。
- **実機検証**: summarizer 0/25 → 分類器で LAUNDRY BEAR 記事を russia_svr (high) 正答、
  曖昧記事は none 棄権 (過剰帰属なし)。
- **backfill の再定義**: 死んだ summarizer の再実行は無意味だが、**動く分類器を本文現存
  記事に流す backfill は有意** (今度は機能する抽出器を回すため)。フォワード運用で
  足りるか過去も遡及するかは LLM コスト (曖昧クラス数千件) と価値のトレードオフで判断。

## summarizer 枯死の抜本対策 — 統合判断分類器 (2026-07-26)

primary_actor 枯死の診断から、**症状 (個別フィールド) でなく病のクラス**へ対処した抜本策。

- **根本原因 (3 層)**: ①近接=schema 強制出力ゆえ末尾フィールドは空デフォルトで出る (長い
  summary 後の truncation + 力尽き) ②構造=18 フィールドに**抽出と判断の 2 タスクを多重化**
  した過負荷単一呼び出しで、判断系 (推論を要する) が枯死 ③病は 5 回発見されたのに**毎回
  そのフィールドだけ focused 化する対症**を繰り返し、LLM 呼び出しが 3〜5 個に増殖していた。
- **A/B 実証**: 判断を 1 呼び出しに統合しても充足・正確性が保たれる (analysis_axes が既に
  判断 3 種を統合済み=viable の証)。実データ目視で article_type (summarizer 100% breaking の
  暗黙枯死) が advisory/recap に正しく分類され、primary_actor (0%) が救済されることを確認。
  importance の分布退化疑いは**テストで否定** (summarizer と完全一致=健全、low 率は正当分布)。
- **実装**: `src/cti/judgment_classifier.py` が editorial_stance/intent/diamond/event_date/
  i_infra/article_type/subject_actor を **1 focused 呼び出し**に集約。briefing.py は
  stance/analysis_axes/subject の 3 呼び出しをこれ 1 つに置換。summarizer は抽出専用。
- **スコープ判断**: importance/category/victim は**抽出寄りで枯死していない**ため summarizer
  据え置き (triage/routing への波及リスク回避)。victim を統合に入れると過剰解釈でゴミ充足
  になることを実データで確認し除外。i_infra は決定論フロア (nisc_sector_for) が backstop。
- **確定教訓**: fill-rate 100% でも**分布が退化していれば暗黙の枯死** (article_type が典型)。
  「充足の有無」でなく「中身の正誤 (分布・実データ目視)」で検証する。診断スクリプト =
  `scripts/diag_llm_primary_actor.py` (枯死検出) / `ab_consolidated_judgment.py` (統合可否)。

## 境界 (再提案不可)

actor_nation 述語の再導入なし / 自動ソース提案の再建なし / MISP・OTX 不採用のまま。
source slug は照合 alias に足さない (名前空間キーは prose 照合層と分離)。
過負荷 summarizer に判断フィールドを足さない — **判断は統合判断分類器 1 つに集約**し、
summarizer は抽出専用に保つ (per-field focused 分類器を新たに増やさない)。
