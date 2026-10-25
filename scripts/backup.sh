#!/usr/bin/env bash
# Snapshot backup of an ppg-bp-server deployment: a weekly full, a daily
# differential against that full, and nothing at all on days nothing changed.
#
# Backs up three things, in increasing order of how hard they are to replace:
#
#   1. tokens.json       -- small, but losing it means re-provisioning every phone.
#   2. uploads/          -- the raw ROP bundles as the phone sent them, hardlinked
#                           against the previous snapshot (rsync --link-dest), so
#                           unchanged bundles cost no extra disk.
#   3. sessions.duckdb   -- exported to Parquet over a read-only connection, so the
#                           server keeps serving. A full is EXPORT DATABASE; a diff
#                           holds the sensor rows of sessions newer than the full
#                           plus complete copies of the small tables. Restore is
#                           always the full plus at most one diff.
#
# DuckDB runs with a capped memory_limit and thread count, and spills to disk
# beyond that. The logic lives in ppg_pi_server/backup.py; see its docstring for
# the layout, the change detection, and why diffs are keyed on session_id.
#
# IMPORTANT: a snapshot on the same physical disk protects against database
# corruption, an accidental delete, and a bad migration -- not against losing the
# disk. Off-machine replication is a separate requirement, and it is a
# precondition for ever deleting data from the phone (ppg-bp-server#4).
#
# Usage:  scripts/backup.sh [--keep-full 5] [--full-weekday 7] [--force-full] [--no-prune]
#         (run under `uv run` so duckdb is importable)
# Env:    PPG_PI_SERVER_DB_PATH, PPG_PI_SERVER_UPLOAD_DIR, PPG_PI_SERVER_TOKENS_FILE
#         PPG_PI_BACKUP_DIR (default: ~/.local/share/ppg-pi-server-backups)
#         PPG_PI_BACKUP_MEMORY_LIMIT (default 1GB), PPG_PI_BACKUP_THREADS (default 2)

set -euo pipefail

for a in "$@"; do
    if [[ "$a" == "--keep" ]]; then
        echo "--keep N was replaced by --keep-full N (number of weekly fulls kept)" >&2
        exit 2
    fi
done

exec python -m ppg_pi_server.backup \
    --memory-limit "${PPG_PI_BACKUP_MEMORY_LIMIT:-1GB}" \
    --threads "${PPG_PI_BACKUP_THREADS:-2}" \
    run "$@"
