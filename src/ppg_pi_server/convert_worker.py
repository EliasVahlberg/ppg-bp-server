"""Run one bundle conversion in a child process.

Conversion has to happen outside the server process, not merely off the event
loop. DuckDB refuses two different configurations for the same database file
within one process, and ``/healthz`` deliberately opens the store read-only
while conversion needs it read-write:

    Connection Error: Can't open a connection to same database file
    with a different configuration than existing connections

Inline conversion never hit this, but only by accident: it blocked the single
event-loop thread for the full 70-270s, so no health check could run
concurrently and no second connection ever existed. Deferring conversion to a
worker thread made it genuinely concurrent and the conflict became reachable --
it fired immediately in an end-to-end test on 2026-09-12, failing the conversion
after the client had already been told 200 and had written its .synced marker.
That is a worse failure than the re-upload loop the deferral was meant to fix.

Serialising the two with an in-process lock was rejected: conversion holds the
store for minutes, so /healthz would either block far past the monitor's timeout
or need a new "busy" state that every consumer (widget, check.sh) would have to
learn. Running conversion in a child process removes the configuration conflict
by construction and leaves every external contract untouched. Cross-process
locking still applies, but that is a pre-existing condition the ingest path
already retries against (the dashboard holds the same lock during /refresh).

Prints the conversion stats as JSON on stdout. Exit code 0 on success, 1 on
failure with the error text on stderr.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

from ._vendor.polar_ble import converter


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("bundle_dir", type=Path)
    ap.add_argument("db_path", type=Path)
    args = ap.parse_args(argv)

    try:
        stats = converter.convert_session(
            args.bundle_dir, args.db_path, append=True
        )
    except Exception as exc:  # noqa: BLE001 - reported to the parent via exit code
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    json.dump(dataclasses.asdict(stats), sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
