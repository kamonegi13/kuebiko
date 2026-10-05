#!/usr/bin/env bash
# アドバンスド (旧「写し」) を作り直し、公開サイトと合わせて配信する。
#
#   scripts/deploy_mirror.sh [--force]
#
# ⚠ 2026-10-05: アドバンスドは公開サイトと同じ Cloudflare Pages プロジェクトの
#    `/app/` 配下に統合した (scripts/deploy_site.sh)。このスクリプトは旧名を
#    維持するための薄いラッパ。LaunchAgent は scripts/deploy_site.sh mirror を
#    直接呼ぶ (scripts/install_mirror_launchagent.sh)。
#
# 旧サブドメイン ($CLOUDFLARE_MIRROR_PAGES_PROJECT) は中身を持たず、/app への
# リダイレクトだけを配る (scripts/deploy_mirror_redirect.sh、別経路)。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec "$ROOT/scripts/deploy_site.sh" mirror "$@"
