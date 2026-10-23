"""Runner-aware subprocess deadlines for the OS-specific test lanes.

The arm64 Windows CI lane (a 4-vCPU hosted runner with slow cold
PowerShell/Python starts) cannot meet the wall-clock deadlines the self-hosted
x64 box meets in single-digit seconds. Every hard-coded ``timeout=`` deadline
in a test that spawns a real process tree is wrapped in
``scaled_timeout``; the arm64 row of .github/workflows/tests-os.yml sets
``HERMES_TEST_TIMEOUT_SCALE`` (e.g. ``3``) and each deadline is multiplied by
it. Every other lane leaves the variable unset and runs the original
deadlines — no assertion is weakened anywhere else.
"""

from __future__ import annotations

import os

_SCALE_ENV = "HERMES_TEST_TIMEOUT_SCALE"


def _scale() -> float:
    raw = os.environ.get(_SCALE_ENV, "").strip()
    if not raw:
        return 1.0
    try:
        value = float(raw)
    except ValueError:
        return 1.0
    return value if value > 0 else 1.0


def scaled_timeout(seconds: float) -> float:
    """A subprocess/round-trip deadline of *seconds*, widened on the slow arm64 CI lane."""
    return seconds * _scale()
