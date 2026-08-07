#!/bin/sh
# PostgreSQL バックアップからの復元 (Phase 0 F1)。
#
# 使い方:
#   ./scripts/restore_db.sh data/backups/kuebiko_YYYYMMDD_HHMMSS.dump
#
# 動作: postgres コンテナ内の pg_restore で、指定 dump を現在の kuebiko に
#   上書き復元する (--clean --if-exists で既存オブジェクトを drop してから restore)。
#   破壊的操作のため実行前に確認プロンプトを出す。
#
# 前提: postgres コンテナが稼働中であること。
set -eu

DUMP="${1:-}"
if [ -z "$DUMP" ]; then
  echo "usage: $0 <dump-file>" >&2
  echo "  例: $0 data/backups/kuebiko_20260607_033000.dump" >&2
  exit 2
fi
if [ ! -f "$DUMP" ]; then
  echo "[restore] dump が見つかりません: $DUMP" >&2
  exit 1
fi

CONTAINER="${PG_CONTAINER:-postgres}"
PGUSER="${PGUSER:-kuebiko}"
PGDATABASE="${PGDATABASE:-kuebiko}"

echo "[restore] WARNING: 現在の '$PGDATABASE' を以下で上書きします (既存データは消えます):"
echo "          dump      = $DUMP ($(du -h "$DUMP" | cut -f1))"
echo "          container = $CONTAINER  db = $PGDATABASE"
printf "続行しますか? 'yes' と入力: "
read -r ans
if [ "$ans" != "yes" ]; then
  echo "[restore] 中止しました。"
  exit 1
fi

echo "[restore] 復元中..."
docker exec -i "$CONTAINER" pg_restore -U "$PGUSER" -d "$PGDATABASE" --clean --if-exists --no-owner < "$DUMP"
echo "[restore] 完了。app を再起動して反映してください: docker compose restart kuebiko readonly"
