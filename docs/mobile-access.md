# Mobile アクセス設定 (Phase Diamond verify-mobile)

スマホ等の外部端末から本ツールの **閲覧専用 view** に接続するための設定手順。

`docker compose up -d` で **自動起動 + Discord ops ch へ URL 通知** + UI から **real-time toggle** が可能。

## 構成概要

```
[ローカル Mac]
  ├── kuebiko (port 8001, full mode)     ← PC からのみアクセス、write 可
  ├── readonly (port 8002, READ_ONLY=1)
  │     └── 全 POST/PUT/PATCH/DELETE を 403 で block (アプリ層 middleware)
  │     └── /api/v1/runtime-flags が read_only=true を返す → frontend が write button を hide
  └── tunnel (container、自動起動)
        ├── supervisor loop が data/.mobile_tunnel_enabled (flag file) を 2 秒間隔で監視
        ├── flag file あり: cloudflared quick tunnel 起動 → 8002 を外部公開 → URL を data/.mobile_tunnel_url に出力
        ├── 初回 URL 検出時に DISCORD_WEBHOOK_OPS に notify
        └── UI の POST /api/v1/mobile-tunnel/{enable,disable} で flag file 操作 → real-time on/off

[外出先スマホ]
        └── Cloudflare URL でアクセス → 閲覧専用 UI を見る
```

## なぜ 2 instance ?

- **構造的に write 不可** を保証するため。認証ゲートだけでは「認証を突破すれば write 可能」になるが、本構成は **物理的に write API が 403 で固定**。
- 一方の PC ローカル運用 (127.0.0.1:8001) は従来通り全機能利用可能。

詳細な設計判断は CLAUDE.md §12 を参照。

## 到達範囲の 3 層 (2026-08-01)

公開 instance の到達範囲は `src/ui/read_only_policy.py` が **一箇所で** 決める (SSoT)。

| 層 | 誰が | 何ができるか |
|---|---|---|
| **Tier0 匿名** | 公開 URL を知る全員 | 閲覧系 read API と SPA (ブリーフ / ニュース / アクター / PIR / 地図 / 履歴 等) |
| **Tier1 認証済み** | Cloudflare Access を通過した利用者 | 上記 + 運用系 read API の閲覧 (ジョブ計画 / 設定 / プロンプト / ルーティング / レビューキュー) + **ジョブ即時実行** + 分析チャット・記事翻訳 |
| **Tier2 ローカル専用** | 127.0.0.1:8001 の full instance のみ | 設定保存 / API キー / プロンプト保存 / モデルティア / ソース編集 / tunnel 管理 / レビュー承認 |

- Tier0/Tier1 の境界は `READ_ONLY_GET_DENYLIST` (path-prefix)。フロントの `nav.ts` の
  `fullOnly` は表示上の隠蔽で、**遮断の実体は常にサーバ側**。
- Tier1 の write は **ジョブ即時実行 1 つだけ**。readonly コンテナは scheduler を起動しない
  ため、認証済みの `POST /api/v1/jobs/{id}/run` を docker 内部ネットワーク経由で
  full instance (`FULL_INSTANCE_URL`、既定 `http://kuebiko:8000`) へ転送する。
  **write の実行主体は常に full instance** なので §12 の境界は保たれる。
- **Access 未設定なら Tier1 は存在しない** (匿名 = Tier0 のみ)。ただし段階導入のため、
  未設定時に限り分析チャット・記事翻訳は従来どおり匿名で使える。

### 認証の監査証跡 (2026-08-02)

Tier1 の認証イベントは DB (`access_audit`) に永続化される。**成功・失敗の両方**を残すのが
要点 — 失敗しか記録しないと「認証層が正常稼働している」と「一度も使われていない」を
区別できない (実際にこの誤判定を踏んだ)。stdout ログはデプロイ時のコンテナ再作成と
ログローテーションで消えるため、監査証跡としては DB 永続が必須。

| 事象 | 記録タイミング |
|---|---|
| `authenticated` | 認証済みで運用系 API に到達 (同一 subject は 10 分間隔に集約) |
| `rejected` | 資格情報を提示したが検証に失敗 (期限切れ / 偽造) |
| `tier1_write` | 認証済みのジョブ即時実行 (集約せず必ず記録) |

