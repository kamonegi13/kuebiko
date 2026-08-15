# Deployment Guide (Phase 1.5)

CTI Briefing Pipeline の Docker ベース運用ガイド。

> Phase 1 で利用していた launchd は廃止。スケジューリングは APScheduler
> (コンテナ内部) が担当し、コンテナの自動起動は Docker ランタイムの
> auto-start 機能 + `restart: unless-stopped` で完結する。

---

## 1. 前提条件

- macOS (Intel / Apple Silicon どちらでも可)
- **Ollama がホストネイティブで稼働** (`brew services start ollama`)
  - `ollama list` で `gemma4:31b` (または暫定 `gemma3:27b`) が見える
  - Metal 加速のためコンテナ化はしない
- **Docker ランタイム** のいずれか:
  - **OrbStack** (推奨): Apple Silicon ネイティブ、起動 2 秒、メモリ ~200MB
  - **Colima**: `brew install colima && colima start`
  - **Docker Desktop**: 商用ライセンスに注意
- `.env` が記入済み (`.env.example` からコピー。Discord webhook 5 本 + `POSTGRES_PASSWORD` が必須)
- ホストに `git` (UI からの auto-commit 用)

### Claude Code サブスク連携 (任意、2026-07-24 sidecar 化)

外部 LLM をサブスク枠 (Pro/Max) で使う場合。**ホストへの CLI 導入や LaunchAgent は不要**
(旧方式から移行済み — ホスト前提は上記 Ollama のみ):

1. `docker compose up -d claude-bridge` — sidecar が claude CLI を永続 volume
   (`./data/claude-bridge`) へ自己インストール/更新する (イメージ再ビルド不要。
   版固定したい場合は `.env` に `CLAUDE_BRIDGE_AUTOUPDATE=0`)
2. 長期トークンを発行し **UI「設定 → モデル」の Claude Code カードに貼付**
   (→ `.env` の `CLAUDE_CODE_OAUTH_TOKEN`。保存は bridge 再起動なしで即時反映)。
   発行はホストに claude が無くても sidecar 内で完結できる:
   ```bash
   docker exec -it -e HOME=/data/claude-home claude-bridge \
     /data/claude-home/.local/bin/claude setup-token
   # 表示された URL をブラウザで開き認可 → コード貼付 → トークンが出力される
   ```
3. `.env` に `CLAUDE_CODE_BRIDGE_URL=http://claude-bridge:8010`

**rollback (旧ホスト方式)**: `.env` の `CLAUDE_CODE_BRIDGE_URL` 行を削除し、ホストで
`bash scripts/install_claude_bridge_launchagent.sh` を再実行 (CLI 導入 +
`claude login` が必要)。bridge スクリプトは両方式共通。

---

## 2. 初回セットアップ

```bash
bash scripts/setup.sh
```

スクリプトは以下を実行:
1. Docker ランタイム検出 (OrbStack / Colima / Docker Desktop)
2. `docker compose` プラグインの確認
3. `.env` 雛形のコピー (存在しない場合)
4. `data/` `logs/` ディレクトリ作成
5. ホスト Ollama の疎通確認

---

## 3. ビルドと起動

```bash
docker compose build
docker compose up -d
```

`up -d` でデタッチモード起動。コンテナは `restart: unless-stopped` ポリシーにより:
- クラッシュ時に自動再起動
- Mac 再起動時に Docker ランタイム auto-start に追従して自動復活
- ユーザが `docker compose down` した場合のみ停止状態を保持

ブラウザで管理画面を開く:

```bash
open http://127.0.0.1:8001/
```

---

## 4. Mac 起動時の自動立ち上げ

### OrbStack (推奨)
1. OrbStack を起動 → 設定 → "Start at login" を ON
2. 一度 `docker compose up -d` を実行しておけば、Mac 再起動後にコンテナが自動復活

### Colima
```bash
brew services start colima
```
で常駐化。起動後は `docker compose up -d` で OK。

