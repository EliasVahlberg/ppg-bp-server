#!/usr/bin/env bash
# Rebuild a DuckDB store from backup snapshots.
#
#   scripts/restore.sh <full-snapshot> [<diff-snapshot>] --out new.duckdb
#       Restore one full, plus optionally one diff taken against it, into a NEW
#       file. Row counts are checked against the snapshot manifests; deletions and
#       re-uploaded sessions since the full are applied. Never writes over an
#       existing file -- stop the server and swap the file in by hand.
#
#   scripts/restore.sh --check [--scratch DIR]
#       Restore the newest full + its newest diff into a scratch file, check it,
#       and delete it. A backup scheme whose restore is never exercised is not one.
#
# Run under `uv run` so duckdb is importable. Env as for backup.sh.

set -euo pipefail

ARGS=(--memory-limit "${PPG_PI_BACKUP_MEMORY_LIMIT:-1GB}" --threads "${PPG_PI_BACKUP_THREADS:-2}")

if [[ "${1:-}" == "--check" ]]; then
    shift
    SCRATCH="${PPG_PI_BACKUP_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/ppg-pi-server-backups}/.restore-check"
    if [[ "${1:-}" == "--scratch" ]]; then SCRATCH="$2"; shift 2; fi
    exec python -m ppg_pi_server.backup "${ARGS[@]}" restore-check --scratch "$SCRATCH" "$@"
fi

exec python -m ppg_pi_server.backup "${ARGS[@]}" restore "$@"
