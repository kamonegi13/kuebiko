#!/usr/bin/env bash
# 公開サイト (標準面) を作り直し、アドバンスドと合わせて配信する。
#
#   scripts/deploy_public_site.sh [--force]
#
# ⚠ 2026-10-05: 標準面とアドバンスド (旧写し) は同じ Cloudflare Pages プロジェクトへ
#    1 回でまとめて配信する (scripts/deploy_site.sh)。このスクリプトは旧名を
#    維持するための薄いラッパ。LaunchAgent は scripts/deploy_site.sh public を
#    直接呼ぶ (scripts/install_public_site_launchagent.sh)。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec "$ROOT/scripts/deploy_site.sh" public "$@"
