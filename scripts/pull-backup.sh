#!/usr/bin/env bash
# Replicate a server's backup snapshots to a second machine.
#
# This is the other half of backup.sh. That script says so in its own header: a
# snapshot on the same physical disk protects against corruption, an accidental
# delete and a bad migration, but not against losing the disk. Until a copy exists
# somewhere else, the deployment is one hardware failure away from losing every
# recording, and off-machine replication is a precondition for ever deleting data
# from a phone (ppg-bp-server#4).
#
# What it does:
#
#   1. Pulls the newest remote snapshot (or --all of them) over ssh.
#   2. Deduplicates against snapshots already held, so a nightly pull costs roughly
#      its daily delta rather than a full copy.
#   3. Verifies the result by loading the Parquet export and counting rows, not by
#      trusting that rsync exited 0.
#
# On deduplication, because getting it wrong is silent and expensive. Two things
# are needed and neither is sufficient alone:
#
#   --link-dest paths are resolved relative to the *destination of the transfer*.
#   Copying the directory itself (src/SNAP dest/) makes lookups <link-dest>/SNAP/...
#   so nothing ever matches and you get a full copy nightly with no error. Copying
#   its contents (src/SNAP/ dest/SNAP/) makes them <link-dest>/uploads/... which is
#   correct. This script always uses the second form.
#
#   --link-dest still matches on size+mtime, so a content-based dedupe pass that
#   has touched local mtimes will defeat it on later pulls. A hardlink(1) pass
#   after the transfer is mtime-independent and reclaims the space regardless.
#
# Hardlinking immutable snapshots is safe: rsync writes a temp file and renames,
# which breaks the link rather than editing shared content in place. Do not edit
# files inside a snapshot by hand.
#
# Usage:
#   scripts/pull-backup.sh --from oldpc
#   scripts/pull-backup.sh --from oldpc --dest ~/backups/ppg-pi-server
#   scripts/pull-backup.sh --from oldpc --all
#   scripts/pull-backup.sh --from oldpc --verify-only
#
# Env: PPG_PI_BACKUP_DIR    remote snapshot dir (default ~/.local/share/ppg-pi-server-backups)

set -euo pipefail

REMOTE=""
DEST="$HOME/backups/ppg-pi-server"
REMOTE_DIR="${PPG_PI_BACKUP_DIR:-.local/share/ppg-pi-server-backups}"
ALL=0
VERIFY_ONLY=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --from) REMOTE="$2"; shift 2 ;;
        --dest) DEST="$2"; shift 2 ;;
        --remote-dir) REMOTE_DIR="$2"; shift 2 ;;
        --all) ALL=1; shift ;;
        --verify-only) VERIFY_ONLY=1; shift ;;
        -h|--help) sed -n '2,36p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