### Docker Desktop
設定 → General → "Start Docker Desktop when you sign in" を ON。

---

## 5. 動作確認

### 5.1 コンテナ状態

```bash
docker compose ps
docker compose logs -f kuebiko
```

### 5.2 死活エンドポイント (機械可読)

```bash
curl -s http://127.0.0.1:8001/api/health | jq
```

### 5.3 即時実行 (UI から)

ブラウザで `/runs` → 「dry-run」をチェックして「実行」。
ライブログがそのまま画面に流れる。dry-run で内容を確認してから本番投稿する。

### 5.4 即時実行 (CLI から、コンテナ外で開発時)

```bash
uv run python -m src.main --dry-run --max-articles 1
```

---

## 6. ログの見方

structlog の出力は **stdout (JSON Lines)** に流れる (12-Factor App)。
`docker compose logs` で取得できる。

```bash
# 末尾追跡
docker compose logs -f kuebiko

# JSON フィルタ (jq)
docker compose logs kuebiko | jq 'select(.level=="error")'
docker compose logs kuebiko | jq 'select(.event=="pipeline_complete")'
docker compose logs kuebiko | jq 'select(.event=="ollama_response") | {duration: .duration_seconds, tokens: .output_tokens}'
```

履歴の検索性は **SQLite (`data/run_history.db`)** で確保。Web UI の `/history` タブから閲覧。

---

## 7. 設定変更

### 7.1 `.env` (シークレット) — 不可視の保存層 (2026-07-24 画面統合)
UI の「.env タブ」は廃止。**接続を持つ設定は対象画面で設定+死活+状態を一体管理**する。
さらに初期設定・復旧のワンストップとして **設定 → 接続タブ (既定タブ)** に同じカードを
集約している (同一コンポーネント・同一 API の再掲 — どちらで編集しても同じ):

| 設定 | 編集画面 | 死活表示 |
|---|---|---|
| Discord webhook (チャンネル別) | 情報フロー → チャンネル編集 Drawer | チャンネルカードの疎通ドット (GET 検証) |
| IMAP (Grok メール受信) | 購読ソース → 「Grok メール受信」カード | 同カードの接続テスト (ログイン試行) |
| Ollama 接続 URL | 設定 → モデルタブの ollama 行 | 同行の稼働ドット |
| 外部 LLM キー (Anthropic / 接続先) | 設定 → モデルタブの接続先パネル | 同パネル |
| LOG_LEVEL / TIMEZONE | 設定 → システムタブ | 不要 |
| モバイル公開 tunnel | ジョブ管理 → 「モバイル公開」カード | 同カード (URL 検出) |

**`.env` ファイル自体は保存層として存続** (書込は従来どおり allowlist + `.bak` + atomic
rename、シークレットは常にマスク表示)。UI が使えない障害時はホストで `.env` を直接
編集してよい — AppConfig は request ごとに再読込するため多くのキーは再起動不要で反映
される (例外は §7.4)。

### 7.2 `config/*.yaml`
Web UI の `/config` (yaml タブ) から編集。pydantic スキーマで検証してから保存。

### 7.3 `prompts/*.j2`
Web UI の `/config#prompts` から編集。保存前に dry-run プレビューが可能。

### 7.4 反映タイミング
- `.env`: **原則 即時反映** (AppConfig が request ごとに再読込)。例外:
  `LOG_LEVEL` はアプリ本体のログ出力にはコンテナ再起動まで反映されない (保存後に
  起動するパイプライン subprocess には反映される)。`TIMEZONE` は表示系のみ即時、
  スケジューラの cron 解釈は Asia/Tokyo 固定
- `config/*.yaml`: 次回 `run_pipeline()` 呼び出し時に再ロード
- `prompts/*.j2`: 次回 `run_pipeline()` 呼び出し時に再ロード
- スケジュール (cron 時刻): UI の `/schedule` から即時反映

---

