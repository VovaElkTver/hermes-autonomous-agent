"""Code-side event handlers (importable refs for the durable registry).

A registry row stores ``module:qualname`` — these functions must stay
importable at boot; renaming one parks its events loud on load, never silent.
"""

from __future__ import annotations

import logging

_log = logging.getLogger("hermes_events.handlers")


def heartbeat_probe(ts: float) -> None:
    """Smoke handler: proves the tick->queue->execute path end to end."""
    _log.info("heartbeat_probe fired at %s", ts)
