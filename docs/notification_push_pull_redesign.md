# 通知モデル再設計 — Discord(警告/push) と Web(状況認識/pull) の分離

> 作成 2026-06-28。Web UI が無かった頃に設計した「全件 Discord push」を、Web UI が
> 揃った現在に合わせて見直す。**方向性はユーザー合意済 (2026-06-28)、実装は次セッション**。
> 関連: [docs/synthesis_assessment_architecture.md](synthesis_assessment_architecture.md)、
> [[discord_channel_design]] [[pir_routing_separation_flow_page]] [[routing_rule_engine]]。

---

## 0. 結論 (一文)

**Discord は「警告 (push・割り込み・要対応)」専用に絞り、状況認識・探索・日次ブリーフ閲覧は Web UI (pull) に寄せる。`watch` チャンネル (Discord 投稿の 86%・260/日) を web-only にし、Discord は alert + 日次ブリーフ(朝刊/夕刊) のみ高 S/N で push する。**

---

## 1. 根本原因 (なぜ今の設計が古いか)

Web UI が無かった時代は Discord が唯一の配信面だったため**全件 push**せざるを得なかった。Web UI
(News/Intel/地図/Synthesis/flow 等) が揃った今、その必要は消えた。にもかかわらず全件 push が残り、
**Discord が DB ダンプ化**している。

## 2. 観測データ (実測、2026-06-28)

posted_channel 別の投稿量 (status='posted'):

| channel | 7日 | 30日 | /日 | 性質 |
|---|---|---|---|---|
| **watch** | 1818 | **6363** | **~260** | 蓄積フィード = 人が Discord で読む量でない |
| brief | 110 | 516 | ~16 | 日次ブリーフ + medium 記事 |
| alert | 78 | 234 | ~11 | 緊急 |
| japan_watch | 49 | 138 | ~7 | JP |

**watch が全 Discord 投稿の 86%**。これは「監視チャンネル」でなく事実上のログ。

## 3. フレームワーク: PUSH(警告) vs PULL(状況認識)

- **Discord = PUSH面**: 今すぐ割り込むべきもの (時間依存・要対応)。モバイル通知・ambient。
- **Web UI = PULL面**: 自分のペースで読む・探索する。
- 副次効果: watch を外すと **alert が高 S/N**になり「alert 通知＝今すぐ動け」が信頼できる
  (現状は 260/日 watch に埋もれ alert fatigue 気味)。
- ミッション対応: 警告 (脅威の即応) = 割り込み駆動 / 状況認識 (日次の通読・探索) = 自ペース。

## 4. 目標設計

```
Discord (PUSH・高S/N):
  alert    緊急 (0day/KEV/active exploit/JP重要インフラ) — 即割り込み
  brief    日次ダイジェスト = morning-brief + evening-brief のみ (朝刊/夕刊、ambient nudge)
Web UI (PULL・全件):
  watch(260/日) + medium + JP非緊急  →  web-only (push しない、DB保存 = News/Intel/地図で browse)
```

朝刊/夕刊 (2026-06-28 実装済 morning-brief/evening-brief) が「1日2回のダイジェスト push」=
ambient nudge として機能する。watch を Web に回しても、日次ブリーフが状況を要約し詳細は Web で掘る。

## 5. 必須 enabler (これが無いと pull は成立しない)

### W1. Web「日次ブリーフ」ビュー (★最優先・前提)
**現状ギャップ (実コードで確認済)**: 日次ブリーフ (朝刊/夕刊) に Web ビューが無い。
- daily synthesis ナラティブ (ブリーフ上段) → Synthesis タブで見られる (period_type=daily)。
- **PIR daily focus (朝刊下段) → 永続化も Web ビューも無い** (生成して Discord 投稿するだけ)。
- 統合ブリーフそのもの → Web に統一ビュー無し。
→ 「Web を見ろ」に倒す前に、ブリーフ自体を Web で読める必要がある。
**実装**: 段5 の `weekly_recaps` 永続化と同パターン — 合成ブリーフ本文 (+ slot/date) を保存 →
「日次ブリーフ」ページ (今日の朝刊/夕刊 + 履歴) で表示。`_run_daily_brief_default` の compose 済
メッセージを永続化フックで保存するのが最小。

