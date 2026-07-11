"""End-to-end ingest tests using FastAPI's TestClient.

Builds a tiny ROP session bundle in-memory, uploads it through the API, and
verifies the server stages files, converts via the shared converter, and
reconciles sample counts. No real Polar device or phone needed.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import tempfile
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Configure a temporary environment BEFORE importing the app.
_TMP = tempfile.TemporaryDirectory()
_DATA_DIR = Path(_TMP.name)
os.environ["PPG_PI_SERVER_DB_PATH"] = str(_DATA_DIR / "test.duckdb")
os.environ["PPG_PI_SERVER_UPLOAD_DIR"] = str(_DATA_DIR / "uploads")
os.environ["PPG_PI_SERVER_TOKENS_FILE"] = str(_DATA_DIR / "tokens.json")

TEST_TOKEN = "0" * 64
(_DATA_DIR / "tokens.json").write_text(
    json.dumps({TEST_TOKEN: {"phone_id": "test-phone", "created_at": "2026-05-16T00:00:00"}})
)

from ppg_pi_server.main import app  # noqa: E402  (after env setup)

# polar_ble is vendored under ppg_pi_server/_vendor/, importable directly.
from ppg_pi_server._vendor.polar_ble.rop_format import (  # noqa: E402
    RECORD_SIZE, RopHeader, SensorType, pack_acc, pack_ppg,
)

AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}
_EPOCH = 1_240_000_000_000_000_000


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


def _build_bundle(session_uuid: str, ppg_n: int = 40, acc_n: int = 12) -> dict[str, bytes]:
    su = uuid.UUID(session_uuid)
    base = 538_000_000_000_000_000
    iv_ppg = 1_000_000_000 // 176
    iv_acc = 1_000_000_000 // 52
    ppg_hdr = RopHeader(
        version=1, sensor=SensorType.PPG, record_size=RECORD_SIZE[SensorType.PPG],
        sample_rate_hz=176, session_uuid=su, rotation_start_ms=1_780_000_000_000,
        epoch_offset_ns=_EPOCH,
    )
    ppg = ppg_hdr.pack() + b"".join(
        pack_ppg(base + i * iv_ppg, 1, 100 + i, 100 - i, 100, 5) for i in range(ppg_n)
    )
    acc_hdr = RopHeader(
        version=1, sensor=SensorType.ACC, record_size=RECORD_SIZE[SensorType.ACC],
        sample_rate_hz=52, session_uuid=su, rotation_start_ms=1_780_000_000_000,
        epoch_offset_ns=_EPOCH,
    )
    acc = acc_hdr.pack() + b"".join(
        pack_acc(base + i * iv_acc, 1, i, -i, 1000) for i in range(acc_n)
    )
    manifest = json.dumps({
        "session_uuid": session_uuid,
        "started_at": 1_780_000_000.0,
        "ended_at": 1_780_000_001.0,
        "device_name": "Polar Test",
        "settings": {"ppg": 176, "acc": 52},
        "epoch_offset_ns": _EPOCH,
        "rotation_period_minutes": 15,
        "rop_files": ["ppg_000.rop", "acc_000.rop"],
    }).encode()
    segments = (json.dumps({"event": "connect", "segment_id": 1, "ts": 1_780_000_000.0}) + "\n").encode()
    return {
        "manifest.json": manifest,
        "segments.jsonl": segments,
        "ppg_000.rop": ppg,
        "acc_000.rop": acc,
    }


def _open(client: TestClient, sid: str) -> dict:
    return client.post(
        "/api/v1/sessions",
        json={"phone_session_uuid": sid, "device_name": "Polar Test"},
        headers=AUTH,
    ).json()


def _put(client: TestClient, sid: str, name: str, data: bytes, *, sha: str | None = None):
    return client.put(
        f"/api/v1/upload/{sid}/{name}",
        content=data,
        headers={**AUTH, "X-SHA256": sha if sha is not None else hashlib.sha256(data).hexdigest()},
    )


class TestAuth:
    def test_missing_bearer_rejected(self, client):
        r = client.post("/api/v1/sessions", json={"phone_session_uuid": "x" * 16})
        assert r.status_code == 401

    def test_bad_bearer_rejected(self, client):
        r = client.post("/api/v1/sessions", json={"phone_session_uuid": "x" * 16},
                        headers={"Authorization": "Bearer wrongtoken"})
        assert r.status_code == 403

    def test_health_no_auth(self, client):
        assert client.get("/health").status_code == 200


class TestSessions:
    def test_open_creates_and_is_idempotent(self, client):
        sid = str(uuid.uuid4())
        first = _open(client, sid)
        assert first["already_existed"] is False
        second = _open(client, sid)
        assert second["already_existed"] is True


class TestUpload:
    def test_upload_unknown_session_404(self, client):
        files = _build_bundle(str(uuid.uuid4()))
        sid = str(uuid.uuid4())  # never opened
        r = _put(client, sid, "ppg_000.rop", files["ppg_000.rop"])
        assert r.status_code == 404

    def test_upload_bad_filename_400(self, client):
        sid = str(uuid.uuid4())
        _open(client, sid)
        r = _put(client, sid, "evil.txt", b"nope")
        assert r.status_code == 400

    def test_upload_sha_mismatch_400(self, client):
        sid = str(uuid.uuid4())
        _open(client, sid)
        files = _build_bundle(sid)
        r = _put(client, sid, "manifest.json", files["manifest.json"], sha="0" * 64)
        assert r.status_code == 400
        assert "SHA-256 mismatch" in r.json()["detail"]

    def test_gzip_upload_ok(self, client):
        sid = str(uuid.uuid4())
        _open(client, sid)
        files = _build_bundle(sid)
        data = files["manifest.json"]
        r = client.put(
            f"/api/v1/upload/{sid}/manifest.json",
            content=gzip.compress(data),
            headers={**AUTH, "X-SHA256": hashlib.sha256(data).hexdigest(),
                     "Content-Encoding": "gzip"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["bytes"] == len(data)


class TestComplete:
    def test_full_round_trip(self, client):
        sid = str(uuid.uuid4())
        _open(client, sid)
        files = _build_bundle(sid, ppg_n=40, acc_n=12)
        for name, data in files.items():
            assert _put(client, sid, name, data).status_code == 200
        r = client.post(f"/api/v1/sessions/{sid}/complete", headers=AUTH)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "complete"
        assert body["samples_per_sensor"]["ppg"] == 40
        assert body["samples_per_sensor"]["acc"] == 12
        assert body["rop_files"] == 2
        assert body["db_session_id"] >= 1

    def test_complete_without_manifest_400(self, client):
        sid = str(uuid.uuid4())
        _open(client, sid)
        # stage only a rop file, no manifest
        files = _build_bundle(sid)
        _put(client, sid, "ppg_000.rop", files["ppg_000.rop"])
        r = client.post(f"/api/v1/sessions/{sid}/complete", headers=AUTH)
        assert r.status_code == 400

    def test_complete_unknown_404(self, client):
        r = client.post(f"/api/v1/sessions/{'a' * 36}/complete", headers=AUTH)
        assert r.status_code == 404


class TestListing:
    def test_landing_renders(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "PPG-BP Pi backend" in r.text

    def test_sessions_endpoint(self, client):
        r = client.get("/api/v1/sessions", headers=AUTH)
        assert r.status_code == 200
        assert "sessions" in r.json()


class TestCuff:
    @staticmethod
    def _reading(minute: int, sys: int = 120) -> dict:
        ts = f"2026-05-31T17:{minute:02d}:00"
        return {"id": f"{ts}|{sys}|70|72", "ts": ts, "sys": sys, "dia": 70,
                "pulse": 72, "ihb": False, "mov": False, "device": "AA:BB"}

    def test_cuff_requires_auth(self, client):
        r = client.post("/api/v1/cuff", json={"readings": []})
        assert r.status_code == 401

    def test_cuff_upload_dedupes(self, client):
        a, b, c = self._reading(1), self._reading(2), self._reading(3)
        r1 = client.post("/api/v1/cuff", json={"readings": [a, b]}, headers=AUTH).json()
        assert r1["received"] == 2 and r1["inserted"] == 2
        base = r1["total"]
        # Re-upload the rolling buffer: a,b known, only c new.
        r2 = client.post("/api/v1/cuff", json={"readings": [a, b, c]}, headers=AUTH).json()
        assert r2["received"] == 3 and r2["inserted"] == 1
        assert r2["total"] == base + 1
        # Idempotent: nothing new on repeat.
        r3 = client.post("/api/v1/cuff", json={"readings": [a, b, c]}, headers=AUTH).json()
        assert r3["inserted"] == 0 and r3["total"] == base + 1

    def test_cuff_empty_ok(self, client):
        r = client.post("/api/v1/cuff", json={"readings": []}, headers=AUTH)
        assert r.status_code == 200
        assert r.json()["received"] == 0
