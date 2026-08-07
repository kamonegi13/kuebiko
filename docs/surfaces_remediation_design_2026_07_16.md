# 評価サーフェス監査 (2026-07-16) — 発見問題の本質的処置設計

7 サーフェス監査 (synthesis / 脅威マップ / 重要インフラ / 国家情勢 / 脅威アクター /
将来予測 / PIR・Spotlight) で発見した問題への処置設計。**各問題について「対処療法」と
「本質対処」を明示的に区別**し、後者を設計する。実装前のレビュー用。

---

## 0. 総論 — 共通病因と処置原則

今回の発見は、有機的結合監査 (2026-07-12) で特定した 4 病因のうち 2 つの**再発**である:

| 病因 (既知) | 今回の再発箇所 |
|---|---|
| 供給網無監視 (作った軸が枯れても気づかない) | PMESII T/I-infra 軸 (6 週間沈黙・fill-rate 監査対象外) |
| 消費者・監視なしの追加 | weekly 製品の鮮度が dead-man 監視の対象外 (9 日間欠落に気づかず) |

処置原則 (この設計全体を貫く):

1. **供給は過負荷経路から外す** — LLM の tail フィールド枯死は「指示の強化」では治らない
   (intent 修復 2026-07-13 で実証済)。focused 分類器か決定論に移す。
2. **供給網と製品鮮度を常設監視に載せる** — 修理するたびに監視も登録する
   (監査規約 3 点セットの徹底)。
3. **人の判断は自動化せず、注意を誘導する** — 承認キューの滞留は自動承認でなく
   通知モデル (heartbeat/ops) への合流で解く。
4. **報告は状態の射影** (期間 = render 軸) **を完遂する** — weekly が評価と射影を
   兼務している残滓を解消する。

---

## 1. [HIGH] PMESII T / I-infra 軸の暗黒化

### 真因

- `pmesii_axes` は summarizer.j2 (per-article 要約 LLM) の出力に残っている。
  2026-07-13 の分析軸修復 (intent/technical/event_date → focused 分類器
  `src/cti/analysis_axes_classifier.py`) の**対象外**だった。
- 8 軸のうち出現頻度が最も低い T / I-infra が、プロンプト変更 (5/30 category 再編・
  6/7 Diamond 2 軸) を境に枯死。頻度の高い 6 軸は生き残った — **過負荷時に tail から
  枯れる**という intent 崩壊 (被覆 75→3%) と同一の故障モード。
- I-infra だけ 0 でなく 6 件/30日なのは feed_default (Dragos/ENISA 等) の決定論残滓。
  = **決定論供給は枯れない**ことの傍証。
- 検知されなかった理由 (二重の監視盲点): ① fill_rate_audit の 16 METRICS に pmesii 軸が
  未登録 ② PMESII カードの non_empty_count が「total>0」判定のため 98% 崩落を「非空」に
  数える。

### 対処療法 (採らない)

summarizer.j2 に「T と I-infra を必ず出力せよ」と追記する — 過負荷という根本原因を
残したまま優先度を口頭で上げるだけで、次のプロンプト変更でまた枯れる。

### 本質設計

**(a) 供給の再建 — 決定論フロア + focused 分類器**

- **I-infra は決定論を主にする**: `pmesii_i_infra := (victim_sector_canonical ∈ NISC CI
  セクター [SSoT: src/cti/nisc_sectors.py]) ∨ (CI キーワード照合 [SSoT:
  src/cti/keyword_match.py 系]) ∨ feed_default (既存)`。重要インフラ該当性はセクター
  写像でほぼ決まるため、LLM 判定は補助に格下げする。決定論は枯れない。
- **T / I-infra の LLM 判定は focused 分類器に移設**: `AnalysisAxesOut` に
  `i_infra: bool` / `time_axis: bool` の 2 フィールドを追加 (現 7 → 9 フィールド。
  focused の範囲内)。`briefing.py` の分析軸上書き箇所で summarizer の当該 2 軸を
  分類器値 ∨ 決定論フロアで置換。他 6 軸は summarizer 継続 (健全なものは触らない)。
