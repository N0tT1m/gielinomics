#!/usr/bin/env bash
#
# Dumps the Gielinomics database and prunes old dumps.
#
# The dataset is the moat: the upstream APIs publish only recent windows, so anything lost
# here cannot be re-fetched at 5-minute resolution. It is re-derivable only by waiting
# however many months of accumulation the dump held.
#
# Usage:  ops/backup.sh [destination-directory]
#
set -euo pipefail

cd "$(dirname "$0")/.."

DEST="${1:-${GIELINOMICS_BACKUP_DIR:-/mnt/ai/backups/gielinomics}}"
KEEP_DAYS="${GIELINOMICS_BACKUP_KEEP_DAYS:-30}"
DB="${POSTGRES_DB:-gielinomics}"
USER="${POSTGRES_USER:-gielinomics}"

stamp=$(date -u +%Y%m%dT%H%M%SZ)
target="$DEST/gielinomics-$stamp.dump"

mkdir -p "$DEST"

echo "==> dumping $DB to $target"

# Custom format (-Fc): compressed, and restorable selectively with pg_restore, which a plain
# SQL dump is not. Written to a .partial and renamed only on success, so a dump interrupted
# halfway can never be mistaken for a complete one by the restore or the pruning below.
docker compose exec -T postgres \
  pg_dump -U "$USER" -d "$DB" -Fc --no-owner --no-privileges \
  > "$target.partial"

mv "$target.partial" "$target"

# A checksum recorded at dump time is the only way to tell a bit-rotted archive from a good
# one before you are relying on it.
if command -v sha256sum >/dev/null 2>&1; then
  (cd "$DEST" && sha256sum "$(basename "$target")" > "$(basename "$target").sha256")
else
  (cd "$DEST" && shasum -a 256 "$(basename "$target")" > "$(basename "$target").sha256")
fi

size=$(du -h "$target" | cut -f1)
echo "==> wrote $target ($size)"

# Prune. Only complete dumps are ever named .dump, so this cannot delete a good backup in
# favour of a truncated one.
if [ "$KEEP_DAYS" -gt 0 ]; then
  echo "==> pruning dumps older than $KEEP_DAYS days"
  find "$DEST" -maxdepth 1 -name 'gielinomics-*.dump' -mtime "+$KEEP_DAYS" -print -delete
  find "$DEST" -maxdepth 1 -name 'gielinomics-*.dump.sha256' -mtime "+$KEEP_DAYS" -delete
  find "$DEST" -maxdepth 1 -name 'gielinomics-*.dump.partial' -mtime +1 -print -delete
fi

# Off-box copy. A backup on the same disk as the database survives exactly the failures that
# were never going to lose the data anyway.
if [ -n "${GIELINOMICS_BACKUP_REMOTE:-}" ]; then
  echo "==> copying to $GIELINOMICS_BACKUP_REMOTE"
  rsync -av --partial "$target" "$target.sha256" "$GIELINOMICS_BACKUP_REMOTE/"
else
  echo "==> GIELINOMICS_BACKUP_REMOTE is unset; this dump exists on one disk only" >&2
fi