トークン無しの匿名アクセスは記録しない (公開 URL には bot が来るため証跡が埋もれる)。
**email は保存しない** (§4) — 識別は subject の SHA-256 先頭 12 桁。異常な所在地の検知の
ため接続元 IP と `Cf-Ipcountry` を残す。retention は 180 日 (run_logs の 30 日より長い —
不正の発覚は遅れる前提)。

参照:

```bash
curl -s http://127.0.0.1:8001/api/v1/access-audit | python3 -m json.tool
```

この endpoint は `READ_ONLY_GET_DENYLIST` に登録済みで、公開 instance からは匿名で読めない
(監査証跡自体を公開面に出さない)。

### Cloudflare Access の設定手順 (Tier1 を有効化する場合)

> ダッシュボードは 2026-07 に **Cloudflare One** UI へ刷新され、旧 "Zero Trust > Access >
> Applications" は **「アクセス制御」→「アプリケーション」** に移動した。以下は新 UI の導線
> (トップページ右カラム 推奨事項の「Access でアプリケーションを保護する」からも同じ画面に入る)。

1. **チームドメインを確認**: Zero Trust トップ →左ナビ **設定** (または 再利用可能な部品 →
   カスタムページ → チーム名とドメイン)。`<team>.cloudflareaccess.com` が
   `CF_ACCESS_TEAM_DOMAIN` の値。アカウント詳細の「チーム名」表示と実ドメインは
   一致しないことがあるので、次のコマンドで疎通確認してから使う:
   ```bash
   curl -s https://<team>.cloudflareaccess.com/cdn-cgi/access/certs | head -c 120
   # JWK の JSON ({"keys":[...]}) が返れば正しい
   ```
2. 左ナビ **アクセス制御 → アプリケーション → アプリケーションを作成**
   → **セルフホストとプライベート** → **パブリックホスト名を追加**。
   - ドメイン: `kuebiko.example`
   - **パス: `auth`** ← apex 全体ではなく `/auth/*` にだけ被せるのが肝。
     これで匿名の Tier0 閲覧は維持されたまま、ログインしたい人だけが認証を通る
     (Access は**より具体的なパスを優先**するため、apex は無保護のまま残る)
   - アプリ名: `kuebiko auth` / セッション期間: 1 か月程度 (毎回のログインを避ける)
3. **Access ポリシー**: Action=*Allow*、Include=*Emails* に自分のアドレス
   (メール OTP のワンタイムコード。IdP 連携は不要)。
   Access は既定で deny — Allow ポリシーが 1 つ必要。
4. 作成したアプリの **設定 (Configure) → 追加設定 (Additional settings)** に表示される
   **Application Audience (AUD) Tag** をコピー。
5. `.env` に 2 行追加してデプロイ:
   ```bash
   CF_ACCESS_TEAM_DOMAIN=<team>.cloudflareaccess.com
   CF_ACCESS_AUD=<AUD tag>
   ```
   ```bash
   docker compose up -d --build kuebiko readonly   # tunnel は触らない (URL 不変)
   ```
6. 反映確認 (`auth_available` が true になる):
   ```bash
   curl -s https://kuebiko.example/api/v1/runtime-flags
   # {"read_only":true,"authenticated":false,"auth_available":true}
   ```
7. スマホで `https://kuebiko.example/app/` → サイドバー下部の **ログイン** → OTP → 戻ると
   運用ページが表示され、ジョブ詳細に「今すぐ実行」が出る。

検証は JWT (`Cf-Access-Jwt-Assertion` ヘッダ or `CF_Authorization` cookie) を team の
JWKS で行う (`src/ui/services/cf_access.py`)。署名不正・期限切れ・aud/iss 不一致・
鍵取得失敗はすべて **未認証に倒す (fail-closed)**。`.env` の 2 行を消せば即座に
Tier1 が消滅し、従来の匿名閲覧のみに戻る (rollback 手順)。

