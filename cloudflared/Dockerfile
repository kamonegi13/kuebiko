# Cloudflare Tunnel sidecar for Phase Diamond verify-mobile.
# Phase 5T-X: supervisor pattern で flag file による real-time toggle 可能。
# - readonly (port 8000 in container) を quick tunnel で外部公開
# - URL は /app/data/.mobile_tunnel_url に出力 (main app が API 経由で読む)
# - flag file (/app/data/.mobile_tunnel_enabled) で on/off 制御

FROM alpine:3.20

# 版を固定する (latest だと再ビルドしない限り古い版が残り、更新の有無も分からない)。
# 2026.10.2 未満は弱い鍵の中間証明書を信頼するため、それ以降が必須 (2026-10 の Cloudflare 告知)。
ARG CLOUDFLARED_VERSION=2026.10.3

RUN apk add --no-cache curl bash ca-certificates \
 && ARCH=$(uname -m) \
 && case "$ARCH" in \
      x86_64)  CF_ARCH=amd64 ;; \
      aarch64) CF_ARCH=arm64 ;; \
      *) echo "Unsupported architecture: $ARCH" >&2; exit 1 ;; \
    esac \
 && curl -fSL "https://github.com/cloudflare/cloudflared/releases/download/${CLOUDFLARED_VERSION}/cloudflared-linux-${CF_ARCH}" \
        -o /usr/local/bin/cloudflared \
 && chmod +x /usr/local/bin/cloudflared

COPY launcher.sh /launcher.sh
RUN chmod +x /launcher.sh

# /app/data は docker-compose で host から bind mount される (main app と共有)
WORKDIR /app

ENTRYPOINT ["/launcher.sh"]
