# ソース取得の段階的エスカレーション・ポリシー (2026-08-01)

コード SSoT: [`src/tools/fetch_policy.py`](../src/tools/fetch_policy.py)

## 1. 背景 — 「プレビューでは見えるのに取得されない」の解剖

2026-08-01、購読ソース 2 件が「取得できていない」症状を示したが、原因は別層だった:

| ソース | 症状 | 実際に壊れていた層 |
|---|---|---|
| Security Joes | 403 で 124 回連続失敗。**ライブプレビューでは取れる** | feed 取得層 (WAF が bot UA を遮断。プレビューだけ browser UA fallback を持っていた) |
| Indo-Pacific Defense Forum | 「30日 記事なし」表示 | 本文取得層 (feed は 200。7 月から JS チャレンジ導入で本文抽出が全滅 → posted 0) |

構造的な病因は 3 つ:

1. **エスカレーション実装の分岐**: browser UA fallback がソース追加プレビュー経路にしか
   なく、本番の定期取得 (feed / sitemap / html_listing) は bot UA 固定だった。
2. **観測面と実行面の乖離**: プレビューが独自実装である限り、プレビューの成功は
   本番の成功を意味しない。人が見に行くと成功して見える。
3. **健全性ラベルの層潰れ**: 分類が最終層 (`posted_count`) しか見ておらず、feed 死・
   本文死・記事化ゼロが同じ「記事なし」に潰れて triage 不能だった。

## 2. 原則 1 — エスカレーションは取得層の共通ポリシー

**既定は礼儀正しい bot UA (`kuebiko/1.0 …`) で名乗る。WAF にブロックされた事実に
応答して、ブラウザ相当 UA へ 1 段だけエスカレーションする。**

- エスカレーション発火 status (`BLOCK_ESCALATION_STATUSES`): 202 / 403 / 406 / 429 / 503。
  404・5xx (520 等) はコンテンツ/サーバ側の問題で UA では変わらないため発火しない。
- ネットワーク失敗 (timeout / 接続断) では発火しない (UA で回避できない tarpit が大半)。
- browser UA は `src/tools/user_agent.browser_user_agent()` を**呼出時**に解決
  (UA 自己修復ジョブ ua-health の更新を再起動なしで拾う)。
- 両段失敗時は**bot 段の status を記録する** (本来のブロック状態を health に残す)。

適用マップ (全て `fetch_policy.staged_get / staged_get_sync` 経由):

| 経路 | モジュール | 第 3 段 (JS レンダ) |
|---|---|---|
| RSS/Atom feed (毎時) | `src/tools/direct_rss_source.py` | なし (XML に JS は無意味) |
| sitemap watcher | `src/watchers/sitemap_base.py` | なし |
| HTML listing watcher | `src/watchers/html_listing.py` | なし |
| 本文抽出 | `src/tools/content_extractor.py` (UA ローテ + Playwright) | **あり** |
| ソース追加 / ライブプレビュー | `src/ui/api/_source_http.py` | なし |

本文抽出の UA ローテーションは従来実装のまま、発火 status 集合のみ SSoT を参照する
(本文だけ 406 を欠く、といった分岐を作らない)。

### bot UA に載せてよい情報 (2026-08-16)

**bot UA は「役割」だけを名乗り、運用者や組織に辿れる識別子を載せない。**
形式は `kuebiko/1.0 (+<役割>)` — 例 `(+rss-fetcher)` / `(+sitemap-watcher)` /
`(+html-listing-watcher)` / `(+source-discovery)`。

UA は購読先すべてに毎回届くため、識別子を載せると「誰が・どのソースを・どの周期で
購読しているか」= **収集網の構成**が相手側のログに残る。収集対象を監視する立場では
これは秘匿すべき情報にあたる。⚠ 動的 IP でも UA は変わらないので、IP の匿名性とは
別問題として扱う。

かつて RSS 経路だけが運用者の GitHub アカウント URL を名乗っており、有効な全 feed へ
毎時送出されていた。OSS の「礼儀正しい bot 識別」としては真っ当な実装だが、本ツールの
脅威モデルでは意味が異なる。`tests/unit/test_fetch_policy.py` の
`TestBotIdentityDoesNotLeakOperator` が 4 経路すべてを検査し、退行を防ぐ。

### 第 3 段 (Playwright) の発火条件 (2026-08-01 拡張)

本文抽出の Playwright fallback は **status 集合 + 応答の実態 (JS チャレンジ指紋)** で
発火する (`fetch_policy.looks_like_js_challenge`):

- 従来: `202/403/429/503` のみ → ipdefenseforum の **307 + チャレンジ本文** が
  すり抜け、Playwright 経路が一度も発火せず本文抽出が 1 か月無音全滅した
- 現在: block status **または** ボディ先頭にチャレンジ指紋 (`javascript is required` /
  `just a moment` 等) があれば発火。指紋があって Playwright でも取れない場合の
  failure_reason は `js_challenge` (UA では直らない失敗として `http_error_*` と区別)

**per-run cap** (`PLAYWRIGHT_EXTRACT_CAP`、既定 10、0 以下=無制限): Playwright は
1 記事 10-30 秒かかるため、extractor インスタンス (= 1 run) あたりの試行回数を制限して
pipeline 時間予算 (soft deadline) と両立させる。溢れた分は既存 failure 経路に落ち、
body-refetch-backlog が次周期で拾う (`js_challenge` は恒久失敗リストに入れない —
再試行で Playwright が拾えるため)。

