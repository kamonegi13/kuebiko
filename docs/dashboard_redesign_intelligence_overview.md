# ダッシュボード再設計: Intelligence Overview (蓄積知への窓)

> 2026-05-30。「このツールは OSINT 収集・分析・蓄積ツールになった。運用ダッシュボードでなく
> それにふさわしいダッシュボードを」という問題提起に対する設計。
> 媒体特性の議論 (Discord vs Dashboard) から原則を導出。

## 0. 確定した正体 (2026-05-30 再確定) — Dashboard = Composition 層

**重大発見**: 求めていた intelligence overview の柱 (異常/アクター・ドシエ/standing assessment/
PMESII) は **既に Intel Graph (デフォルト `/app`) に全部ある** (Shell.tsx: Synthesis/PMESII/
Threats/Operations タブ + 常設 DiscoveryPanel)。`/app/dashboard` にそれを作り直すのは Intel Graph
の三重化 = Schedule/Subscriptions と同じ二重化ミス。

→ **Dashboard の非重複な正体 = 「統合された一望 (composition) 層」**:
- Intel Graph タブ = **各次元の深掘り (exploration)**、1 度に 1 次元。
- Dashboard = **各タブの要点を compact widget に凝縮し 1 画面に合成 (at-a-glance)**。各 widget は
  深掘り UI を持たず、該当 Intel Graph タブ / ページへ**ドリルダウン link** する。
- **ユーザが widget を取捨選択・並べ替え・設定できる (configurable)** → 真の "dashboard"。

これは「タブ縦割りの詳細を統合して全体表示」を実現し、かつ deep-dive を複製しないので重複ゼロ。
加えて Intel Graph に**欠けている蓄積機能 (過去参照アーカイブ / holdings / PIR ギャップ集約)** を
新規 widget として composition に含める (これらは Intel Graph にも無い真の不足)。

## 1. 設計原則 — 媒体特性から導く

2 つの直交軸で Discord と Dashboard は本質的に異なる:

| 軸 | Discord | Dashboard |
|---|---|---|
| 取得 | **push** (向こうから届く) | **pull** (assess したい時に開く) |
| 時間性質 | **フロー** (流れて消える) | **ストック** (蓄積し保持・問い直せる) |
| 粒度 | 個別事象 (1 incident = 1 post) | 集約・関係・構造 |
| 場所 | モバイル (流し読み) | desktop (分析) |

→ **3 層に役割分離** (二重化を避ける):
- **Discord = FLOW**: 「今、何が届いたか」。個別 incident を時系列 push、モバイルで消費。*フィード*。
- **Dashboard = STOCK / 蓄積知への窓 (institutional memory)**: 「これまでに何を蓄積し、全体として
  何が分かっているか / 蓄積比で何が変わったか」。集約・記憶・振り返り・異常・関係。*地図と記憶*。
- **Intel Graph = DEEP DIVE**: その構造を filter/pivot で探索 (dashboard からドリルダウン)。

**dashboard の正体 = 蓄積 PG ストアの上に立つ「組織の記憶」への窓**。フィードに原理的に
できないこと (記憶する・蓄積全体で集約する・蓄積 baseline 比で変化を示す) が最大価値。

現 dashboard の本質的ミスマッチ: 「集約」自体は dashboard 的に正しいが、集約対象が
**運用指標 (run 数 / 失敗率)**。これを **蓄積インテリジェンス (アクター記憶 / 異常 / PIR / holdings)**
に替えるのが再設計の核心。

## 2. anti-duplication 鉄則 (2026-05-30 訂正)

**訂正**: 当初「個別 incident の逐次列挙はしない」としたが誤り。同じ「incident を並べる」でも
**フローの配信フィード**と**ストックの参照アーカイブ**は性質が真逆:

| | Discord (フロー) | Dashboard (ストック) |
|---|---|---|
| 性質 | 配信フィード (届いた順・一度・流れる) | 参照アーカイブ (蓄積・常設・検索/絞り込み可) |
| 操作 | 読み流す | PIR/アクター/国/重要度/期間/全文で絞り込み・遡及 |

→ 鉄則 (訂正版):
- ❌ **Discord の時系列 push フィードの再現**はしない (最新 N 件を到着順に流すだけ)。
- ✅ **蓄積 incident コーパスを queryable な参照アーカイブとして提供する**のは dashboard の**中核**
  (Discord は流れて埋もれ検索・遡及不可。ストックの本質)。
- **深掘り探索 UI (actor pivot / PMESII 軸 / 全 narrative) は Intel Graph**。dashboard はサマリ +
  ドリルダウン導線。Schedule/Subscriptions の「同機能 2 画面」を再発させない (層を明確に分ける)。

## 3. 2 本柱 + 補助セクション (蓄積ネイティブ)

### 柱A: 異常 vs ベースライン (発見支援) — 蓄積があって初めて定義できる
- **spiking / new / waking actors** + spike_alert。「30 日平均比で急増」「初観測」「休眠→再活性」。
- データ源: `GET /api/v1/snapshot` の `discovery.{spiking,new,waking}_actors` (sparkline 付) — **既存**。
- 価値: 「朝、蓄積比で何が動いたか」を 30 秒で。Discord (個別 push) が原理的に出せない dashboard 固有値。

### 柱B: アクター・ドシエ (学習記憶) — エンティティ単位の累積記憶
- 脅威アクターごとの累積知。`GET /api/v1/threats/actor/{id}` が**既に充実**:
  sponsor / description / top_sectors/countries/cves/ttps/malware/tools/IOC(ip/domain/hash/url) /
  relations(family/cooccur/campaigns) / recent_articles / **timeline_daily**。
