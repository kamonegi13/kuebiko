# ビジュアル selector ピッカー 実装計画書

> ソース追加ウィザード (Phase F) の HTML scrape mode に、Inoreader 型の
> 「ページを描画してユーザがクリックで記事部分を指定する」ビジュアル selector
> ピッカーを追加する計画。2026-05-29 起案。

## 1. 目的 / 解決する問題

現状の HTML scrape mode は **LLM が truncate された HTML から CSS selector を推測**する。
MERICS で実証された通り、これは以下の理由で不安定:

- LLM が存在しない class 名を当て推量する (`article a[href][class*='node-title']` → 0 件)
- 正しい selector (`.views-row .field--name-title a`) を出せても、各カードの先頭リンク
  (カテゴリバッジ) を拾うなど意図とズレる
- **UI で selector が読み取り専用**のため、外れても手動修正できず hint で振り直すしかない

ビジュアルピッカーは「推測」を「ユーザのクリックからの決定的導出」に置き換え、この
不安定さを構造的に解消する。LLM 提案は **初期推測**として残し、外れたらクリックで上書き
する hybrid とする。

## 2. UX フロー

```
[URL 入力] → probe → RSS/sitemap 見つからず → [HTML scrape mode]
   ↓
[ビジュアルピッカー画面]  ← 新規
   ├─ 左: 対象ページを iframe 描画 (same-origin proxy)
   │    hover で要素ハイライト、クリックで記事要素を選択
   │    LLM 初期推測の selector を最初からハイライト表示
   ├─ 右: 選択中 selector / マッチ件数 / preview 5 件 / 手動 selector 入力欄
   └─ [この selector で確定] → 既存 confirm → register へ
```

クリック 1 回で繰り返しコンテナを汎化し「同種の記事カード全部」を選択するのが肝
(Inoreader と同じ)。

## 3. 新規 / 変更ファイル

| 種別 | パス | 内容 |
|---|---|---|
| 新規 | `src/tools/url_guard.py` | SSRF guard: public http(s) のみ許可、private/loopback/link-local/metadata IP を拒否 |
| 新規 | `src/tools/page_renderer.py` | Playwright で URL を描画し post-render HTML を返す共用ヘルパ (chromium launch は `watchers/playwright_base.py` のパターン踏襲)。SSRF 用の `page.route` 遮断込み |
| 新規 | `src/ui/api/_source_proxy.py` | proxy endpoint 本体 (静的 or JS 描画 → sanitize → base href + picker JS 注入) |
| 変更 | `src/ui/api/sources.py` | `GET /api/v1/sources/proxy_page` + `POST /preview_html_listing_explicit` 追加 |
| 変更 | `src/ui/api/_source_html_preview.py` | 明示 selector で preview する関数を抽出 (LLM 経路と共用) |
| 新規 | `frontend/src/picker/picker-overlay.js` | iframe 内に注入する選択オーバーレイ + selector 導出ロジック |
| 新規 | `frontend/src/components/VisualSelectorPicker.tsx` | ピッカー画面 (iframe + postMessage 受信 + preview 表示) |
| 変更 | `frontend/src/components/AddSourceWizardV2.tsx` | Step union に `html_picker` 追加、遷移配線 |
| 変更 | `frontend/src/api/sources_v2.ts` | proxy URL builder + explicit preview API |
| 変更 | `src/ui/app.py` | proxy endpoint を READ_ONLY instance で無効化 (open proxy 防止) |
| 新規 | `tests/unit/test_url_guard.py` | SSRF guard の許可/拒否ケース |
| 新規 | `tests/unit/test_source_proxy.py` | sanitize (script 除去) / base href 注入 / selector 導出 |
| 新規 | `tests/integration/test_source_proxy_endpoint.py` | endpoint の readonly 403 / SSRF 拒否 / 正常系 |

## 4. backend: proxy endpoint 設計

### `GET /api/v1/sources/proxy_page?url=<encoded>&render=<auto|static|js>`

1. `url_guard.assert_safe_public_url(url)` で SSRF 検証 (§5 参照)
2. **取得 (render mode で分岐)**:
   - `static`: 既存 `fetch_text(url)` (browser-UA fallback 込み、速い)
   - `js`: `page_renderer.render_html(url)` で Playwright 描画後 HTML
   - `auto` (既定): まず static 取得 → 「JS-shell 判定」(本文が空 / `<div id=root>` 等の
     SPA マウント点のみ / `<a href>` が極端に少ない) なら js に自動フォールバック
