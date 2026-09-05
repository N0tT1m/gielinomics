#!/usr/bin/env bash
#
# Restores a dump into a database.
#
# Defaults to a scratch database rather than the live one, because the common reason to run
# this is rehearsal, and a restore script whose default target is production gets run wrong
# exactly once.
#
# Usage:  ops/restore.sh <dump-file> [target-database]
#         ops/restore.sh latest                          # newest dump -> gielinomics_restore
#         ops/restore.sh latest gielinomics              # over the live database. Asks first.
#
set -euo pipefail

cd "$(dirname "$0")/.."

DUMP="${1:?usage: ops/restore.sh <dump-file|latest> [target-database]}"
TARGET="${2:-gielinomics_restore}"
LIVE="${POSTGRES_DB:-gielinomics}"
USER="${POSTGRES_USER:-gielinomics}"
BACKUP_DIR="${GIELINOMICS_BACKUP_DIR:-/mnt/ai/backups/gielinomics}"

if [ "$DUMP" = "latest" ]; then
  DUMP=$(find "$BACKUP_DIR" -maxdepth 1 -name 'gielinomics-*.dump' | sort | tail -1)
  [ -n "$DUMP" ] || { echo "no dumps in $BACKUP_DIR" >&2; exit 1; }
  echo "==> latest dump is $DUMP"
fi

[ -f "$DUMP" ] || { echo "no such dump: $DUMP" >&2; exit 1; }

if [ -f "$DUMP.sha256" ]; then
  echo "==> verifying checksum"
  if command -v sha256sum >/dev/null 2>&1; then
    (cd "$(dirname "$DUMP")" && sha256sum -c "$(basename "$DUMP").sha256")
  else
    (cd "$(dirname "$DUMP")" && shasum -a 256 -c "$(basename "$DUMP").sha256")
  fi
else
  echo "==> no checksum alongside this dump; restoring unverified" >&2
fi

if [ "$TARGET" = "$LIVE" ]; then
  echo
  echo "!!  This will DROP and recreate '$LIVE', the live database."
  echo "!!  Everything ingested since $(basename "$DUMP") is lost."
  read -r -p "Type the database name to confirm: " confirm
  [ "$confirm" = "$LIVE" ] || { echo "aborted"; exit 1; }
fi

psql_admin() {
  docker compose exec -T postgres psql -U "$USER" -d postgres -v ON_ERROR_STOP=1 "$@"
}

echo "==> recreating $TARGET"
psql_admin -c "DROP DATABASE IF EXISTS \"$TARGET\" WITH (FORCE);"
psql_admin -c "CREATE DATABASE \"$TARGET\";"

# TimescaleDB restores need the extension present before the hypertable definitions land,
# and its own pre/post hooks run around the data load. Skipping them leaves the chunks in
# place but the hypertable catalog empty, which looks like a successful restore of no data.
psql_admin -d "$TARGET" -c "CREATE EXTENSION IF NOT EXISTS timescaledb;" >/dev/null 2>&1 || \
  docker compose exec -T postgres psql -U "$USER" -d "$TARGET" -v ON_ERROR_STOP=1 \
    -c "CREATE EXTENSION IF NOT EXISTS timescaledb;"

docker compose exec -T postgres psql -U "$USER" -d "$TARGET" -v ON_ERROR_STOP=1 \
  -c "SELECT timescaledb_pre_restore();" >/dev/null

echo "==> restoring"
docker compose exec -T postgres \
  pg_restore -U "$USER" -d "$TARGET" --no-owner --no-privileges \
  < "$DUMP"

docker compose exec -T postgres psql -U "$USER" -d "$TARGET" -v ON_ERROR_STOP=1 \
  -c "SELECT timescaledb_post_restore();" >/dev/null

echo "==> restored into $TARGET"
