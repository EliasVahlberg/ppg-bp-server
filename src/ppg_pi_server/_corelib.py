"""Expose the vendored ``polar_ble`` core library.

``ppg-pi-server`` needs the canonical ROP -> DuckDB converter and file-format
reader used by the recording side (``ppg-bp``, the core signal-processing
repo), but not the rest of that project (BLE transport, cuff protocol,
scipy/pandas-based analysis) which would drag in dependencies that don't
belong on an ingest server.

Rather than depending on the ``ppg-bp`` repo at runtime (extra install step,
version drift risk), ``converter.py`` and ``rop_format.py`` are vendored
verbatim into ``ppg_pi_server/_vendor/polar_ble/``. Both files only depend on
the stdlib, so copying them is safe and keeps this repo runnable standalone.

If the wire format changes in ``ppg-bp``, re-copy the two files from there:
``src/polar_ble/converter.py`` and ``src/polar_ble/rop_format.py``.
"""

from __future__ import annotations

from ppg_pi_server._vendor.polar_ble import converter  # noqa: F401  (re-exported)
