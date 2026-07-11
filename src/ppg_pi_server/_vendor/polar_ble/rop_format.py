"""Raw ROP (Rotation Output Period) file format v1.

Per-sensor append-only binary log. Each ROP file holds one sensor's
samples for one rotation window (default 15 minutes). The format is
designed for zero-overhead writes from the BLE callback: each record
is a fixed-size byte string produced by ``struct.pack`` and committed
with a single ``os.write``. There is no Python-level locking, no
queue, no DuckDB.

See ``docs/design/raw_rop_storage.md`` for the full design rationale.

Format layout
-------------

Each ROP file starts with a 64-byte header followed by N records of
fixed size. Records are little-endian. A partial trailing write
(daemon SIGKILL mid-record) is detectable as
``(file_size - 64) % record_size != 0`` and the reader truncates to
the last whole record.

Header fields (offsets in bytes, little-endian)::

     0   4  magic            ASCII b"ROP1"
     4   1  version          uint8, currently 1
     5   1  sensor_type      uint8, 0=PPG 1=ACC 2=GYRO 3=MAG 4=PPI
     6   2  record_size      uint16, bytes per record
     8   2  sample_rate_hz   uint16
    10   2  reserved         uint16, zero
    12  16  session_uuid     UUID4 bytes (uuid.bytes)
    28   8  rotation_start_ms int64, UTC unix milliseconds
    36   8  epoch_offset_ns  int64, wall_ns - device_ns, captured from
                             the first frame of the session (0 until then)
    44  20  reserved         twenty zero bytes

Per-sensor record formats
-------------------------

All records start with ``(ts_ns:i64, segment_id:i32)`` so the join key
to the segments table is always at offsets 0..12.

Sensor   Size  Fields after the (ts_ns, segment_id) header
PPG      32    ppg0:i32, ppg1:i32, ppg2:i32, ambient:i32, _:i32
ACC      24    x:i32, y:i32, z:i32
GYRO     24    x:f32, y:f32, z:f32
MAG      32    x:f32, y:f32, z:f32, calibration:i32, _:i32
PPI      24    hr:u16, ppi_ms:u16, err_ms:u16, flags:u16, _:i32

PPI flag bits: bit 0 blocker, bit 1 skin_contact, bit 2 sc_supported.
MAG calibration: -1 = NOT_AVAILABLE (TYPE_0 frames), 0..3 from
:class:`polar_ble.frames.mag.MagCalibration` (TYPE_1 frames).
"""

import json
import os
import struct
import uuid
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Iterator, List, Optional

# Module-level constants exposed for callers.

ROP_MAGIC: bytes = b"ROP1"
ROP_VERSION: int = 1
ROP_HEADER_SIZE: int = 64


class SensorType(IntEnum):
    """Numeric sensor identifier used in the ROP header.

    Stable across format versions. Adding a new sensor means appending
    a new value here and bumping :data:`ROP_VERSION`.
    """

    PPG = 0
    ACC = 1
    GYRO = 2
    MAG = 3
    PPI = 4


# Record sizes per sensor for v1. The reader uses these to validate
# the header's ``record_size`` field and to walk the file.
RECORD_SIZE = {
    SensorType.PPG: 32,
    SensorType.ACC: 24,
    SensorType.GYRO: 24,
    SensorType.MAG: 32,
    SensorType.PPI: 24,
}

# struct format strings, one per sensor. Used by both writer and reader.
# All little-endian, no padding (we control alignment manually with the
# explicit reserved fields).
RECORD_STRUCT = {
    SensorType.PPG: struct.Struct("<qiiiiii"),   # 8+4+4+4+4+4+4 = 32
    SensorType.ACC: struct.Struct("<qiiii"),     # 8+4+4+4+4 = 24
    SensorType.GYRO: struct.Struct("<qifff"),    # 8+4+4+4+4 = 24
    SensorType.MAG: struct.Struct("<qifffii"),   # 8+4+4+4+4+4+4 = 32
    SensorType.PPI: struct.Struct("<qiHHHHi"),   # 8+4+2+2+2+2+4 = 24
}


# Verify the structs match the documented sizes at import time. If
# this assertion fires, the format spec or the struct strings are
# inconsistent and we want to know immediately, not at runtime in the
# BLE callback.
for _st, _sz in RECORD_SIZE.items():
    assert RECORD_STRUCT[_st].size == _sz, (
        f"RECORD_STRUCT[{_st}] size {RECORD_STRUCT[_st].size} != "
        f"RECORD_SIZE[{_st}] {_sz}"
    )