## 自動起動の運用 (推奨)

`docker compose up -d` だけで完結します:

```bash
docker compose up -d
```

これだけで:
- `readonly` (port 8002) が READ_ONLY=1 で起動
- `tunnel` が cloudflared quick tunnel を起動
- 取得した URL が Discord `DISCORD_WEBHOOK_OPS` ch に投稿される
  - 例: `🌐 CTI mobile read-only URL ready https://xxx.trycloudflare.com/app/`
- UI の Schedule page 上部に "🌐 Mobile Tunnel" card が表示され、現在 URL の確認 + ▶ 起動 / ⏸ 停止 が real-time でできる
  - 停止: flag file 削除 → ~2 秒で cloudflared 停止
  - 起動: flag file 作成 → ~15 秒で新 URL 発行 + Discord 再通知
- toggle 状態は flag file (`data/.mobile_tunnel_enabled`) で永続化、Mac 再起動でも保持

`MOBILE_TUNNEL_DEFAULT_ENABLED=0` を `.env` で設定すると、**初回 deploy 時のみ** OFF で起動 (それ以降は UI toggle の状態を尊重)。

### quick tunnel の URL が変わる / ops が賑やか (2026-07-05)

account-less の quick tunnel は **URL が cloudflared 再起動のたびに変わる**。主な変動要因:

1. **全 service を巻き込む再デプロイ** — `docker compose up -d --build` を無指定で打つと
   `tunnel` も毎回再作成され、その都度 新 URL → ops 投稿。**app だけ更新するときは
   `docker compose up -d --build kuebiko readonly`** で tunnel を触らない
   (URL 安定・ops 静粛)。
2. **quick tunnel のセッション切れ** — Cloudflare が account-less tunnel を随時切る。supervisor が
   到達性を監視して自動再発行する (これは正しい動作) が、その度に新 URL → ops 投稿。

現 URL は常に **Web UI のスケジュール画面** で確認できる (Discord 投稿に依存しない)。

### 恒久安定 URL: named tunnel (UI から管理・URL 不変・2026-07-30 C2)

URL を再起動でも一切変えたくない場合は named tunnel を使う (Cloudflare 無料アカウントが必要)。
**設定は Web UI から完結**し、`.env` を触る必要はない (再デプロイも不要)。

1. Cloudflare で公開に使うドメイン (例 `kuebiko.example`) を用意 (Registrar or 既存ゾーン)。
2. Cloudflare Zero Trust → Networks → Tunnels → **Create a tunnel (Cloudflared)** → 名前を付ける。
   作成画面に出る `docker run … --token eyJ…` の **token 文字列**を控える (コマンド自体は実行不要)。
3. その tunnel の **Published application (旧 Public Hostname)** を追加:
   - Domain = 公開ドメイン (Subdomain は空欄で apex)
   - Service URL = **`http://readonly:8000`** (⚠️ write 可能な 8001 側は指定しない)
4. **Web UI → 設定 → 接続 (またはジョブ管理) の「モバイル公開」カード → 固定ドメイン (named tunnel)**
   で、控えた **token** と **公開ホスト名** を入力して「保存して反映」。

保存すると token/hostname は data/ ファイル (token は 0600) に書かれ、`launcher.sh` がファイル変化を
検知して cloudflared を**自動再起動 → 数秒で反映**する (再デプロイ不要)。以後 **URL は固定** (再起動・
再デプロイでも不変) で、ops 通知はコンテナ起動につき 1 回のみ。「解除」で quick tunnel に戻せる。

**セキュリティ**: token は **API から生値が返らない** (状態は「設定済み」boolean のみ)。書き込みは
full instance 限定 (公開 readonly instance は write を 403 で遮断)。token は compose の env 注入を
廃し `--token` 引数で渡すため **docker logs にも出ない**。旧来 `.env` に `CLOUDFLARE_TUNNEL_TOKEN`
を書く方式は廃止 (env に置いても不発・秘密残留のため)。token 未設定なら quick tunnel に自動 fall back。

---

## 手動セットアップ手順 (cli から運用したい場合の参考)

### 1. docker-compose を更新