- **決定 (2026-07-16 レビュー): T 軸は廃止、P-env (物理環境) は維持** — 実質
  「PMESII + 物理環境」の 7 軸に整理する。理由:
  1. T は歴史的に ~30% の記事に付与 = 何にでも付く軸はレンズとして弁別力がない。
  2. 6 週間の完全沈黙に人も分析も気づかなかった = 消費実態が事実上なかった。
  3. 時間次元は既に一級フィールドが担う (event_date / event_date_basis /
     compromise_date → 時系列 ACH・タイムライン・再浮上判定)。boolean の T は冗長な劣化版。
  4. フィールド削減は過負荷 (根本病因) の予防そのもの。
  - P-env は対照的に健全 (254 件/30日) かつ防衛 CTI で決定価値がある (海底ケーブル・
    物理サボタージュ・災害×サイバーのハイブリッド脅威識別) ため維持。
  - ドクトリン純度は基準にしない (I を infra/cyber に分割済みで既に PMESII-PT 原型では
    ない)。基準は決定に効くか。
  - 廃止の実装: **DB 列 pmesii_t は残す** (履歴保全・列削除はしない)。カード/地図フィルタ/
    summarizer プロンプト指示から撤去。fill-rate 登録は残 7 軸のみ。可逆 (表示復帰は
    フロント + プロンプトの revert のみ)。
  - よって focused 分類器への追加は `i_infra` 1 boolean のみ (8 フィールド)。

**(b) 監視の恒久化 (再発防止の本体)**

- fill_rate_audit の METRICS に `pmesii_t` / `pmesii_i_infra` を追加 (articles 列なので
  FillMetric 形にそのまま適合)。残り 6 軸も同時登録 (計 8 行追加、閾値は既存の
  急落判定を流用)。
- PMESII カード: `baseline_avg > 0 かつ current < 0.2 × baseline` で「供給劣化」バッジを
  表示し、non_empty_count から除外する (98% 崩落を「非空」に数えない)。

**(c) 暗黒期間の backfill**

- `scripts/backfill_axes.py` と同型の一回性スクリプトで 2026-06-01 以降の posted 記事に
  決定論フロア + 分類器を適用 (決定論部分は LLM ゼロで即時、LLM 部分は detached 実行)。

**検証**: 修復デプロイ後 7 日で両軸の週次件数がベースライン share (絶対数でなく
posted 比) に回帰すること。backfill 後に PMESII タブの baseline 比が 1.0x 近傍に戻ること。
**rollback**: 分類器フィールドは additive、決定論フロアは flag 不要の純関数追加。
戻すのは briefing.py の上書き 1 箇所の revert で足りる。

---

## 2. [MEDIUM] weekly-status-synthesis の timeout 常習 + 欠落週

### 真因 (2 層)

1. **構造**: weekly run が「増分評価 (cap 12 × Dense 31B、1 呼出数百秒) + 全 active
   (~93 件) 一括 adversarial sweep + trajectory 射影 + narrative」を 30 分予算に同居させて
   いる。7/05 に timeout (1800s)、7/07 に手動復旧×2 の常習。**台帳が育つほど悪化する
   構造的スケーリング問題**。なお sweep の adversarial は 1 バッチ call
   (max_tokens 4000) のため、93 judgment では出力切断のリスクもある。
2. **一回性**: 7/6-7/12 週の欠落は、日曜夕方 → 月曜深夜へのスケジュール移設 (7/12-13) の
   遷移窓で旧枠も新枠も踏めなかった複合事象。scheduler 自体は健全
   (全ジョブ next_run_at 設定済・次回 7/20 02:25)。

### 決定 (2026-07-16 レビュー): 再構成はしない — timeout は既修正と実測で確認

実測裏取り: 7/05 の timeout (30.0 分) は 7/07-08 の修正 (detect の fast 26B 化ほか) の
**前**であり、修正後の 7/07 成功 run は **13.9 分** (予算の半分)。ユーザー認識のとおり
timeout 問題は解決済み。当初案 (i) 「weekly から増分評価を撤去」は**撤回**する。

残す対処 (再構成ではなくバグ修正 + 監視 + 補填):

- **(i) 欠落週の backfill (主対処)**: `src/main.py` に `--as-of <ISO>` (期間解決の now
  上書き) を追加し、7/6-7/12 週の weekly を手動再生成する。`revisions_since` は「当 run
  より後の revision を除外する」ガードが既にあり、過去期間 backfill での未来混入は
  防がれる (stateful.py §5c コメントの想定ケース)。forecast_indicators の欠落 snapshot
  (6/29・7/13) も同経路で追走される。