## 8. スリープと自動実行

**Mac が完全にスリープしている時刻は Docker コンテナも停止する**
(Docker Desktop / OrbStack / Colima の VM が一緒にスリープするため)。

APScheduler は `coalesce=True` + `misfire_grace_time=3600` で **1 時間以内のずれを
キャッチアップ実行** する設計。

### 確実に 05:00 に実行したい場合

```bash
# 平日 04:55 に Mac を Wake させる (要 sudo)
sudo pmset repeat wake MTWRFSU 04:55:00
pmset -g sched   # 確認
```

`MTWRFSU` で月〜日。週末も含めて毎日 Wake。

---

## 9. アップデート

```bash
git pull
docker compose build
docker compose up -d
```

依存関係に変更があった場合は build が走る。Phase 5 の責務別パッケージ
再構成までは API 互換性は保たれる想定。

---

## 10. トラブルシューティング

### 10.1 コンテナが起動しない
```bash
docker compose logs kuebiko | tail -50
```
よくある原因:
- `.env` の必須項目が空 (Discord webhook 5 本 / `POSTGRES_PASSWORD`)
- `data/` への書き込み権限が UID 1001 にない
  → ホストで `chown -R 1001:1001 data logs` (Linux) もしくは
    Docker 側で USER 1001 マウントの調整

### 10.2 Ollama に接続できない (`Ollama サーバに接続できません`)
- `ollama list` でモデルが見えるか確認
- `curl http://localhost:11434/api/tags` でホストから疎通確認
- コンテナから: `docker compose exec kuebiko curl http://host.docker.internal:11434/api/tags`

### 10.3 RSS フィードが取得できない (`購読ソース` が赤)
- 購読ソース画面の疎通ドットで層別に切り分ける (取得成功 / 本文抽出失敗)
- 403 が続く場合は `src/tools/fetch_policy.py` の UA エスカレーションを確認
- feed URL 自体の廃止は `uv run python scripts/verify_feed_urls.py` で一括検査

### 10.4 Discord 投稿が `webhook 投稿失敗: HTTP 401`
- webhook URL が再生成された可能性
- Discord チャンネル設定 → 連携サービス → ウェブフックで URL を確認
- Web UI の `/config` で `.env` を更新

### 10.5 git auto-commit が失敗 (UI からの設定編集後)
- `.git` ボリュームがマウントされているか確認
- コンテナ内の git ユーザ設定: `docker compose exec kuebiko git config --global user.email cti@local`
  (デフォルトは UID 1001 のホスト側 git config を継承)

### 10.6 文字化け (CJK)
- compose の `LANG=ja_JP.UTF-8` が有効か `docker compose exec kuebiko locale` で確認

### 10.7 Phase 2: Grok pipeline で「matched: 0」になる
- IMAP 認証は通っているが該当メールが見つからない場合:
  1. Gmail の対象メールが INBOX に届いているか (アーカイブされていないか)
  2. 既読 (Seen) になっていないか — 既定では UNSEEN のみ取得する
  3. 既読も拾いたい場合は `config/pipelines.yaml` の grok-briefing で
     `grok_unseen_only: false` に変更
  4. `grok_lookback_minutes` の窓を広げる (例: 7 日 = 10080)
- Playwright セッション state が無い場合、Grok の URL 取得時に
  `session_expired` で skip される。下記の初回ログイン手順を実行:

```bash
# ホスト側の Python で headed Chromium を立ち上げる (Mac の GUI 必須)
uv run python scripts/grok_login.py

# 開いた Chromium で grok.com / x.com にログイン →
# ターミナルで Enter → data/playwright/state.json が保存される

# 確認: コンテナ内から実行
docker compose exec kuebiko python -m src.main \
  --pipeline grok-briefing --dry-run --max-articles 1
```

### 10.8 grok_login.py で x.com にユーザ名/パスワードを入力できない

