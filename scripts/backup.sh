#!/usr/bin/env bash
# Snapshot backup of an ppg-bp-server deployment.
#
# Backs up three things, in increasing order of how hard they are to replace:
#
#   1. tokens.json       -- small, but losing it means re-provisioning every phone.
#   2. uploads/          -- the raw ROP bundles as the phone sent them. These are
#                           the real source of truth: the DuckDB store is derived
#                           from them and can be rebuilt.
#   3. sessions.duckdb   -- exported to Parquet rather than copied. A file copy of
#                           a database the server may be mid-write on can capture a
#                           torn state; a read-only EXPORT DATABASE is consistent
#                           and needs no downtime.
#
# Uploads are hardlinked against the previous snapshot (rsync --link-dest), so
# unchanged bundles cost no extra disk. Old snapshots are pruned by count.
#
# IMPORTANT: a snapshot on the same physical disk protects against database
# corruption, an accidental delete, and a bad migration -- not against losing the
# disk. Off-machine replication is a separate, harder requirement, and it is a
# precondition for ever deleting data from the phone (ppg-bp-server#4).
#
# Usage:  scripts/backup.sh [--keep N]
# Env:    PPG_PI_SERVER_DB_PATH, PPG_PI_SERVER_UPLOAD_DIR, PPG_PI_SERVER_TOKENS_FILE
#         PPG_PI_BACKUP_DIR (default: ~/.local/share/ppg-pi-server-backups)

set -euo pipefail

KEEP=14
while [[ $# -gt 0 ]]; do
    case "$1" in
        --keep) KEEP="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
DB_PATH="${PPG_PI_SERVER_DB_PATH:-$DATA_HOME/ppg-pi-server/sessions.duckdb}"
UPLOAD_DIR="${PPG_PI_SERVER_UPLOAD_DIR:-$DATA_HOME/ppg-pi-server/uploads}"
TOKENS_FILE="${PPG_PI_SERVER_TOKENS_FILE:-$DATA_HOME/ppg-pi-server/tokens.json}"
BACKUP_DIR="${PPG_PI_BACKUP_DIR:-$DATA_HOME/ppg-pi-server-backups}"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="$BACKUP_DIR/$STAMP"
PREV="$(ls -1d "$BACKUP_DIR"/*/ 2>/dev/null | tail -1 || true)"

mkdir -p "$DEST"

# --- 1. tokens ------------------------------------------------------------
if [[ -f "$TOKENS_FILE" ]]; then
    install -m 600 "$TOKENS_FILE" "$DEST/tokens.json"
fi

# --- 2. raw uploads, hardlinked against the previous snapshot -------------
if [[ -d "$UPLOAD_DIR" ]]; then
    if [[ -n "$PREV" && -d "${PREV}uploads" ]]; then
        rsync -a --link-dest="${PREV}uploads" "$UPLOAD_DIR/" "$DEST/uploads/"
    else
        rsync -a "$UPLOAD_DIR/" "$DEST/uploads/"
    fi
fi

# --- 3. database, exported read-only so the server keeps serving ----------
if [[ -f "$DB_PATH" ]]; then
    python3 - "$DB_PATH" "$DEST/duckdb-export" <<'PY'
import sys

import duckdb

db, out = sys.argv[1], sys.argv[2]
con = duckdb.connect(db, read_only=True)
try:
    con.execute(f"EXPORT DATABASE '{out}' (FORMAT PARQUET)")
finally:
    con.close()
PY
fi

# --- prune ----------------------------------------------------------------
mapfile -t SNAPS < <(ls -1d "$BACKUP_DIR"/*/ 2>/dev/null | sort)
if (( ${#SNAPS[@]} > KEEP )); then
    for old in "${SNAPS[@]:0:$(( ${#SNAPS[@]} - KEEP ))}"; do
        rm -rf -- "$old"
    done
fi

echo "snapshot: $DEST"
du -sh "$DEST" 2>/dev/null | awk '{print "size (excl. hardlinks): " $1}'
echo "snapshots kept: $(ls -1d "$BACKUP_DIR"/*/ 2>/dev/null | wc -l) (limit $KEEP)"