# Header struct. Manual layout because we have a 16-byte UUID and
# explicit reserved padding. ``struct.pack`` gives us exactly the
# bytes documented above.
#
# Format: magic(4s) version(B) sensor(B) rec_size(H) rate(H) rsv(H)
#         uuid(16s) rotation_ms(q) epoch_off_ns(q) rsv(20s)
_HEADER_STRUCT = struct.Struct("<4sBBHHH16sqq20s")
assert _HEADER_STRUCT.size == ROP_HEADER_SIZE, (
    f"_HEADER_STRUCT size {_HEADER_STRUCT.size} != "
    f"ROP_HEADER_SIZE {ROP_HEADER_SIZE}"
)


@dataclass(frozen=True)
class RopHeader:
    """Parsed ROP file header. Immutable."""

    version: int
    sensor: SensorType
    record_size: int
    sample_rate_hz: int
    session_uuid: uuid.UUID
    rotation_start_ms: int
    epoch_offset_ns: int

    def pack(self) -> bytes:
        """Serialise to 64 bytes."""
        return _HEADER_STRUCT.pack(
            ROP_MAGIC,
            self.version,
            int(self.sensor),
            self.record_size,
            self.sample_rate_hz,
            0,
            self.session_uuid.bytes,
            self.rotation_start_ms,
            self.epoch_offset_ns,
            b"\x00" * 20,
        )

    @classmethod
    def unpack(cls, data: bytes) -> "RopHeader":
        """Parse from at least :data:`ROP_HEADER_SIZE` bytes.

        Raises:
            ValueError: magic mismatch or unknown version/sensor.
        """
        if len(data) < ROP_HEADER_SIZE:
            raise ValueError(
                f"ROP header needs {ROP_HEADER_SIZE} bytes, got {len(data)}"
            )
        (magic, version, sensor_id, rec_size, rate,
         _rsv, uuid_bytes, rot_ms, epoch_off, _pad) = _HEADER_STRUCT.unpack(
            data[:ROP_HEADER_SIZE]
        )
        if magic != ROP_MAGIC:
            raise ValueError(
                f"Not a ROP file: magic {magic!r} != {ROP_MAGIC!r}"
            )
        if version != ROP_VERSION:
            raise ValueError(
                f"Unsupported ROP version {version}, this build supports "
                f"only v{ROP_VERSION}"
            )
        try:
            sensor = SensorType(sensor_id)
        except ValueError as e:
            raise ValueError(f"Unknown sensor_type {sensor_id}") from e
        expected_size = RECORD_SIZE[sensor]
        if rec_size != expected_size:
            raise ValueError(
                f"Header record_size {rec_size} != expected "
                f"{expected_size} for {sensor.name}"
            )
        return cls(
            version=version,
            sensor=sensor,
            record_size=rec_size,
            sample_rate_hz=rate,
            session_uuid=uuid.UUID(bytes=uuid_bytes),
            rotation_start_ms=rot_ms,
            epoch_offset_ns=epoch_off,
        )


def pack_ppg(ts_ns: int, segment_id: int, ppg0: int, ppg1: int,
             ppg2: int, ambient: int) -> bytes:
    """Pack one PPG record. 32 bytes."""
    return RECORD_STRUCT[SensorType.PPG].pack(
        ts_ns, segment_id, ppg0, ppg1, ppg2, ambient, 0
    )


def pack_acc(ts_ns: int, segment_id: int, x: int, y: int, z: int) -> bytes:
    """Pack one ACC record. 24 bytes."""
    return RECORD_STRUCT[SensorType.ACC].pack(ts_ns, segment_id, x, y, z)


def pack_gyro(ts_ns: int, segment_id: int,
              x: float, y: float, z: float) -> bytes:
    """Pack one GYRO record. 24 bytes."""
    return RECORD_STRUCT[SensorType.GYRO].pack(ts_ns, segment_id, x, y, z)


def pack_mag(ts_ns: int, segment_id: int, x: float, y: float, z: float,
             calibration: int) -> bytes:
    """Pack one MAG record. 32 bytes.

    ``calibration`` is the int value of
    :class:`polar_ble.frames.mag.MagCalibration` (-1 for
    NOT_AVAILABLE). Pass through whatever the parser gave us.
    """
    return RECORD_STRUCT[SensorType.MAG].pack(
        ts_ns, segment_id, x, y, z, calibration, 0
    )


def pack_ppi(ts_ns: int, segment_id: int, hr: int, ppi_ms: int,
             err_ms: int, blocker: bool, skin_contact: bool,
             sc_supported: bool) -> bytes:
    """Pack one PPI record. 24 bytes."""
    flags = (
        (0x01 if blocker else 0)
        | (0x02 if skin_contact else 0)
        | (0x04 if sc_supported else 0)
    )
    return RECORD_STRUCT[SensorType.PPI].pack(
        ts_ns, segment_id, hr, ppi_ms, err_ms, flags, 0
    )