**feed / sitemap 層 (①) には Playwright を自動適用しない**: feed XML を JS チャレンジで
守るサイトは稀で、現れた場合は bespoke Playwright scraper として transport ごと
切り替える (nicter / 38north と同じ運用)。

第 3 段の留意点 (2026-08-01 評価): stealth は WAF 更新で壊れうる (壊れると本文取得
エラー群に再浮上して気づける) / 検出されると IP ブロックに発展しうる (単一 IP 運用の
ため指紋発火 + cap で頻度を抑える) / 敵対者隣接コンテンツの JS をコンテナ内ブラウザで
実行するリスクは既存 (nicter 等) と同種。

## 3. 原則 2 — プレビューは本番と同じ取得層を通す

`fetch_text` / `fetch_bytes` (プレビュー) と本番取得が**同じ `fetch_policy` を通る**ため、
プレビューの成功 = 本番の取得可否をそのまま意味する。プレビュー応答は
`fetch_stage` (bot / browser) を返し、UI は browser 段で取れた場合に
「bot UA はブロックされるため、ブラウザ相当 UA へ自動切替して取得しました
(本番の定期取得も同じ動作)」と明示する。

不変量: **観測面に、実行面に無い救済段を実装しない**。新しい fallback を足すときは
fetch_policy に足す (片側にだけ足すと今回の事故が再発する)。

## 4. 原則 3 — 健全性ラベルは壊れた層を名指しする

購読ソース画面の分類 (`frontend/src/pages/SubscriptionsPage.tsx: classify`):

| グループ | 判定 | 意味 (どの層が壊れているか) |
|---|---|---|
| 取得エラー | `health.consecutive_failures >= 3` | ① feed 取得層 (WAF / feed 消滅 / DNS) |
| 本文取得エラー | 記事化 0 かつ `extract_failed_count > 0` | ② 本文取得層 (JS チャレンジ等。feed は生きている) |
| 要対処 (記事化なし) | 記事化 0 (上 2 つに該当せず) | ③ 取得も本文も OK で新着が無い (feed が枯れた等) |

`extract_failed_count` は `subscription_analytics.fetch_all_feed_stats` が
`articles.status = 'extract_failed'` (30 日窓) から集計する。

## 4.5 短文ページの汎用受理 (2026-08-01)

公式 advisory (JVN 等) は「概要 + 詳細表」の構造でページ自体が短く、抽出は成功して
いるのに `min_content_length` (200) の一律閾値で捨てられていた (実測: JVNVU が
189 字で 11 字不足 → extract_failed 10 件/30日)。

- Playwright でも本文が増えず、paywall 判定も通過した後、抽出テキストが
  **閾値の 7 割以上** (`_SHORT_PAGE_ACCEPT_RATIO`) あれば「短いが完全なページ」
  として採用する (log: `content_extract_short_page_accepted`)。
- サイト名はコードに出てこない汎用ルール — 専用パースは引き続き持たない
  (bespoke は nicter のみ、という現状を維持する)。

## 4.6 死活記録は transport 横断 (2026-08-02)

「記事が出ない」は ①ソースが発信していない (正常) と ②取得が壊れて発信を観測できて
いない (異常) の重ね合わせ。**成果 (articles) を数えるだけでは区別できず、区別する
唯一の方法は「取得という行為が成立したか」を成果とは別に記録すること**。

`source_fetch_health` への記録は `last_fetch_health()` という単一 seam に統一され、
RSS / html_scraper / sitemap が同じ形で死活を残す (SSoT:
`src/tools/source_fetch_outcome.py`)。**「行為の成立」の境界は transport ごとに違う**:

| transport | 成立の境界 | 0 件の扱い |
|---|---|---|
| RSS | 取得 + feedparser の parse | entry 0 = 「0 件提示した」観測 (失敗ではない) |
| html_scraper | 取得 + **自前 selector の適用** | 抽出 0 = セレクタ陳腐化 → **失敗** |
| sitemap | 取得 + **include/exclude の適用** | 一致 0 = パターン陳腐化 → **失敗** |

後者 2 つで抽出 0 件を失敗とするのは、サイト改修で selector が腐っても listing は
200 を返し続け、**取得成功だけを見ていると無音で死ぬ**ため (listing/sitemap が常に
0 件を提示することは実務上ありえない)。`source_key` は購読一覧の `url` と一致させる
(RSS=feed URL / scraper=listing URL / sitemap=先頭 sitemap URL)。

## 5. 運用ノート

- **JS チャレンジ型 (Indo-Pacific 型) は UA では直らない**。「本文取得エラー」に出たら
  選択肢は (a) Playwright レンダ経路への切替 (b) feed summary で degrade (c) 無効化。
- feed が browser 段で恒常的に取れている場合、log イベント `rss_feed_fetched` の
  `stage=browser` で観測できる (WAF 側の方針変化の手掛かり)。
- ua-health ジョブは「UA バージョン陳腐化による全体劣化」の検知が役目。
  **サイト個別の bot ブロックは本ポリシーが取得時に自動救済**するため、ua-health の
  canary が健全ソースに偏る盲点はこの層で吸収される。
