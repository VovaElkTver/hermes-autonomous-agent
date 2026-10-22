"""Scoped-env config helpers for the Matrix adapter (moved out of adapter.py).

Kept importable from ``plugins.platforms.matrix.adapter`` re-exports so existing
tests monkeypatching adapter attributes keep working.
"""

from __future__ import annotations

import logging
from typing import Any

from gateway.platforms._shared import get_scoped_secret as _get_scoped_secret

logger = logging.getLogger(__name__)


def _env_truthy(name: str, default: str = "") -> bool:
    """Return True when the env var is one of true/1/yes (case-insensitive)."""
    return str(_get_scoped_secret(name, default)).lower() in ("true", "1", "yes")


def _env_number(name: str, default, cast):
    """Parse a numeric env var, falling back to *default* on ValueError."""
    try:
        return cast(_get_scoped_secret(name, str(default)))
    except ValueError:
        return default


def _csv_set(raw: Any) -> set[str]:
    """Normalize a comma-separated string or list into a set of stripped tokens."""
    if isinstance(raw, list):
        return {str(r).strip() for r in raw if str(r).strip()}
    return {r.strip() for r in str(raw).split(",") if r.strip()}


def _default_approval_timeout_seconds() -> int:
    """Approval-prompt lifetime when MATRIX_APPROVAL_TIMEOUT_SECONDS is unset: follow the
    shared ``approvals.timeout`` config so the Matrix prompt never dies before the ledger
    entry it approves (a 300s prompt over a 3600s window stranded live commands silently)."""
    try:
        from tools.approval_context import _get_approval_timeout

        return max(int(_get_approval_timeout()), 1)
    except Exception:  # health: allow BLE001 -- config-read boundary keeps the platform default
        logger.debug("approvals.timeout unreadable; using 300s approval default", exc_info=True)
        return 300