`docker-compose.yml` に `readonly` service が定義済 (本リポジトリで設定済)。

両 service を起動:

```bash
docker compose up -d --build
docker ps
# kuebiko            127.0.0.1:8001:8000  (Up healthy)
# readonly   127.0.0.1:8002:8000  (Up healthy)
```

確認:

```bash
# Read 系 API は両方 OK
curl -s http://127.0.0.1:8001/api/v1/runtime-flags  # → {"read_only": false}
curl -s http://127.0.0.1:8002/api/v1/runtime-flags  # → {"read_only": true}

# Write 系 API は readonly 側で 403
curl -s -X POST http://127.0.0.1:8002/api/v1/schedule/daily-briefing/pause
# → {"detail": "read-only instance: ...", "method": "POST", "path": "..."}
```

### 2. Cloudflare Tunnel をインストール

Mac で:

```bash
brew install cloudflared
```

### 3. Cloudflare account 作成 (無料)

[https://dash.cloudflare.com/sign-up](https://dash.cloudflare.com/sign-up) で account 作成。
無料 plan で十分。

### 4. Tunnel 認証

```bash
cloudflared tunnel login
```

ブラウザが開いて Cloudflare account へログイン → tunnel 用 cert がローカルに保存される。

### 5. Tunnel 作成

```bash
cloudflared tunnel create cti-mobile
# Tunnel ID と credentials JSON path が表示される
```

### 6. 設定ファイル作成

`~/.cloudflared/config.yml`:

```yaml
tunnel: cti-mobile
credentials-file: /Users/<your-user>/.cloudflared/<tunnel-id>.json

ingress:
  - hostname: cti-mobile.<your-domain>
    service: http://localhost:8002
  - service: http_status:404
```

(任意) ドメインを持っていなければ Cloudflare の `*.trycloudflare.com` で一時 URL 取得:

```bash
# 永続 URL なし、起動時に URL が発行される
cloudflared tunnel --url http://localhost:8002
# → https://random-name-here.trycloudflare.com
```

### 7. Tunnel を Mac サービスとして登録 (起動時自動起動)

```bash
sudo cloudflared service install
sudo launchctl start com.cloudflare.cloudflared
```

または手動で前景実行:

```bash
cloudflared tunnel run cti-mobile
```

### 8. スマホからアクセス

スマホブラウザで `https://cti-mobile.<your-domain>/app/` を開く。

確認:
- AppHeader の「設定」を click → 「read-only mode」メッセージが表示される
- 「実行管理」を click → table 表示、操作 button は出ない (詳細は `👁` で閲覧可)
- Intel Graph → Threats tab → actor list (mobile では 1 列)、tap で詳細 view 切替

## 動作確認

| 動作 | 確認方法 |
|---|---|
| Write 全 403 | スマホで `/api/v1/schedule/daily-briefing/pause` を curl してみる |
| Frontend で write button 非表示 | 各 page を見て pause/resume/trigger/編集 button がないこと |
| Mobile hamburger menu | `<md` (768px) で右上に `☰` が表示される |
| Threats tab mobile | actor list 1 列表示、actor tap で詳細に切替、← back で list に戻る |

## トラブルシューティング

### Cloudflare Tunnel が接続不能

```bash
cloudflared tunnel info cti-mobile
# Status: HEALTHY / DEAD を確認
```

### docker-compose で readonly が起動しない

```bash
docker logs readonly --tail 30
```

`READ_ONLY=1` 環境変数が反映されているか確認:

```bash
docker exec readonly env | grep READ_ONLY
# READ_ONLY=1
```

### Write button が hide されない

ブラウザのキャッシュをクリア。Frontend は `/api/v1/runtime-flags` を起動時に 1 回 fetch して `read_only` flag を見る。

## セキュリティ留意点

CLAUDE.md §12 参照。要約:

- 外部公開する場合でも **write API は構造的に 403**、コンテンツの読み取りのみ可能
- 万一 Cloudflare account を奪われた場合の damage は **閲覧のみ**
- 重大事象 (config 改竄 / pipeline 妨害) は readonly instance では不可能
