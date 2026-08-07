#!/bin/sh
# PostgreSQL 日次バックアップ ループ (Phase 0 F1)。
#
# decoupled な compose sidecar (backup, postgres:16-alpine) で実行する。
# app コンテナが障害でも独立して動くため、バックアップの本旨 (app 障害時こそ必要) に合致。
#
# 動作:
#   - 起動時に即時 1 回、以降は 24h 間隔で pg_dump (custom format -Fc)。
#   - 出力: /backups/kuebiko_<UTC timestamp>.dump (= host の ./data/backups/)。
#   - BACKUP_RETENTION_DAYS (既定 14) より古い dump を削除 (rotation)。
#   - 成否を stdout に記録 (docker logs backup で確認)。
#
# 復元: scripts/restore_db.sh <dump-file> を参照。
set -u

BACKUP_DIR="${BACKUP_DIR:-/backups}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
PGHOST="${PGHOST:-postgres}"
PGUSER="${PGUSER:-kuebiko}"
PGDATABASE="${PGDATABASE:-kuebiko}"
INTERVAL_SECONDS="${BACKUP_INTERVAL_SECONDS:-86400}"

mkdir -p "$BACKUP_DIR"
echo "[backup] sidecar started: dir=$BACKUP_DIR retention=${RETENTION_DAYS}d interval=${INTERVAL_SECONDS}s host=$PGHOST db=$PGDATABASE"

while true; do
  ts="$(date -u +%Y%m%d_%H%M%S)"
  out="$BACKUP_DIR/kuebiko_${ts}.dump"
  if pg_dump -h "$PGHOST" -U "$PGUSER" -d "$PGDATABASE" -Fc -f "$out"; then
    sz="$(du -h "$out" 2>/dev/null | cut -f1)"
    echo "[backup] ok $(date -u +%FT%TZ) -> $out ($sz)"
    # rotation: 古い dump を削除
    find "$BACKUP_DIR" -name 'kuebiko_*.dump' -type f -mtime +"$RETENTION_DAYS" -delete 2>/dev/null || true
  else
    rc=$?
    echo "[backup] FAILED $(date -u +%FT%TZ) (pg_dump exit=$rc)" >&2
    rm -f "$out" 2>/dev/null || true  # 壊れた/空 dump を残さない
  fi
  sleep "$INTERVAL_SECONDS"
done
