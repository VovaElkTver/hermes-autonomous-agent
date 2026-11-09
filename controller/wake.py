"""controller.wake — cross-process wake seam for the ticker (go Вл 09.11).

Doctrine 28.10/02.11: pump/watchdog/followups deadlines are counters over one
1 Hz base beat — a due counter is a POLICY, not latency. When another process
mutates the queue (a transition, a follow-up registered, an approval answered)
the ticker must learn it on the NEXT beat, not at the next pump deadline.

The signal is a one-line seq file, deliberately NOT the DB: the idle beat must
stay zero-SQL (doctrine), and 28.10's wedge class was exactly a shared sqlite
writer — a stat+read of a tiny file can never block on the pump's lock.

Live path resolution (HERMES_CONTROLLER_WAKE override) so tests and other
profiles never fight over the production seq file.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

ENV_OVERRIDE = "HERMES_CONTROLLER_WAKE"


def path() -> Path:
    override = os.environ.get(ENV_OVERRIDE)
    if override:
        return Path(override)
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "controller.wake"


def notify() -> bool:
    """Ring the bell: best-effort, never raises, never blocks on the DB."""
    p = path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(str(time.time_ns()), encoding="ascii")
        os.replace(tmp, p)
        return True
    except OSError:
        return False


def read_seq() -> str:
    """Current bell identity ('' = never rung). Cheap: one tiny file read."""
    try:
        return path().read_text(encoding="ascii").strip()
    except OSError:
        return ""