- **(ii) sweep 出力切断バグの修正 (timeout とは別の潜在バグ)**: 台帳が 41 (7/07) →
  93 situation (現在) に成長し、全 active 一括の adversarial batch call (max_tokens
  4,000) は 93 件 × ~50 tokens で**出力が確実に溢れる** → parse 失敗 → 例外 catch →
  **sweep 全体が silent skip**。対処 = 「salience 上位 + 最終 adversarial からの経過が
  長い順」ローテーションの **N=24/週** 有界化 (全量は 3-4 週で一巡、切り詰めは log =
  no silent caps)。台帳がいくら育っても恒久有界。
- **(iii) 製品鮮度の dead-man 監視**: daily-heartbeat (`build_heartbeat_text`) に
  「製品鮮度」1 行 — weekly 総括 / recap / spotlight / monthly の最終生成からの経過
  日数、閾値超 (weekly 系 9 日 / monthly 35 日) で WARN。今回 9 日間の欠落無検知の
  再発防止。timeout が万一再発しても翌朝に気づける。

**検証**: backfill で 7/6-7/12 週の status_synthesis が生成され retrospect の synthesis
パネルが埋まること。7/20 の定時 weekly が完走し sweep が N=24 で成功すること。
**rollback**: いずれも additive (--as-of は新規引数、ローテは定数、heartbeat は行追加)。

---

## 3. [MEDIUM] アクター提案の人承認滞留 (pending 33・決裁 0)

### 真因

承認キュー (actor_update_proposals) が UI に存在するだけで、**注意経路に載っていない**。
通知モデル (Discord=警告+日次 / Web=pull) において、pull 専用のキューは「見に行く習慣」が
ないと成長に気づけない。

### 対処療法 (採らない)

自動承認・自動期限切れ — 「確定帰属は辞書ゲート + 人承認のみ」は再提案不可の確定原則。
辞書の判断を自動化しない。放置での自動破棄も CTI 辞書の判断としては不誠実。

### 本質設計 — 注意誘導 (判断は人に残す)

- **(i) heartbeat に滞留行**: 「アクター提案 pending N 件 (最古 X 日)」を daily-heartbeat に
  追加。閾値 (pending ≥ 10 or 最古 ≥ 14 日) で WARN 昇格。
- **(ii) UI バッジ**: サイドバー「アクター辞書」nav とダッシュボード ops widget に
  pending 数バッジ。
- **(iii) 決裁摩擦の低減**: Actors ページに複数選択の一括 approve/reject (現 1 件ずつ)。
  emerging 25 件の一括棄却/承認を現実的にする。

---

## 4. LOW 群の設計

| # | 問題 | 本質対処 | 備考 |
|---|---|---|---|
| 4a | dead PIR (pir_known_threat_followup match 0) | PIR 一覧/KPI に「30 日 match 0」バッジ + 再 compile 導線を表示。**定義の修正は人** (PIR = canonical intent、ツールが書き換えない) | 表示のみ・小 |
| 4b | 予測的中率 97% の準トートロジー | hit 判定を v2 に較正 (**推奨閾値確定 2026-07-16**、下記 §4b) | 意味論変更なので版分離 |
| 4c | standing posture の確度根拠が台帳から辿れない | **状態分離 (7/16) により自己治癒見込み** — 次回 standing 評価から引用証拠が record_assessment で残る。7/20 週明けに検証し、不足なら posture カードに source_basis 内訳 (出典 tier 集計) 行を追加 | まず観察 |
| 4d | s-8f2723f120e1 の連続 hypothesis_flip | 対処しない (ACH が新着で揺れているのは機構が働いている証跡)。1 週間後に収束を確認。3 週連続 flip なら増分 prompt のアンカリング/証拠品質を個別調査 | 観測のみ |
| 4e | 地図 centroid 欠落 ISO + 国名 normalizer 尾部 | `_COUNTRY_CENTROIDS` に PY/UY/DO/HN/ME/GH/BW/ML 等を追加 + normalizer に Mali/Ghana/Botswana 等の alias 追加。決定論・純追加。盤外 (unplaced) の正直計上は維持 | 小 |
| 4f | situation と地図の「cyber」分母乖離 | 両集合を 1 モジュール (例 `src/cti/category_scopes.py`) に**命名して**定義 (広義=脅威状況レンズ / 狭義=攻撃イベントレンズ) し双方が import。UI に「地図と国家情勢は分母が異なる」注記。**統一はしない** (2 レンズは意図的) — silent drift だけを封じる | SSoT 化 |
| 4g | mitre-sync が job_last_run に不在 | **対処不要** — pipeline 種は runs テーブル経由で jobs console に表示される設計どおり。記録のみ | 非問題 |
| 4h | CLAUDE.md §7 の実行時刻表の陳腐化 | 静的な時刻表を廃止し「スケジュール SSoT = DB (実行管理 UI / /api/v1/jobs)」への参照 + 不変則 (安全なデプロイ帯 = 毎時 :16-:29/:35-:57、heavy ジョブ実行中の rebuild 回避) のみ残す。**DB が SSoT のものを文書に複製しない** (病因: 二重管理は必ず乖離する) | doc 修正 |
| 4j | 証拠 polarity supports:contradicts = 13:1 | **対処しない**。polarity は leading 基準・leading は反整合最少の仮説なので supports 優勢は構造的必然。非 supports 23% + adversarial 検証が対称性を担保。月次の監査クエリで観測継続 | 観測のみ |

