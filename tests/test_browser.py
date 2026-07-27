"""Run the browser tests as part of the normal suite.

The page is the instrument used to verify that a collection session worked, so a
broken chart is not cosmetic: it hides whether the data arrived. Keeping these
behind ``node`` being installed means they are skipped rather than silently
absent on a machine without it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

RUNNER = Path(__file__).parent / "js" / "run.mjs"


def _node() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    # fnm keeps its versions outside PATH for non-interactive shells.
    root = Path.home() / ".local/share/fnm/node-versions"
    if root.is_dir():
        for v in sorted(root.iterdir(), reverse=True):
            candidate = v / "installation" / "bin" / "node"
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return None


@pytest.mark.skipif(_node() is None, reason="node not installed")
def test_browser_code():
    node = _node()
    assert node is not None
    proc = subprocess.run(
        [node, str(RUNNER)], capture_output=True, text=True, timeout=120, check=False
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
