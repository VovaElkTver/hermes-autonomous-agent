"""Safe-mode boot identity (fork).

``/reset-safe`` force-quits the node and arms a one-shot boot flag; the next
gateway boot consumes it and comes up as the SAFE identity: owner transport
only — cron, controller pump, MCP discovery and background tool watchers stay
off, replies carry a banner. ``/reset`` force-quits and disarms the flag, so
the next boot is the ordinary autonomous one. No snapshots, no undo: each
button *is* the next boot's identity, quit means quit.
"""
from __future__ import annotations

import os
from pathlib import Path

BOOT_FLAG_NAME = "NEXT_BOOT_SAFE"
_ENV = "HERMES_SAFE_MODE"
_state = False


def home_dir() -> Path:
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def consume_boot_flag() -> bool:
    """Read and disarm ``~/.hermes/NEXT_BOOT_SAFE``; caches the identity for boot.

    Called once during gateway startup, before subsystems are launched. A
    corrupt/any-content file counts as armed (fail toward safety)."""
    global _state
    flag = home_dir() / BOOT_FLAG_NAME
    armed = flag.exists()
    if armed:
        try:
            flag.unlink()
        except OSError:
            pass
    set_safe(armed)
    return armed


def set_safe(value: bool) -> None:
    global _state
    _state = bool(value)
    os.environ[_ENV] = "1" if _state else "0"


def is_safe() -> bool:
    if _state:
        return True
    # Child tools / secondary entry points inherit the identity via env only.
    return os.environ.get(_ENV, "") == "1"
