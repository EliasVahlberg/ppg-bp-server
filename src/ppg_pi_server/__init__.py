"""the user's BP-monitoring Pi backend.

Runs the ingest API (and eventually the dashboard) for a personal Polar
Verity Sense + Omron Evolv pipeline. The phone records to local SQLite
and stages + converts raw ROP session bundles here over Tailscale.

See ``docs/design/sync_architecture.md`` in the parent repository for
the full architecture.
"""

__version__ = "0.1.0"