### §4b 予測 hit 閾値の推奨 (考察、2026-07-16)

**推奨: 固定倍率でなく Poisson 2σ スケーリング** —
`hit ⇔ 週次正規化した観測 ≥ baseline + 2√baseline`。

- **固定倍率 (1.5× 等) が較正にならない理由**: 偶然 hit 率がボリュームで変わる。
  Poisson 近似で baseline λ=2 の指標は 1.5×=3 件を偶然でも **~32%** の週が超える
  (ほぼ当てずっぽうでも 3 回に 1 回「的中」)。λ=100 なら 150 件は事実上不可能で
  不当に厳しい。倍率一定 = 指標ごとに検定の意味が違う。
- **2σ (≈97.7 パーセンタイル) は偶然 hit 率を全指標で ~2-3% に揃える**:
  λ=2 → 閾値 4.8 (≈2.4×) / λ=10 → 16.3 (≈1.63×) / λ=100 → 120 (≈1.2×)。
  ボリュームに応じて厳しさが自動調整され、マジックナンバーは k=2 の 1 つだけで
  統計的意味 (2σ) を持つ。
- **3 値判定** (現行は miss が構造的に出ない → 下振れを初めて記録できる):
  - hit: 週次正規化観測 ≥ baseline + 2√baseline (有意な増勢 = 予測どおり)
  - partial: baseline − 2√baseline 〜 +2σ の帯 (基礎率での継続)
  - miss: < baseline − 2√baseline または観測 0 (下振れ = 増勢予測は外れ)
- **週次正規化**: observed / max(1, 経過週数) — 検証窓の週跨ぎによる累積インフレを除去
  (6/22 batch が 2 週跨ぎ検証で observed 過大化した実測への対処)。
- **低頻度フロア**: λ < 1 の指標は閾値 = max(2, λ + 2√λ) (最低 2 件観測で hit)。
- **導入は v2 並記**: 既存 hit/verified の生カウントは書き換えない (97.4% の数字の
  意味を静かに変えない)。UI は v2 を主表示・v1 を参考表示。
- **較正の検証**: 運用 4 週で v2 hit 率が 30-70% のレンジに入るかを観察。恒常的に
  外れるなら k (=2) を 1.5〜2.5 で再較正する (k は定数 1 箇所)。

---

## 5. 実施順序 (推奨)

| 優先 | 内容 | 規模感 |
|---|---|---|
| P1 | #1 PMESII 供給根治 (I-infra 決定論フロア + 分類器 1 boolean、**T 軸は廃止**) + fill-rate 7 軸登録 + カード劣化バッジ + backfill | 1 セッション |
| P2 | #2 欠落週 backfill (--as-of) + sweep 出力切断バグ修正 (ローテ N=24) + 製品鮮度 dead-man — **7/20 の次回発火前に (ii) を入れないと sweep が silent skip する** | 0.5-1 セッション |
| P3 | #3 注意誘導 (heartbeat 2 行 [提案滞留 + 製品鮮度は P2 と共用] + UI バッジ + 一括決裁) + 4a dead PIR バッジ | 0.5 |
| P4 | 4e centroid / 4f カテゴリ SSoT / 4h CLAUDE.md | 0.5 |
| P5 | 4b 予測較正 v2 (Poisson 2σ、§4b で閾値確定) | 0.5 |
| — | 4c/4d/4j は観察 (7/20 週明けに再確認) | — |

**判断点はレビュー (2026-07-16) で全て確定**: ① T 軸 = 廃止 (P-env は維持)。
② weekly 再構成 = 撤回 (timeout は既修正・実測 13.9 分)。backfill + sweep バグ修正 +
鮮度監視のみ。③ 予測 hit 閾値 = Poisson 2σ (baseline + 2√baseline、週次正規化、v2 並記)。