def interpolate_sample_timestamps(frame_ts_ns: int, n_samples: int,
                                  sample_rate_hz: int) -> List[int]:
    """Compute per-sample timestamps for a frame.

    The PMD spec says the frame's ``timestamp_ns`` is the timestamp of
    the **last** sample in the frame. Earlier samples are at preceding
    intervals of ``1 / sample_rate_hz`` seconds.

    Returns a list of ``n_samples`` ints in chronological order. The
    last entry equals ``frame_ts_ns``.
    """
    if n_samples <= 0:
        return []
    if sample_rate_hz <= 0:
        # Defensive: if rate is unknown, all samples get the frame
        # timestamp. Worst case we lose intra-frame ordering precision.
        return [frame_ts_ns] * n_samples
    interval_ns = 1_000_000_000 // sample_rate_hz
    return [
        frame_ts_ns - (n_samples - 1 - i) * interval_ns
        for i in range(n_samples)
    ]


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


class RopWriter:
    """Append-only writer for one sensor's ROP file.

    The constructor opens a file descriptor with O_APPEND | O_CREAT
    and writes the 64-byte header. Subsequent ``write_record`` /
    ``write_records`` calls are single ``os.write`` syscalls — no
    Python-level locks, no buffer that can grow unbounded.

    Owned by the streaming daemon. One instance per sensor per
    rotation window. ``close()`` is idempotent and safe to call from
    a finally block.
    """

    def __init__(self, path: Path, header: RopHeader) -> None:
        if header.record_size != RECORD_SIZE[header.sensor]:
            raise ValueError(
                f"Header record_size {header.record_size} doesn't match "
                f"{header.sensor.name} expected {RECORD_SIZE[header.sensor]}"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        # O_APPEND so concurrent writes from the same process append
        # atomically. Mode 0o600 because ROP files may contain
        # health-related data; not world-readable by default.
        self._fd: Optional[int] = os.open(
            str(path),
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        self.path = path
        self.header = header
        # Bytes written counter, incremented by the actual return
        # value of os.write. The streaming daemon reads this for the
        # heartbeat without having to stat() anything.
        self.bytes_written: int = 0
        # Records written counter. Useful for the heartbeat too and
        # cheaper than dividing bytes_written by record_size each tick.
        self.records_written: int = 0
        # Write the header. If this fails, surface immediately rather
        # than producing a headerless ROP file.
        n = os.write(self._fd, header.pack())
        if n != ROP_HEADER_SIZE:
            self.close()
            raise OSError(
                f"short header write: wrote {n}/{ROP_HEADER_SIZE} bytes"
            )
        self.bytes_written += n

    def write_record(self, record: bytes) -> None:
        """Append one packed record. Must be exactly ``record_size`` bytes."""
        if self._fd is None:
            raise OSError("RopWriter is closed")
        if len(record) != self.header.record_size:
            raise ValueError(
                f"record length {len(record)} != "
                f"header.record_size {self.header.record_size}"
            )
        n = os.write(self._fd, record)
        if n != len(record):
            raise OSError(f"short record write: {n}/{len(record)}")
        self.bytes_written += n
        self.records_written += 1

    def write_records(self, records: bytes, count: int) -> None:
        """Append ``count`` records concatenated as one byte string.

        Single ``os.write`` for the whole batch — preferred hot path
        for frames carrying multiple samples (PPG, ACC, GYRO, MAG).
        """
        if self._fd is None:
            raise OSError("RopWriter is closed")
        expected = count * self.header.record_size
        if len(records) != expected:
            raise ValueError(
                f"records bytes {len(records)} != "
                f"count*record_size {expected}"
            )
        n = os.write(self._fd, records)
        if n != len(records):
            raise OSError(f"short batch write: {n}/{len(records)}")
        self.bytes_written += n
        self.records_written += count

    def fileno(self) -> int:
        """Return the file descriptor (for fdatasync etc.)."""
        if self._fd is None:
            raise OSError("RopWriter is closed")
        return self._fd

    def close(self) -> None:
        """Close the file descriptor. Idempotent."""
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    def __enter__(self) -> "RopWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


class RopReader:
    """Iterator over records in one ROP file.

    Reads the header, validates magic+version, then yields raw record
    bytes one at a time. Use :func:`unpack_record` to turn each into
    sensor-specific named tuple. A partial trailing record (file size
    not a multiple of record_size after the header) is silently
    truncated to the last whole record.

    Safe to use on a file that's still being appended to: we only
    read up to the size at open time. To re-read fresh data, open a
    new reader.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = open(path, "rb")
        try:
            head = self._fh.read(ROP_HEADER_SIZE)
            if len(head) < ROP_HEADER_SIZE:
                raise ValueError(
                    f"{path} too short to contain a ROP header"
                )
            self.header = RopHeader.unpack(head)
        except Exception:
            self._fh.close()
            raise

    def __iter__(self) -> Iterator[bytes]:
        rec_size = self.header.record_size
        while True:
            chunk = self._fh.read(rec_size)
            if len(chunk) < rec_size:
                # Partial trailing write or EOF. Either way, stop.
                return
            yield chunk

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> "RopReader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Record unpacking helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PpgRecord:
    ts_ns: int
    segment_id: int
    ppg0: int
    ppg1: int
    ppg2: int
    ambient: int


@dataclass(frozen=True)
class AccRecord:
    ts_ns: int
    segment_id: int
    x: int
    y: int
    z: int


@dataclass(frozen=True)
class GyroRecord:
    ts_ns: int
    segment_id: int
    x: float
    y: float
    z: float


@dataclass(frozen=True)
class MagRecord:
    ts_ns: int
    segment_id: int
    x: float
    y: float
    z: float
    calibration: int


@dataclass(frozen=True)
class PpiRecord:
    ts_ns: int
    segment_id: int
    hr: int
    ppi_ms: int
    err_ms: int
    blocker: bool
    skin_contact: bool
    sc_supported: bool


def unpack_ppg(record: bytes) -> PpgRecord:
    ts, seg, p0, p1, p2, amb, _ = RECORD_STRUCT[SensorType.PPG].unpack(record)
    return PpgRecord(ts, seg, p0, p1, p2, amb)


def unpack_acc(record: bytes) -> AccRecord:
    ts, seg, x, y, z = RECORD_STRUCT[SensorType.ACC].unpack(record)
    return AccRecord(ts, seg, x, y, z)


def unpack_gyro(record: bytes) -> GyroRecord:
    ts, seg, x, y, z = RECORD_STRUCT[SensorType.GYRO].unpack(record)
    return GyroRecord(ts, seg, x, y, z)


def unpack_mag(record: bytes) -> MagRecord:
    ts, seg, x, y, z, cal, _ = RECORD_STRUCT[SensorType.MAG].unpack(record)
    return MagRecord(ts, seg, x, y, z, cal)


def unpack_ppi(record: bytes) -> PpiRecord:
    ts, seg, hr, ppi_ms, err_ms, flags, _ = (
        RECORD_STRUCT[SensorType.PPI].unpack(record)
    )
    return PpiRecord(
        ts, seg, hr, ppi_ms, err_ms,
        bool(flags & 0x01),
        bool(flags & 0x02),
        bool(flags & 0x04),
    )


UNPACKER = {
    SensorType.PPG: unpack_ppg,
    SensorType.ACC: unpack_acc,
    SensorType.GYRO: unpack_gyro,
    SensorType.MAG: unpack_mag,
    SensorType.PPI: unpack_ppi,
}


# ---------------------------------------------------------------------------
# Format spec artefact
# ---------------------------------------------------------------------------


def write_format_spec(path: Path) -> None:
    """Write a self-describing JSON spec for the current format version.

    Dropped into each session directory so an analyst with only the
    raw files (no codebase) can decode them. Mirrors what's documented
    in ``docs/design/raw_rop_storage.md``.
    """
    spec = {
        "version": ROP_VERSION,
        "magic": ROP_MAGIC.decode("ascii"),
        "header_size": ROP_HEADER_SIZE,
        "header_layout": [
            {"offset": 0, "size": 4, "type": "ascii", "name": "magic"},
            {"offset": 4, "size": 1, "type": "u8", "name": "version"},
            {"offset": 5, "size": 1, "type": "u8", "name": "sensor_type"},
            {"offset": 6, "size": 2, "type": "u16le", "name": "record_size"},
            {"offset": 8, "size": 2, "type": "u16le", "name": "sample_rate_hz"},
            {"offset": 10, "size": 2, "type": "u16le", "name": "_reserved0"},
            {"offset": 12, "size": 16, "type": "uuid", "name": "session_uuid"},
            {"offset": 28, "size": 8, "type": "i64le",
             "name": "rotation_start_ms"},
            {"offset": 36, "size": 8, "type": "i64le",
             "name": "epoch_offset_ns"},
            {"offset": 44, "size": 20, "type": "bytes", "name": "_reserved1"},
        ],
        "sensors": {
            "PPG": {
                "id": int(SensorType.PPG),
                "record_size": RECORD_SIZE[SensorType.PPG],
                "fields": [
                    {"offset": 0, "size": 8, "type": "i64le", "name": "ts_ns"},
                    {"offset": 8, "size": 4, "type": "i32le",
                     "name": "segment_id"},
                    {"offset": 12, "size": 4, "type": "i32le", "name": "ppg0"},
                    {"offset": 16, "size": 4, "type": "i32le", "name": "ppg1"},
                    {"offset": 20, "size": 4, "type": "i32le", "name": "ppg2"},
                    {"offset": 24, "size": 4, "type": "i32le",
                     "name": "ambient"},
                    {"offset": 28, "size": 4, "type": "i32le",
                     "name": "_reserved"},
                ],
            },
            "ACC": {
                "id": int(SensorType.ACC),
                "record_size": RECORD_SIZE[SensorType.ACC],
                "fields": [
                    {"offset": 0, "size": 8, "type": "i64le", "name": "ts_ns"},
                    {"offset": 8, "size": 4, "type": "i32le",
                     "name": "segment_id"},
                    {"offset": 12, "size": 4, "type": "i32le", "name": "x"},
                    {"offset": 16, "size": 4, "type": "i32le", "name": "y"},
                    {"offset": 20, "size": 4, "type": "i32le", "name": "z"},
                ],
            },
            "GYRO": {
                "id": int(SensorType.GYRO),
                "record_size": RECORD_SIZE[SensorType.GYRO],
                "fields": [
                    {"offset": 0, "size": 8, "type": "i64le", "name": "ts_ns"},
                    {"offset": 8, "size": 4, "type": "i32le",
                     "name": "segment_id"},
                    {"offset": 12, "size": 4, "type": "f32le", "name": "x"},
                    {"offset": 16, "size": 4, "type": "f32le", "name": "y"},
                    {"offset": 20, "size": 4, "type": "f32le", "name": "z"},
                ],
            },
            "MAG": {
                "id": int(SensorType.MAG),
                "record_size": RECORD_SIZE[SensorType.MAG],
                "fields": [
                    {"offset": 0, "size": 8, "type": "i64le", "name": "ts_ns"},
                    {"offset": 8, "size": 4, "type": "i32le",
                     "name": "segment_id"},
                    {"offset": 12, "size": 4, "type": "f32le", "name": "x"},
                    {"offset": 16, "size": 4, "type": "f32le", "name": "y"},
                    {"offset": 20, "size": 4, "type": "f32le", "name": "z"},
                    {"offset": 24, "size": 4, "type": "i32le",
                     "name": "calibration"},
                    {"offset": 28, "size": 4, "type": "i32le",
                     "name": "_reserved"},
                ],
                "calibration_values": {
                    "-1": "NOT_AVAILABLE",
                    "0": "UNKNOWN",
                    "1": "POOR",
                    "2": "OK",
                    "3": "GOOD",
                },
            },
            "PPI": {
                "id": int(SensorType.PPI),
                "record_size": RECORD_SIZE[SensorType.PPI],
                "fields": [
                    {"offset": 0, "size": 8, "type": "i64le", "name": "ts_ns"},
                    {"offset": 8, "size": 4, "type": "i32le",
                     "name": "segment_id"},
                    {"offset": 12, "size": 2, "type": "u16le", "name": "hr"},
                    {"offset": 14, "size": 2, "type": "u16le",
                     "name": "ppi_ms"},
                    {"offset": 16, "size": 2, "type": "u16le",
                     "name": "err_ms"},
                    {"offset": 18, "size": 2, "type": "u16le",
                     "name": "flags"},
                    {"offset": 20, "size": 4, "type": "i32le",
                     "name": "_reserved"},
                ],
                "flag_bits": {
                    "0": "blocker",
                    "1": "skin_contact",
                    "2": "skin_contact_supported",
                },
            },
        },
        "endianness": "little",
        "partial_write_recovery": (
            "If (file_size - header_size) % record_size != 0, the trailing "
            "incomplete record was lost to a daemon crash. Truncate the "
            "file (or the in-memory representation) to the last whole "
            "record before processing."
        ),
        "per_sample_timestamps": (
            "The frame timestamp_ns from the PMD layer is the timestamp of "
            "the LAST sample in the frame. Earlier samples in the same "
            "frame are at preceding intervals of (1 / sample_rate_hz) "
            "seconds. The writer interpolates per-sample timestamps at "
            "write time so each ROP record carries an independent ts_ns."
        ),
    }
    path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
