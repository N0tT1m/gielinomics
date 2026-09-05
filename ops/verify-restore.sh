#!/usr/bin/env bash
#
# Restores the newest dump into a throwaway database and asserts it holds real data.
#
# An untested backup of the one irreplaceable asset is not a backup. This is the test, and
# it is written to be run from cron so that it is not a thing anybody has to remember.
#
# Usage:  ops/verify-restore.sh [dump-file]
#
set -euo pipefail

cd "$(dirname "$0")/.."

DUMP="${1:-latest}"
SCRATCH="gielinomics_verify_$$"
USER="${POSTGRES_USER:-gielinomics}"

cleanup() {
  docker compose exec -T postgres psql -U "$USER" -d postgres \
    -c "DROP DATABASE IF EXISTS \"$SCRATCH\" WITH (FORCE);" >/dev/null 2>&1 || true
}
trap cleanup EXIT

ops/restore.sh "$DUMP" "$SCRATCH"

query() {
  docker compose exec -T postgres psql -U "$USER" -d "$SCRATCH" -tAc "$1" | tr -d '[:space:]'
}

echo "==> checking the restored database"

failures=0
check() {
  local label="$1" actual="$2" expectation="$3"
  if [ "$actual" = "t" ]; then
    printf '    ok   %-28s %s\n' "$label" "$expectation"
  else
    printf '    FAIL %-28s %s\n' "$label" "$expectation"
    failures=$((failures + 1))
  fi
}

# Row counts, not just "the tables exist": a restore that produced empty hypertables is the
# specific failure mode this script exists to catch.
items=$(query "SELECT count(*) FROM items;")
series=$(query "SELECT count(*) FROM price_series;")
latest=$(query "SELECT count(*) FROM price_latest;")
runs=$(query "SELECT count(*) FROM ingest_runs;")
newest=$(query "SELECT coalesce(max(bucket_ts)::text, 'none') FROM price_series;")
hypertables=$(query "SELECT count(*) FROM timescaledb_information.hypertables;")

echo "    items=$items price_series=$series price_latest=$latest ingest_runs=$runs"
echo "    hypertables=$hypertables newest bucket=$newest"

check "items populated"        "$([ "$items" -gt 0 ] && echo t)"        "> 0 rows"
check "price_series populated" "$([ "$series" -gt 0 ] && echo t)"       "> 0 rows"
check "ingest_runs populated"  "$([ "$runs" -gt 0 ] && echo t)"         "> 0 rows"
check "hypertables restored"   "$([ "$hypertables" -gt 0 ] && echo t)"  "> 0 hypertables"
check "recent data"            "$(query "SELECT max(bucket_ts) > now() - interval '2 days' FROM price_series;")" \
      "newest bucket within 2 days of the dump"

if [ "$failures" -gt 0 ]; then
  echo "==> RESTORE VERIFICATION FAILED ($failures checks)" >&2
  exit 1
fi

echo "==> restore verified"
