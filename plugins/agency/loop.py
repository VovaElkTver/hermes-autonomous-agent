"""Between-call duties of the agency loop (TZ v0.2 §6).

The loop is the sole owner of the agency ledger DB: inside a tool call it executes
the short transaction core; between calls the same core performs service work
that needs no intellect — tick counting over obligations, storage maintenance.
Growth rule (TZ §6): new duty → code in the core, not a new tool; no LLM inside
the loop.

Acceptance case 2b applies: this module must open no database connections of
its own and hold no db-path literals — all storage access goes through
plugins.agency.store.
"""
from __future__ import annotations

import logging
import threading

from plugins.agency import store

logger = logging.getLogger("agency.loop")


class LoopWorker(threading.Thread):
    """Periodic tick runner. Daemon: dies with the process, never blocks exit."""

    def __init__(self, db_path=None, interval_s: float = 60.0,
                 stop_event: threading.Event | None = None):
        super().__init__(name="agency-loop", daemon=True)
        self.db_path = db_path
        self.interval_s = max(0.01, float(interval_s))
        # NB: never name it `_stop` — that shadows an internal Thread hook.
        self.stop_event = stop_event or threading.Event()

    def shutdown(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        logger.info("agency loop online (interval %.1fs)", self.interval_s)
        while not self.stop_event.wait(self.interval_s):
            try:
                self.tick()
            except Exception:                     # never let a failed tick die the loop
                logger.exception("agency loop tick failed")

    def tick(self) -> dict:
        """One pass of between-call duties, all through the store core."""
        marked = store.sweep_overdue(self.db_path)
        refreshed = store.refresh_projections(self.db_path)
        store.wal_checkpoint(self.db_path)
        if marked:
            logger.info("agency loop: %d commitment(s) marked overdue (%s)",
                        len(marked), ",".join(str(s) for s in marked))
        if refreshed:
            logger.info("agency loop: %d projection(s) refreshed", refreshed)
        return {"overdue_marked": marked, "projections_refreshed": refreshed}
