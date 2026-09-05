# Operations

The dataset is the moat. The upstream APIs publish recent windows only and `/timeseries`
at a year's lookback returns daily bars, so 5-minute history lost here is not re-fetchable
at any price — only re-accumulable, at one day per day.

Everything in this directory exists to make that statement survivable.

| Script | What it does |
|---|---|
| `backup.sh` | `pg_dump -Fc` to the backup directory, checksummed, pruned, optionally copied off-box. |
| `restore.sh` | Restores a dump. Defaults to a scratch database, not the live one. |
| `verify-restore.sh` | Restores the newest dump into a throwaway database and asserts it holds real data. |

## Setup

```bash
export GIELINOMICS_BACKUP_DIR=/mnt/ai/backups/gielinomics
export GIELINOMICS_BACKUP_REMOTE=user@otherbox:/backups/gielinomics   # optional but see below
ops/backup.sh
```

Without `GIELINOMICS_BACKUP_REMOTE` the script warns and continues: the dump then exists on
the same disk as the database it protects, which covers `DROP TABLE` and covers nothing else.
The remote can be any rsync target — another machine, a NAS, an rclone mount.

## Cron

```cron
# Nightly dump at 03:15.
15 3 * * *  cd /srv/gielinomics && GIELINOMICS_BACKUP_DIR=/mnt/ai/backups/gielinomics ops/backup.sh >> /var/log/gielinomics-backup.log 2>&1

# Weekly restore rehearsal, Sunday 04:30. Exits non-zero when the newest dump does not
# restore into a working database, which is the only signal that the backups are real.
30 4 * * 0  cd /srv/gielinomics && ops/verify-restore.sh >> /var/log/gielinomics-restore-check.log 2>&1
```

`verify-restore.sh` restores into `gielinomics_verify_<pid>` and drops it on exit, including
on failure. It never touches the live database.

## Restoring for real

```bash
ops/restore.sh latest                    # into gielinomics_restore, alongside the live db
ops/restore.sh latest gielinomics        # over the live database; types out a confirmation
```

Both paths verify the dump's checksum first, and both wrap the load in
`timescaledb_pre_restore()` / `timescaledb_post_restore()`. Skipping those produces a restore
that reports success and leaves the hypertable catalog empty — data present on disk, invisible
to every query. That is why `verify-restore.sh` asserts on row counts rather than exit codes.

## When `pg_dump` stops being enough

`pg_dump` degrades badly as hypertables grow: it is a logical dump, so its cost scales with
row count and it holds a transaction open for the duration. It is fine while `price_series`
is measured in gigabytes. Once it is measured in tens of gigabytes, move to physical backups —
`pg_basebackup` plus WAL archiving — and keep this script as the portable-format secondary.

The number to watch:

```bash
docker compose exec -T postgres psql -U gielinomics -d gielinomics \
  -c "SELECT pg_size_pretty(hypertable_size('price_series'));"
```