x.com (Twitter) のログインフォームは Playwright/Chromium の自動化を検知すると
JavaScript レベルでフォーム入力を遮断する。本リポジトリの `scripts/grok_login.py`
は以下の対策を入れている:

- `--disable-blink-features=AutomationControlled` で `navigator.webdriver` を抑制
- `ignore_default_args=["--enable-automation"]` で自動化フラグを除去
- `add_init_script` で `navigator.webdriver / plugins / languages` をパッチ
- `launch_persistent_context` (`--user-data-dir data/playwright/profile`) で
  実 Chrome に近いプロファイルを永続化

それでも入力できないときの代替手段:

1. **Grok 直接ログイン**: x.com を経由せず `https://grok.com` の
   "Continue with Google" / "Continue with Apple" SSO を使う:
   ```bash
   uv run python scripts/grok_login.py --start-url https://grok.com
   ```

2. **Chromium URL バーで手動入力**: スクリプトが起動したブラウザの
   URL バーは生きている。URL を直接入力して別ページから x.com に戻ると
   入力できることがある。

### 10.8.1 ログインボタン押下時に "Unexpected token '<' ... is not valid JSON" エラー

これは **TLS/HTTP fingerprint** レベルでサーバ側ボット管理 (Arkose,
Akamai, Cloudflare 等) に検知されている兆候。クライアント側の
`navigator.webdriver` パッチでは突破できない。bundled Chromium の TLS
handshake が実 Chrome と微妙に異なるためフィルタされる。

**対策**: 実 Chrome を使う (default)。

```bash
# 実 Chrome で起動 (default)
uv run python scripts/grok_login.py
# 明示指定
uv run python scripts/grok_login.py --channel chrome
# bundled Chromium に戻す (デバッグ用)
uv run python scripts/grok_login.py --channel ''
```

実 Chrome は `/Applications/Google Chrome.app` (macOS) を Playwright が
そのまま起動するので、TLS/HTTP/2 fingerprint が実ブラウザのものになる。

### 10.8.2 "Sorry, you have been blocked" / "You are unable to access x.ai"

これは Cloudflare による IP / 振る舞いベースのフルブロック。実 Chrome
を使っても通らない場合は以下の選択肢がある:

#### A. Google SSO を使う (一番簡単)

x.ai (xAI) のログインフォームを直接たたかず、Grok の "Continue with
Google" / "Continue with Apple" を使う。Google 側の auth flow なので
x.ai の Cloudflare を経由しない:

```bash
uv run python scripts/grok_login.py --start-url https://grok.com
# Grok のページで "Continue with Google" を押す
```

#### B. 普段使い Chrome から cookie だけ抜き出す (推奨)

Playwright で Chrome を起動するアプローチは Cloudflare 検知を通すのが
難しく、実プロファイルへ接続すると Chrome の自動ログアウト保護機構が
発動して **拡張機能やセッションが消える** 不具合があった。Playwright の
プロセスに一切触れず、Chrome の SQLite cookie ファイルを read-only で
読み取る方式に切り替える:

```bash
# 1. 普段使いの Chrome で grok.com にログイン (Google SSO / 通常ログイン)
# 2. Chrome は **起動したまま** で良い (read-only でアクセスする)
uv run python scripts/grok_extract_cookies.py
# → macOS キーチェーンのダイアログが出たら "Allow" を押す
# → data/playwright/state.json に cookie が書き出される

# 3. 結果確認 (auth cookie が含まれているはず)
docker compose exec kuebiko python -m src.main \
    --pipeline grok-briefing --dry-run --max-articles 1
```

依存: `browser-cookie3` (既に pyproject.toml に追加済み)。
macOS Chrome の AES 暗号化 cookie をキーチェーン連携でデコードする。

#### C. ネットワーク (IP) を変える

Cloudflare の Block は ASN/IP reputation に基づくことが多い:

- 別ネットワーク (テザリング、別 Wi-Fi) で実行
- VPN は逆に弾かれることが多いので注意

