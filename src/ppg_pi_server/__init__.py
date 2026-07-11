"""Self-hosted BP-monitoring ingest server (ppg-pi-server).

Runs the ingest API (and eventually the dashboard) for a personal Polar
Verity Sense + Omron Evolv pipeline. The phone records to local ROP files
and stages + converts raw ROP session bundles here over your own network
(Tailscale recommended; see README.md for the auth/networking model).
"""

__version__ = "0.1.0"
