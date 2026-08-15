#!/usr/bin/env bash
# CTI Briefing Pipeline - 初回セットアップスクリプト (Phase 1.5)
#
# 1. Docker ランタイム検出 (OrbStack / Colima / Docker Desktop のいずれか)
# 2. .env が無ければ .env.example からコピー
# 3. data/ logs/ ディレクトリ作成
# 4. ホストネイティブ Ollama の確認
# 5. docker compose build → up -d を促すメッセージ
#
# CLAUDE.md §3 / §10 に従う。

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
PROJECT_ROOT="$(pwd)"

echo "==> CTI Briefing Pipeline セットアップ"
echo "    PROJECT_ROOT=${PROJECT_ROOT}"

# --- Docker ランタイム検出 ---
if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker コマンドが見つかりません。" >&2
    echo "       OrbStack (推奨) / Colima / Docker Desktop のいずれかをインストールしてください:" >&2
    echo "       - OrbStack: https://orbstack.dev/" >&2
    echo "       - Colima:   brew install colima" >&2
    echo "       - Docker:   https://www.docker.com/products/docker-desktop/" >&2
    exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
    echo "ERROR: 'docker compose' プラグインが利用できません。" >&2
    exit 1
fi

# ランタイムの種類を表示 (informational)
DOCKER_INFO="$(docker info 2>/dev/null || true)"
if echo "${DOCKER_INFO}" | grep -qi "orbstack"; then
    RUNTIME="OrbStack"
elif echo "${DOCKER_INFO}" | grep -qi "colima"; then
    RUNTIME="Colima"
elif echo "${DOCKER_INFO}" | grep -qi "docker desktop"; then
    RUNTIME="Docker Desktop"
else
    RUNTIME="Docker (unknown runtime)"
fi
echo "    Docker runtime: ${RUNTIME}"

# --- .env 雛形 ---
if [ ! -f "${PROJECT_ROOT}/.env" ]; then
    if [ -f "${PROJECT_ROOT}/.env.example" ]; then
        cp "${PROJECT_ROOT}/.env.example" "${PROJECT_ROOT}/.env"
        echo "==> .env を .env.example から複製しました"
        echo "    編集して必要な認証情報を埋めてください:"
        echo "      - DISCORD_WEBHOOK_ALERT/BRIEF/WATCH/OPS/JAPAN_WATCH"
        echo "      - POSTGRES_PASSWORD"
        echo "      - IMAP_USER / IMAP_PASSWORD (Grok レポート取込を使う場合)"
    else
        echo "WARNING: .env.example が見つかりません。.env を手動作成してください。" >&2
    fi
else
    echo "    .env: 存在 (上書きしません)"
fi

# --- ディレクトリ ---
mkdir -p "${PROJECT_ROOT}/data" "${PROJECT_ROOT}/logs"
echo "    data/ logs/: 作成"

# --- Ollama 接続確認 ---
OLLAMA_URL="${OLLAMA_BASE_URL:-http://localhost:11434}"
if curl -sf -o /dev/null -m 3 "${OLLAMA_URL}/api/tags"; then
    echo "    Ollama: OK (${OLLAMA_URL})"
else
    echo "    Ollama: 未起動 or 未到達 (${OLLAMA_URL})"
    echo "    macOS の場合: 'brew services start ollama' で起動してください"
fi

cat <<EOF

==> 次の手順
  1. .env を編集して認証情報を記入
     vim .env
  2. コンテナをビルドして起動
     docker compose build
     docker compose up -d
  3. ブラウザで管理画面を開く
     open http://127.0.0.1:8001/
     購読ソース (RSS / sitemap / scraper) は同梱の初期リストが自動投入される

==> 運用コマンド
  - ログ:        docker compose logs -f
  - 停止:        docker compose down
  - 再起動:      docker compose restart
  - 更新:        git pull && docker compose build && docker compose up -d

==> 詳細: docs/deployment.md を参照
EOF
