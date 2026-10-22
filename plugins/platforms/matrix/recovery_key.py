"""Matrix E2EE recovery-key helpers (scoped reads, one-time output file).

Extracted from adapter.py (FILE_LINES ratchet). Names stay importable from the
adapter module (tests and call sites reference ``adapter._scoped_recovery_key``).
"""

from __future__ import annotations

import logging
import os
from contextlib import suppress
from pathlib import Path
from typing import Optional

from gateway.platforms._shared import get_scoped_secret as _get_scoped_secret

logger = logging.getLogger(__name__)


def _recovery_key_output_path() -> Optional[Path]:
    """MATRIX_RECOVERY_KEY_OUTPUT_FILE via the profile-scoped reader: a bare os.getenv under
    multiplex resolves the default profile's path, writing/finding the wrong profile's file."""
    output_file = _get_scoped_secret("MATRIX_RECOVERY_KEY_OUTPUT_FILE", "").strip()
    return Path(output_file).expanduser() if output_file else None


def _write_matrix_recovery_key_output_file(recovery_key: str) -> Optional[Path]:
    """Write a generated recovery key to MATRIX_RECOVERY_KEY_OUTPUT_FILE (0600, never overwritten)."""
    path = _recovery_key_output_path()
    if path is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(recovery_key)
            fh.write("\n")
    except Exception:
        with suppress(OSError):
            os.close(fd)
        raise
    return path


def _get_matrix_recovery_key_output_target() -> tuple[Optional[Path], str]:
    """Return a usable one-time recovery-key output path, or a redacted reason."""
    path = _recovery_key_output_path()
    if path is None:
        return None, "not_configured"
    if path.exists():
        return None, "exists"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return None, f"unusable: {exc}"
    return path, ""


def _handle_generated_matrix_recovery_key(mxid: str, recovery_key: str) -> None:
    """Handle a freshly generated Matrix recovery key without logging it."""
    try:
        output_path = _write_matrix_recovery_key_output_file(recovery_key)
    except FileExistsError:
        logger.warning(
            "Matrix: bootstrapped cross-signing for %s. Recovery key output file "
            "already exists; refusing to overwrite. Store the generated key "
            "securely and set MATRIX_RECOVERY_KEY for future restarts.", mxid)
        return
    except Exception as exc:
        logger.warning(
            "Matrix: bootstrapped cross-signing for %s, but failed to write "
            "MATRIX_RECOVERY_KEY_OUTPUT_FILE: %s. Store the generated key "
            "securely and set MATRIX_RECOVERY_KEY for future restarts.", mxid, exc)
        return
    if output_path:
        logger.warning(
            "Matrix: bootstrapped cross-signing for %s. A new recovery key was written to %s with mode 0600. Move it "
            "to your secret store and set MATRIX_RECOVERY_KEY for future restarts.",
            mxid, output_path)
    else:
        logger.warning(
            "Matrix: bootstrapped cross-signing for %s. A new recovery key was generated but will "
            "not be logged. Set MATRIX_RECOVERY_KEY_OUTPUT_FILE to write it once with mode 0600, "
            "or configure MATRIX_RECOVERY_KEY from your Matrix client before future restarts.",
            mxid)


def _scoped_recovery_key() -> str:
    """MATRIX_RECOVERY_KEY via the profile-scoped reader: a bare os.getenv under multiplex resolves
    the default profile's key and verification fails with "Key MAC does not match"."""
    return _get_scoped_secret("MATRIX_RECOVERY_KEY", "").strip()