3. **既ログインの Chrome プロファイルをコピー** (上級者向け):
   既に手元の Chrome で x.com にログイン済みなら、
   `~/Library/Application Support/Google/Chrome/Default/Cookies` を
   `data/playwright/profile/Default/Cookies` に複製してから
   `scripts/grok_login.py` を起動する。
   profile 単位の Cookie 流用なので、その後 Enter を押せば state.json が出来る。

---

## 11. アンインストール

```bash
docker compose down
docker rmi kuebiko:latest
rm -rf data/ logs/  # 履歴も全削除する場合のみ
```

`.env` と `config/` は手動で残すか削除するか選択。

---

## 12. 注意

- **失敗時の自動リトライはしない** (誤投稿リスク回避)。失敗した日は次の cron に持ち越し
- **構造化ログは stdout のみ**。ファイルに書かない (12-Factor、Docker logs に流れる)
- **編集 allowlist**: `prompts/*.j2`, `config/*.yaml`, `.env` 以外は Web UI から編集不可 (CLAUDE.md §12)
- **127.0.0.1 のみバインド**。LAN/外部公開は `docker-compose.yml` の `ports` を変更しない限り発生しない

## OrbStack 無応答の自動復旧 watchdog (2026-08-02)

本機はサーバではなくノート PC で、持ち出し中はスリープする。**スリープ自体は前提条件**で、
収集は wake 後の APScheduler misfire 追い付きで自己回復する (実測: スリープを挟んでも
記事の取りこぼしゼロ。遅れるのは配信時刻のみ)。

問題は **VM が wake に失敗して固まるケース** (2026-07-10 実績) で、これだけは人が
気づくまで完全停止する。それを「数十分の遅延」に格下げするのがこの watchdog。

**制御の分担** (モバイル公開トンネルと同じ作法):

| 操作 | どこから |
|---|---|
| 導入 / 削除 | ターミナル (UI はコンテナ内なので launchctl を操作できない) |
| 有効 / 無効 | **Web UI のジョブ管理 → ホスト復旧 watchdog** のトグル (フラグファイル経由) |
| 状態 / 復旧履歴 | 同カードに表示 (state.json と watchdog.log は `data/host_watchdog/`) |

```bash
bash scripts/install_orbstack_watchdog_launchagent.sh            # 導入 / 更新
bash scripts/install_orbstack_watchdog_launchagent.sh --uninstall # 削除
tail -f data/host_watchdog/watchdog.log                           # ログ
```

**Linux サーバへ移す場合は何もしなくてよい**: スクリプトは macOS + orbctl のある環境
でのみ動き、それ以外では痕跡も残さず即終了する。導入しなければ実行時フットプリントは
ゼロ (Docker イメージにも compose にもジョブレジストリにも入っていない)。

5 分間隔で `docker ps` の応答性を見る。誤爆すると in-flight のパイプラインを殺すため、
安全弁を 4 つ持つ:

| 安全弁 | 目的 |
|---|---|
| macOS + orbctl がある環境でのみ動く | Linux サーバでは即 no-op (痕跡も残さない) |
| UI で有効化されているときだけ動く | 使わない期間は完全に停止できる |
| OrbStack が `Running` のときだけ介入 | 意図的な停止を尊重する |
| wake 後 180 秒は判定しない | VM の復帰待ち。DarkWake (約 45 秒) での誤爆を構造的に防ぐ |
| 連続 3 回失敗して初めて復旧 (≒15 分) | 一過性のブリップで再起動しない |
| 復旧は 1 時間 3 回まで | 再起動ループの防止。超過は Discord ops に通知して停止 |

復旧は `orbctl restart` → 効かなければ `vmgr` helper 再生成 + `orbctl start` の順
(2026-07-10 に唯一効いた手順)。**docker が無応答 = パイプラインも動いていない**ので、
復旧によって失う in-flight 処理は無い。正常時は完全に無音 (ログも書かない)。