- dashboard には「**追跡中アクター top (China/DPRK/Russia=PIR 対応、sparkline)**」を置き、クリックで
  ドシエ (Intel Graph drill or drawer) へ。データ源: `threats` + `snapshot.actors` — **既存**。
- 価値: 「X について何が分かっているか」が蓄積される唯一の場所。フローには不可能。

### 補助1: standing assessment (状況認識) — 流れて消えない「評価 of record」
- 最新 synthesis を**常設ピン**。`GET /api/v1/synthesis` (headline/weight/chain/cog/spillover/pir
  section) — **既存** (latest のみ)。Discord は流れるが dashboard は常に最新評価を保持。
- 補助: **過去 assessment 履歴** (週次 narrative の推移) → synthesis table に行はあるが list API は**新規小**。

### 補助2: PIR 充足 & 情報ギャップ (発見支援/状況認識)
- PIR 別 24h/7d match + **枯渇 PIR = 収集の盲点**。`GET /api/v1/pir/dashboard/overview` — **既存**。
- 「無いもの (ギャップ)」を示せるのは集約=ストックだけ。フローは absence を示せない。

### 柱C: 蓄積インシデント・アーカイブ (過去参照) — 流れて消えない参照可能なコーパス
- 全 incident を **PIR / アクター / 国 / 重要度 / 期間 / 全文** で絞り込み・検索・遡及。
  「あの件、いつ何があったか」をフローでは埋もれて引けない → ストックの中核。
- データ源: articles テーブル (コーパス本体)。`snapshot` の search filter は**既存**、本格的な
  incident-corpus 検索/絞り込みは**一部既存 + 拡張**。
- dashboard には compact 版 (最近の蓄積 incident を絞り込み可 + 検索ボックス) を置き、本格 archive は
  専用ビューへ展開。**既存 History ページ (run 中心の article 列挙) との役割整理が必要**:
  History = run/運用中心、Archive = intel-incident コーパス中心、と分けるか History を取り込むか。
- 価値: 5 方向の **過去参照** を最も具体的に満たす。Discord 不可能、dashboard 固有。

### 補助3: Intelligence Holdings (学習記憶) — 蓄積を資産として計測
- 追跡アクター数 / 蓄積インシデント / カバレッジ推移。現 DB stats を**再フレーム**。
- 推移 (成長グラフ) は**新規小** (時系列集約)。spotlight (PIR 縦断 narrative) への link。

### 将来スロット: 予測 (将来予測)
- Phase 4 forecast 未実装。今は synthesis の outlook 相当を昇格する余地のみ確保。スロットだけ。

## 4. 運用 (ops health) の扱い
消さず**降格**: 最上部 1 行の status strip (pipeline 稼働 / next run / 直近失敗のみ)。
失敗時は strip を赤くして従来どおり最前面。詳細は既存 Health / History ページへ委譲。

## 5. データ可用性 (既存 vs 新規)

| セクション | エンドポイント | 状態 |
|---|---|---|
| 柱A 異常 | `/api/v1/snapshot` discovery | **既存** |
| 柱B アクター | `/api/v1/threats` + `/threats/actor/{id}` | **既存 (充実)** |
| standing assessment | `/api/v1/synthesis` (latest) | **既存** |
| assessment 履歴 | synthesis list | 新規小 (data あり) |
| PIR ギャップ | `/api/v1/pir/dashboard/overview` | **既存** |
| holdings 現在値 | dashboard DB stats | **既存 (再フレーム)** |
| holdings 推移 | 時系列集約 | 新規小 |
| status strip | runs dashboard_summary | **既存 (圧縮)** |

→ **~80% が既存エンドポイントの surfacing/再構成**。新規は assessment 履歴 + holdings 推移の
小集約 2 本のみ。新規分析ロジックは不要 (蓄積分析は既に実装済、landing に出ていないだけ)。

## 6. 段階

### Composition 版 (確定方針)
Dashboard = compact widget の grid。各 widget = 該当データの要約 + Intel Graph へのドリルダウン link。
deep-dive UI は持たない (= Intel Graph)。widget 候補:
- status strip (ops、降格) / standing assessment (synthesis headline → Synthesis tab) /
  anomaly (discovery 要約 → Threats) / PMESII spike 要約 (→ PMESII tab) / PIR coverage・gaps /
  threats top actors (→ Threats) / **Holdings (新規・蓄積資産)** / **Incident Archive 検索 (新規・過去参照)**。

- **Phase 1 (固定 composite)**: 上記 widget を 1 画面に固定配置で合成。既存 API のみ
  (Holdings は db_stats 再フレーム、Archive 検索は Phase 2 へ)。/app/dashboard を置換。
- **Phase 2 (configurable)**: widget の表示/順序/期間をユーザ設定・永続化 (単一ユーザなので
  config or data/ に JSON 保存)。「設定修正できる」を実現。
- **Phase 3 (蓄積機能の新規 widget)**: Incident Archive (PIR/アクター/期間/全文の参照アーカイブ) +
  Holdings 推移 + assessment 履歴。Intel Graph にも無い過去参照/学習記憶の不足を埋める。
- 未決: composite を default landing (`/app`) に昇格するか、Intel Graph を default のまま `/app/dashboard`
  に置くか。

各 Phase 独立コミット + frontend build 検証。Intel Graph/Discord と重複しないことを各セクションで確認。

## 7. 実装時の確認事項 (未決)
- ドシエ表示は dashboard 内 drawer か Intel Graph drill か (重複回避の観点で後者リンク推奨)。
- standing assessment は Discord 投稿の synthesis と同一 → 「流れ vs 常設」の差で非重複だが、
  全文でなく headline + section 見出し + 「Intel Graph で全文」リンクに留めるのが層分離に忠実。
- モバイル考慮: dashboard は desktop 前提 (Discord がモバイル担当) で良いか。