3. **sanitize** (BeautifulSoup):
   - `<script>` を全除去 (元サイト JS を自オリジンで実行させない)
   - `<base href="<origin>">` を `<head>` 先頭に注入 (CSS/画像を元サイトから読込)
   - `on*` インライベントハンドラ属性を除去
   - `<meta http-equiv=...>` の CSP/refresh を除去
4. ピッカーオーバーレイ JS (`picker-overlay.js` の内容) を `</body>` 直前に注入
5. レスポンスヘッダ: `Content-Security-Policy: script-src 'self' 'unsafe-inline'` /
   `X-Frame-Options` は付けない (同一オリジン iframe 用)。`Cache-Control: no-store`

> 静的経路で MERICS 等 server-rendered サイトは描画可能 (検証済)。JS-SPA サイトは
> Playwright 描画後 HTML を返すことで MVP から対応する。UI に手動「JS で再描画」トグルも
> 併設し、auto 判定が外れた時の escape hatch とする。

### `page_renderer.render_html(url)` (Playwright)

- `watchers/playwright_base.py` の chromium launch / new_context / goto パターンを踏襲
- `wait_until="networkidle"` + 短い post-load wait で SPA の初期描画を待つ
- **SSRF 二重防御**: `context.route("**/*", ...)` で各 subresource/navigation の host を
  解決し private/metadata IP なら `abort` (headless browser 経由の内部到達を遮断)
- **concurrency guard**: `asyncio.Semaphore(1〜2)` で同時描画を制限 (browser は重い)。
  全体 timeout 30s、launch 失敗時は static にフォールバックして degrade

### `POST /preview_html_listing_explicit`

- body: `{listing_url, article_link_selector, title_selector}`
- LLM を呼ばず、`_apply_selectors_html` をそのまま適用して `SourceCandidate` を返す
- ピッカーで選んだ selector / 手動入力欄の両方がこれを叩く

## 5. SSRF guard (`url_guard.py`) — セキュリティ最重要

ユーザ指定 URL をサーバが fetch するため SSRF 対策必須 (CLAUDE.md §4)。

- scheme は `http`/`https` のみ
- host を `socket.getaddrinfo` で解決し、**全解決 IP** が public か検証
  - 拒否: loopback (127/8, ::1) / private (10/8,172.16/12,192.168/16,fc00::/7) /
    link-local (169.254/16 = クラウド metadata, fe80::/10) / `0.0.0.0` / multicast
- `fetch_text` は `follow_redirects=True` のため、**redirect 先も再検証**が必要
  → proxy 用に redirect を都度 guard する fetch ラッパを `url_guard` 側に用意、
    または `follow_redirects=False` + 手動追跡 (各 hop で assert)
- timeout 厳格 (10s)、レスポンスサイズ上限 (例: 5MB) で DoS 防止

## 6. ピッカーオーバーレイ JS (selector 導出)

iframe 内 (proxy 配信ページ) で動作。親 (React) と `postMessage` で通信。

- **hover**: `mouseover` で要素に outline、`mouseout` で解除
- **click**: 
  1. クリック要素から最も近い「記事リンク」(`<a href>`) を特定
  2. 親方向に繰り返し構造を探索 — 同じ tag+class を持つ兄弟が複数ある祖先を見つけ、
     その内部の title link への相対 path から selector を組む
     (例: `.views-row` が 8 個 → `.views-row .field--name-title a`)
  3. 汎化 selector で `document.querySelectorAll` してマッチ件数を数える
  4. `postMessage({selector, titleSelector, matchCount, samples:[{title,href}...]})`
- selector 生成は **class 優先・id/nth-child は最後の手段** (robust 重視)
- 親 React 側でも受信した selector を表示 + 手動編集可能にする

> 導出ロジックは純粋関数として切り出し (`deriveSelector(el)`) frontend unit test 対象。

## 7. frontend 統合

- `AddSourceWizardV2.tsx` の `Step` union に `{kind:"html_picker", listingUrl, initialCandidate}` を追加
- HTML scrape の preview 結果画面 (`html_preview`) に「ビジュアルで選び直す」ボタン →
  `html_picker` へ遷移
- `VisualSelectorPicker.tsx`:
  - `<iframe src={proxyUrl}>` (proxyUrl = `/api/v1/sources/proxy_page?url=...`)
  - `window.addEventListener("message")` で iframe からの selector を受信
  - selector 確定で `previewHtmlListingExplicit` を叩き preview 更新
  - 「確定」で `confirm` step (既存) へ candidate を渡す
