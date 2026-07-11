#!/usr/bin/env python3
"""Synthesise a tiny ROP session bundle and upload it through a running
``ppg-pi-server``. Useful for smoke-testing a live server.

Usage:
    python scripts/make_test_upload.py --token <bearer> [--url http://localhost:8000]
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
import uuid
from pathlib import Path

# Import the vendored ROP writer (see src/ppg_pi_server/_vendor/polar_ble/).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ppg_pi_server._vendor.polar_ble.rop_format import (  # noqa: E402
    RECORD_SIZE, RopHeader, SensorType, pack_ppg,
)

import httpx  # noqa: E402


def build() -> tuple[str, dict[str, bytes]]:
    su = uuid.uuid4()
    epoch = 1_240_000_000_000_000_000
    base = 538_000_000_000_000_000
    iv = 1_000_000_000 // 176
    hdr = RopHeader(
        version=1, sensor=SensorType.PPG, record_size=RECORD_SIZE[SensorType.PPG],
        sample_rate_hz=176, session_uuid=su, rotation_start_ms=1_780_000_000_000,
        epoch_offset_ns=epoch,
    )
    ppg = hdr.pack() + b"".join(
        pack_ppg(base + i * iv, 1, 100 + i, 100 - i, 100, 5) for i in range(50)
    )
    manifest = json.dumps({
        "session_uuid": str(su), "started_at": 1_780_000_000.0,
        "ended_at": 1_780_000_001.0, "device_name": "synthetic",
        "settings": {"ppg": 176}, "epoch_offset_ns": epoch,
        "rotation_period_minutes": 15, "rop_files": ["ppg_000.rop"],
    }).encode()
    segments = (json.dumps({"event": "connect", "segment_id": 1, "ts": 1_780_000_000.0}) + "\n").encode()
    return str(su), {"manifest.json": manifest, "segments.jsonl": segments, "ppg_000.rop": ppg}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--token", required=True)
    args = ap.parse_args()

    su, files = build()
    h = {"Authorization": f"Bearer {args.token}"}

    r = httpx.post(f"{args.url}/api/v1/sessions",
                   json={"phone_session_uuid": su, "device_name": "synthetic"}, headers=h)
    r.raise_for_status()
    print("open:", r.json())

    for name, data in files.items():
        rr = httpx.put(
            f"{args.url}/api/v1/upload/{su}/{name}",
            content=gzip.compress(data),
            headers={**h, "X-SHA256": hashlib.sha256(data).hexdigest(),
                     "Content-Encoding": "gzip"},
        )
        rr.raise_for_status()
        print("put", name, rr.json())

    rc = httpx.post(f"{args.url}/api/v1/sessions/{su}/complete", headers=h)
    rc.raise_for_status()
    print("complete:", rc.json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