### W2. Web「新着/未読」surface
web-only に回す watch 等を「前回確認以降の新着」で効率レビューできるビュー (inbox/unread 的)。
無いと「Web を見ろ」が巨大リストのスクロールになる。News ページに since-last/unread を足す。

## 6. routing 変更

### R1. web-only disposition
現状 status='posted' は必ず channel に push。watch-level を「保存するが push しない」に。
- 既存の「保存のみ」経路 (status='collected' 系、victim collector 等で実績) を流用可能。
- routing engine ([[routing_rule_engine]]) の channel 決定に **no-push 出力**を足す
  (PROPERTY_CATALOG / config-driven の枠内で)。

### +調整
- **alert 閾値レビュー**: 主 push になるので緊急判定 (importance high + 0day/KEV/JP-critical) が適切か。
- **japan_watch collapse**: JP緊急→alert / JP非緊急→web (JP フィルタ + ブリーフが拾う)。

## 7. 段階移行 (W1/W2 が先 — 受け皿を作ってから push を止める)

1. **W1 日次ブリーフ Web ビュー** (ブリーフを Web で読めるように)。
2. **W2 新着/未読 surface** (web-only の効率レビュー)。
3. **R1 web-only disposition** (watch/medium/JP非緊急の push 停止)。
4. alert 閾値レビュー / japan_watch collapse。

各段は単独で出荷可能。**W1→W2 の後に R1** (Web の受け皿ができる前に Discord push を止めると情報が宙に浮く)。

## 8. 決定事項 (2026-06-28 ユーザー合意)

- 方向 (Discord=警告 / Web=状況認識) に同意。
- watch(260/日)+medium → web-only に倒す (核心)。
- 日次ブリーフ (朝刊/夕刊) は push 維持 (= ambient nudge として機能)。
- japan_watch は collapse (JP緊急→alert / 他→web)。
- alert 閾値はこの機にレビュー。
- W1 (日次ブリーフ Web ビュー) + W2 (新着 surface) を作る (pull の前提)。
- 大きめの multi-part feature ゆえ次セッションで W1 から実装。

## 実施ログ
- 2026-06-28: 方向性ユーザー合意 + 本設計文書作成。実装は次セッション (W1 から)。
- 2026-06-28: **W1 + W2 + R1 実装・検証 → 同日デプロイ・本番稼働 (WEB_ONLY_DISPOSITION=1)**。
  - **W1 (日次ブリーフ Web ビュー)**: `daily_briefs` テーブル新設 (PG + SQLite、段5 weekly_recaps と
    同パターン)。`record_daily_brief` / `list_daily_briefs` (run_history)。`_run_daily_brief_default`
    が Discord 投稿の前に本文を永続化 (push 失敗でも Web に残す)。`GET /api/v1/intel-graph/daily-briefs`。
    新ページ `DailyBriefPage` (左=履歴一覧 / 右=本文 MarkdownText) + nav「日次ブリーフ」(BookOpen)。
  - **W2 (新着/未読 surface)**: `/articles` に絶対 `since` (ISO) パラメータ追加 (`_parse_since_iso`、
    相対 since_hours より優先)。News ページに localStorage カーソル (`news-last-seen`) +「新着のみ」
    トグル +「ここまで既読」ボタン。readonly/mobile は localStorage 不可で silent fallback。
  - **R1 (web-only disposition)**: env flag `WEB_ONLY_DISPOSITION` (既定 0 = deploy-dark safe)。
    有効時、push 対象外 tier (既定 watch/brief/japan_watch、`WEB_ONLY_CHANNELS` で上書き可) は
    Discord push をスキップし DB 保存のみ。**表現は status='posted' を維持** (新 status を作らない =
    `status='posted'` を参照する 40+ の web/分析/地図/digest クエリにそのまま出る。新 status は
    blast radius 過大で却下)。posting loop の dedup 後・STIX/post 前に判定、`_register_seen` で
    url_seen+embedding 登録 (post 成功経路と共有、再 surface 防止)。alert と日次ダイジェスト
    (別経路 `_run_daily_brief_default`) は引き続き push。
  - 検証: ruff/mypy 新規エラーゼロ、frontend tsc+build OK、unit 1743 passed (既知 baseline 6 件のみ
    fail=nvd/taxonomy/dashboard、非regression)。新規テスト: test_daily_briefs (5) /
    test_articles_feed_facets (+6 since) / test_main TestWebOnlyDisposition (+4)。
