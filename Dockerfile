# CTI Briefing Pipeline - 単一コンテナ image (Phase 1.5)
#
# - 多段ビルドで slim image を作る
# - 非 root ユーザ (UID 1001) で実行 (CLAUDE.md §12)
# - ホスト側 Ollama に host.docker.internal:11434 で接続する想定
# - 環境変数 TZ=Asia/Tokyo は docker-compose で注入

# ---- Stage 0: frontend builder (React SPA) ----
FROM node:22-alpine AS frontend-builder
WORKDIR /frontend
COPY frontend/package.json ./
# Use npm install (no lock yet) — generate lock at first build
RUN npm install --no-audit --no-fund --prefer-offline
COPY frontend/ ./
RUN npm run build

# ---- Stage 1: builder ----
FROM python:3.12-slim AS builder

# uv は単一バイナリ。pip 経由でインストール (キャッシュレイヤを最小化)
RUN pip install --no-cache-dir uv==0.11.6

WORKDIR /app

# 依存解決のために pyproject.toml + uv.lock のみ先にコピー (レイヤーキャッシュ)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Phase 2: Playwright Chromium をインストール (browser binary を /ms-playwright に)
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN /app/.venv/bin/python -m playwright install chromium

# ソース類を取り込む
COPY src/ ./src/
COPY config/ ./config/
COPY prompts/ ./prompts/
# Phase 0: maintenance スクリプト (backfill / recover 等) を image に含め、
# docker exec ... python -m scripts.X / scripts/X.py を機能させる。
COPY scripts/ ./scripts/

# ---- Stage 2: runtime ----
FROM python:3.12-slim AS runtime

# CJK を含むログのために locale を有効化 + Playwright が必要とする OS 共有ライブラリ
RUN apt-get update && apt-get install -y --no-install-recommends \
        locales tini ca-certificates \
        # Phase E/D auto-commit: Web UI から yaml 編集時の git operations 用
        git \
        # Playwright Chromium が必要とする shared libraries
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
        libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
        libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    && sed -i 's/# *ja_JP.UTF-8 UTF-8/ja_JP.UTF-8 UTF-8/' /etc/locale.gen \
    && locale-gen ja_JP.UTF-8 \
    && rm -rf /var/lib/apt/lists/*

# 非 root ユーザ
RUN groupadd -g 1001 cti && useradd -u 1001 -g 1001 -m -s /bin/bash cti

WORKDIR /app

# builder からの virtualenv とソースをコピー
COPY --from=builder --chown=cti:cti /app /app
# Playwright Chromium バイナリも builder から
COPY --from=builder --chown=cti:cti /ms-playwright /ms-playwright
# React SPA build を frontend/dist にコピー (FastAPI が /app 配下で serve)
COPY --from=frontend-builder --chown=cti:cti /frontend/dist /app/frontend/dist

# ボリュームマウント先 (compose で host にバインド)
RUN mkdir -p /app/data && chown -R cti:cti /app /ms-playwright

USER cti

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LANG=ja_JP.UTF-8 \
    LC_ALL=ja_JP.UTF-8 \
    PATH="/app/.venv/bin:${PATH}" \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    CTI_PROJECT_ROOT=/app

EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://127.0.0.1:8000/api/health', timeout=3.0)" || exit 1

# tini で PID 1 として動作 (シグナル伝達 + zombie reaping)
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "src.ui.app:app", "--host", "0.0.0.0", "--port", "8000"]