- LLM 自動推測 (既存 `previewHtmlListing`) を最初に走らせ、その selector を
  ピッカーに初期ハイライトとして渡す = hybrid

## 8. セキュリティ要件チェック (§4 / §12)

- [ ] SSRF guard (§5) — private/metadata IP 全拒否 + redirect 再検証
- [ ] **Playwright 経路の SSRF**: `context.route` で subresource/navigation の host も
      private/metadata なら abort (httpx より SSRF 面が広がるため必須)
- [ ] proxy endpoint を **READ_ONLY instance で無効化** (mobile 公開の open proxy 化を防止)
      → `app.py` で READ_ONLY 時は router を include しない or endpoint 内で 403
- [ ] Playwright 同時実行を Semaphore で制限 + 全体 timeout (browser 枯渇 / DoS 防止)
- [ ] `<script>` / `on*` / CSP meta を sanitize してから配信
- [ ] レスポンスに `Cache-Control: no-store`、proxy したコンテンツを永続化しない
- [ ] fetch サイズ上限 + timeout で DoS 防止
- [ ] 既存 §12 の編集 allowlist / atomic write / git auto-commit (register 経路は不変)

## 9. テスト計画

- **unit**:
  - `test_url_guard.py`: 各種 private/public IP、redirect、scheme、DNS rebinding 風ケース
  - `test_source_proxy.py`: script 除去 / base href 注入 / on* 除去
  - frontend: `deriveSelector()` の繰り返し構造汎化 (jsdom)
- **integration**:
  - `test_source_proxy_endpoint.py`: READ_ONLY=1 で 403 / private URL で 400 /
    正常 URL (static) で sanitized HTML 返却 / `render=js` で Playwright 経路
    (Playwright は重いので `@pytest.mark.integration` で分離、CI 任意)
  - `page_renderer` の route abort が private host を弾くこと (モック)
- **e2e** (任意): MERICS (static) + 既知 JS-SPA サイト (js) の 2 系統で proxy →
  クリック相当 → explicit preview で記事取得を検証
- カバレッジ 80% 維持

## 10. フェーズ分割 (段階リリース)

- **Phase 1 (MVP)**: SSRF guard + 静的/JS 両対応 proxy (auto fallback + 手動トグル) +
  Playwright `page_renderer` + sanitize + ピッカー JS + explicit preview + wizard 統合。
  **server-rendered (MERICS) と JS-SPA の両方を最初から対象**
- **Phase 2 (任意)**: ピッカーで title/date/summary フィールドも個別指定 (現状 title のみ)
- **Phase 3 (任意)**: 描画 HTML の短期キャッシュ (同一 URL 再クリックの再描画コスト削減)

## 11. リスク / 未確定事項

- **iframe 内 CSS が崩れるケース**: base href で大半は解決するが、CSP を厳格に持つ
  サイトの CSS が CORS で弾かれる可能性 → 描画が崩れてもクリック自体は DOM 構造に
  依存するので selector 導出は機能する (見た目だけの問題)
- **bot-block サイト**: fetch_text の browser-UA fallback で多くは取れる。Cloudflare JS
  challenge 系は static 不可だが `render=js` の Playwright 経路で突破できる場合がある
  (Grok fetcher の実績あり)。それでも抜けない challenge は対象外と割り切る
- **Playwright のレイテンシ/重さ**: 描画は数秒かかり browser はメモリを食う。auto 判定で
  static で済むサイトは Playwright を起動しない設計 + Semaphore で枯渇を防ぐ
- **JS 描画後の `<base href>` と SPA ルーティング**: 描画後 HTML を静的配信するので
  iframe 内の SPA 再ナビゲーションは無効化 (script 除去済) → 選択操作には支障なし
- **selector 汎化の精度**: 多階層ネスト/不規則 DOM で誤汎化の可能性 → 手動 selector
  入力欄を必ず併設して escape hatch とする
- **LLM 経路の存続**: ピッカー追加後も LLM 自動提案は初期値として残す (撤去しない)

## 12. 見積もり感

MVP (Phase 1) で backend 4 ファイル (url_guard / page_renderer / _source_proxy +
sources.py 変更) + frontend 3 ファイル + テスト 4 ファイル程度。技術的な山は
(1) SSRF guard (httpx + Playwright route の二重防御)、(2) ピッカー JS の selector
繰り返し汎化、(3) static/js auto 判定。1 機能として独立コミット単位。