- **✅ デプロイ完了 (2026-06-28 21:0X JST)**: ① 新コードを image build + recreate (両コンテナ healthy、
  daily_briefs テーブル作成、起動エラーなし)。② R2c/R2d を DB に適用 (config_store v2、gap-fix を
  engine で実証・回帰なし)。③ `WEB_ONLY_DISPOSITION=1` を **.env + docker-compose.yml の environment
  ブロック**に設定し recreate (PID1 environ 反映確認、`_web_only_config()`=True/channels={watch,brief,
  japan_watch})。**⚠gotcha**: env flag は .env だけでは効かない (アプリは load_dotenv 不使用、
  pydantic-settings が .env をモデルに読むのみで os.environ に入れない)。`_web_only_config()` は
  os.environ を読むため docker-compose の `environment:` ブロックに `WEB_ONLY_DISPOSITION:
  ${WEB_ONLY_DISPOSITION:-0}` を追加 (ROUTING_RULES_ENGINE/READ_ONLY と同方式) が必須だった。
  - **残**: 観察 (次 run 以降 watch/brief/japan_watch が無音化し alert のみ push か、
    `docker logs kuebiko | grep web_only_suppressed` + #alert 量 + 明朝 06:30 の daily_briefs)。
    working tree からデプロイ済 = 未 commit (docker-compose.yml + .env も変更)。rollback = flag 0 + recreate /
    routing は config-history で v1 revert。
- **2026-06-28 (同日 follow-up): R1 を env フラグ → channel レジストリの push 属性へ統合・再デプロイ**。
  env フラグは「routing が決めた配信を後段で握り潰す第二の制御面」で、配信 SSoT を routing/flow に
  一本化する原則 ([[operational_config_db]] [[pir_routing_separation_flow_page]]) に反する + 再デプロイ要。
  → **`ChannelDef.push: bool=True` を追加** (push=False=保存のみ)。投稿ループは `push_map()` を読み、
  push=False の tier を web-only に。情報フロー /app/flow の「Discord 配信」トグルで編集 (DB config_store・
  版履歴・config-history で revert・**再デプロイ不要**)。alert は validate で push=False を拒否 (緊急の最終 ch)。
  日次ダイジェストは投稿ループ外の別レールゆえ brief.push=False でも配信維持 (#brief=ダイジェスト専用)。
  env 機構 (`_web_only_config`/`WEB_ONLY_DISPOSITION`/docker-compose/.env) は撤去。
  **ゼロギャップ移行**: 先に DB channels に push=False を保存 (旧コードは push 無視で env=1 が web-only 維持)
  → 新コード deploy で registry push が引き継ぐ → env 撤去。web-only が一瞬も途切れない。
  検証: ruff/mypy 新規ゼロ・frontend build OK・unit 1746 passed (baseline 6 のみ)・push_map() 本番反映確認。

## 配信の統一: コンテンツ・ルーター + プロダクト・ルーター (2026-06-28 完了・デプロイ済)

「配信が2つの世界に分裂 (記事=config 駆動 / 8 つの digest プロダクト=チャンネルをコード直書き
+push 無視)」という不整合を解消し、**全配信を情報フローで制御可能**にした。

- **段1 — brief の二重役割を解消**: `brief` は「ダイジェスト配信先 (push したい)」と「medium 個別
  記事の tier (web-only にしたい)」を兼ねており channel.push を一値に決められず例外ハックを生んでいた。
  記事ルール R3.5/R6.brief を **brief→watch に再ルート** (DB)、`brief.push=True` に。→ **brief は
  記事を受けずダイジェスト専用**になり brief.push が正直に (例外ハック消滅)。記事は alert(緊急)/
  japan_watch/watch(web-only) のみへ。
- **段2 — プロダクト・ルーター新設**: `src/tools/product_routing.py` (config_store key
  "product_routing"、built-in fail-safe、版履歴)。`product_channel(id)` で curated product の配信先を
  解決。editorial product (morning/evening brief・weekly-recap・status_synthesis・pir_spotlight) の
  runner を **ハードコード廃止 → product_channel + channel.push 尊重**に。情報フロー /app/flow に
  「プロダクト配信」カード (`ProductRoutingCard`) で product→channel を編集可能。
  既定: morning/evening/recap/synthesis→brief(push)、**spotlight→watch (=push=False で web-only 化、
  Synthesis タブで閲覧。push したいなら flow で brief へ)**。ops heartbeat (死活=常時 push) と
  ransomware-ingest (コンテンツ収集系) は性質が違うため対象外。
- 設計原則 (確定): **記事=コンテンツ・ルーター (条件→tier) / ダイジェスト=プロダクト・ルーター
  (名前→channel) / medium=channel.push**。3 つとも情報フローで一望・編集可能。push は channel(tier)
  に置く (DRY、両ルーターが共有)。「配信は flow で一元管理」。
- 検証: ruff/mypy 新規ゼロ・frontend build OK・unit 1756 passed (baseline 6 のみ)・本番 seed + push 整合確認。

## alert 閾値レビュー結果 (2026-06-28 完了・R2c/R2d 本番適用済)

R1 を有効化すると watch/brief/japan_watch が無音化するため、真に緊急なものが確実に `alert` に
届く必要がある。現状 alert に push されるのは 3 ルールのみ (R8 apt_leak / R2a JP-critical+known_apt /
R2b breaking+kev|zero_day)。これに対し緊急事象が漏れる穴が 2 件:

- **Gap 1 (HIGH)**: KEV/0day が `article_type=breaking` でないと alert に上がらない (R2b の breaking
  要件)。ベンダ PSIRT 等で `advisory`/`research` に分類された KEV(悪用中)/0day は brief→無音になる。
- **Gap 2 (HIGH)**: JP-critical が `known_apt` 無しだと alert に上がらない (R2a)。アクター未特定の
  日本重要インフラ/政府/防衛の被害が japan_watch→無音になる (= japan_watch collapse の核心)。
- (補足) `zero_day` 信号は regex のみ (kev は regex+LLM+CISA カタログの 3 系統)。ルールでは塞げない
  信号品質の限界として記録。

**確定推奨ルール (R2 群直後・R3 より前に first-match で配置)**:
```yaml
  - id: R2c.alert_active_exploit          # Gap 1: KEV/0day は記事形式に依らず即応
    channel: alert
    when:
      all:
        - {property: article_type, op: not_in, value: [recap, tutorial, opinion, press]}
        - {property: category, op: in_config, value: high_threat_brief_categories}
        - any:
            - {property: kev, op: is_true}
            - {property: zero_day, op: is_true}
  - id: R2d.alert_japan_critical          # Gap 2: 日本重要事象はアクター特定の有無に依らず即応
    channel: alert
    when:
      all:
        - {property: article_type, op: not_in, value: [recap, tutorial, opinion, press]}
        - {property: japan_critical, op: is_true}
        - {property: stance, op: ne, value: propaganda}
```

**適用先 = DB (config_store) のみ、`/app/flow` UI 経由**。`config/routing_rules.yaml` には**入れない**:
seed yaml は legacy ladder (`_route_legacy`) と等価な凍結ベースラインで、`test_routing_rules.py`
の `test_equivalence_over_matrix`/`test_seed_shows_no_change_vs_legacy` が seed≡legacy を不変条件
として検証している (yaml に足すと等価性テストが壊れる)。かつ本番 DB は seed 済 (v1) なので yaml
編集は本番に届かない。運用チューニングは DB に重ねるのが設計 ([[operational_config_db]])。

**適用手順**: ① デプロイ (flag off) → ② `/app/flow` で R2c/R2d を追加 → preview で影響確認 → 保存
(DB に版保存、`/app/config-history` で revert 可) → alert 量が許容内か観察 (alert のみ変わるので
flag off でも安全に検証可) → ③ 信頼できたら `.env` `WEB_ONLY_DISPOSITION=1`。