say()  { printf '==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

command -v rsync >/dev/null || die "rsync not installed"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Pick an interpreter that can actually import duckdb. The system python usually
# cannot -- this project's deps live in a uv-managed venv -- and silently falling
# back to "present but unverified" would defeat the point of verifying at all.
pick_python() {
    if python3 -c "import duckdb" 2>/dev/null; then
        echo "python3"; return
    fi
    if [ -x "$REPO_ROOT/.venv/bin/python3" ] \
       && "$REPO_ROOT/.venv/bin/python3" -c "import duckdb" 2>/dev/null; then
        echo "$REPO_ROOT/.venv/bin/python3"; return
    fi
    if command -v uv >/dev/null 2>&1 \
       && (cd "$REPO_ROOT" && uv run python -c "import duckdb" 2>/dev/null); then
        echo "UV"; return
    fi
    echo ""
}

PY_BIN="$(pick_python)"

verify() {
    # An unverified backup is not a backup. Load the export and count rows.
    local snap="$1"
    local exp="$snap/duckdb-export"
    [ -d "$exp" ] || { note "no duckdb-export/ in ${snap##*/} -- cannot verify"; return 1; }
    if [ -z "$PY_BIN" ]; then
        note "no python with duckdb available; $(find "$exp" -name '*.parquet' | wc -l) parquet file(s) present, UNVERIFIED"
        return 1
    fi
    local script
    script=$(cat <<'PY'
import sys
from pathlib import Path
import duckdb
exp = Path(sys.argv[1])
con = duckdb.connect(":memory:")
total = 0
for p in sorted(exp.glob("*.parquet")):
    n = con.execute(f"SELECT count(*) FROM read_parquet('{p}')").fetchone()[0]
    total += n
    if p.stem in ("cuff_readings", "sessions", "ppg"):
        print(f"    {p.stem:<16} {n:>12,} rows")
print(f"    total {total:,} rows readable")
con.close()
PY
)
    if [ "$PY_BIN" = "UV" ]; then
        (cd "$REPO_ROOT" && printf '%s' "$script" | uv run python - "$exp")
    else
        printf '%s' "$script" | "$PY_BIN" - "$exp"
    fi
}

if [ "$VERIFY_ONLY" -eq 1 ]; then
    [ -d "$DEST" ] || die "no local snapshots at $DEST"
    for snap in "$DEST"/*/; do
        say "verifying ${snap%/}"
        verify "${snap%/}" || true
    done
    exit 0
fi

[ -n "$REMOTE" ] || die "need --from <ssh-host>"
mkdir -p "$DEST"

say "listing snapshots on $REMOTE"
mapfile -t REMOTE_SNAPS < <(ssh "$REMOTE" "ls -1 '$REMOTE_DIR' 2>/dev/null" | tr -d '\r' | sed '/^$/d')
[ "${#REMOTE_SNAPS[@]}" -gt 0 ] || die "no snapshots found in $REMOTE:$REMOTE_DIR"
note "${#REMOTE_SNAPS[@]} remote snapshot(s), newest ${REMOTE_SNAPS[-1]}"

if [ "$ALL" -eq 1 ]; then
    WANTED=("${REMOTE_SNAPS[@]}")
else
    WANTED=("${REMOTE_SNAPS[-1]}")
fi

for SNAP in "${WANTED[@]}"; do
    if [ -d "$DEST/$SNAP" ]; then
        note "$SNAP already present, skipping"
        continue
    fi
    # Link against the newest snapshot already here, which is the most likely to
    # share content with the one arriving.
    LINK_ARG=()
    PREV="$(ls -1 "$DEST" 2>/dev/null | tail -1 || true)"
    if [ -n "$PREV" ]; then
        LINK_ARG=(--link-dest="$DEST/$PREV")
        note "hardlinking unchanged files against $PREV"
    fi

    say "pulling $SNAP"
    mkdir -p "$DEST/$SNAP"
    # Trailing slashes on both sides: copy the contents, so --link-dest lookups
    # land on <link-dest>/uploads/... rather than <link-dest>/$SNAP/uploads/...
    rsync -a --partial "${LINK_ARG[@]}" \
        "$REMOTE:$REMOTE_DIR/$SNAP/" "$DEST/$SNAP/" \
        || die "rsync failed for $SNAP (partial data left in $DEST/$SNAP)"

    say "verifying $SNAP"
    verify "$DEST/$SNAP" || die "$SNAP arrived but could not be verified"

    # --link-dest alone is not dependable here. It matches on size+mtime, and any
    # prior content-based dedupe pass (below) can leave local mtimes that no longer
    # match the remote, after which every subsequent pull silently copies in full.
    # A content-based pass afterwards is independent of mtime and reclaims the same
    # space either way. Safe on snapshots because nothing edits them in place and
    # rsync writes via temp-file rename.
    if command -v hardlink >/dev/null 2>&1; then
        say "deduplicating against existing snapshots"
        before=$(du -s "$DEST" | cut -f1)
        hardlink --ignore-time "$DEST" >/dev/null 2>&1 || note "dedupe pass failed (harmless)"
        after=$(du -s "$DEST" | cut -f1)
        note "reclaimed $(( (before - after) / 1024 )) MB"
    else
        note "hardlink(1) not installed -- snapshots will not share disk"
    fi
done

say "done"
note "local snapshots at $DEST:"
du -sh "$DEST"/*/ 2>/dev/null | sed 's/^/      /'
note "real disk use: $(du -sh "$DEST" | cut -f1) (hardlinks shared between snapshots)"
