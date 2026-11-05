"""controller.effects — durable side-effect queue (port of donor mechanism #4).

Ported from dsh-superagent-prototype ``src/app/effects.ts`` @6337fc8 (audit
2026-11-01; mechanism donor, runtime NOT adopted). Semantics kept:

- an effect is upserted INSIDE the caller's transaction and coalesces with any
  waiting row of the same (kind, key) while keeping its queue position (seq);
  the payload bump increments ``generation`` so a running attempt's finish-up
  can never delete a fresher payload;
- :class:`Later` reschedules without growing the failure count (busy, not broken);
- real failures back off exponentially, capped per kind;
- rows persist across restarts: nothing is lost between passes.

The queue is pull-based like the donor's dispatch loop: producers call
:meth:`Effects.enqueue` inside their transaction, and the pump/ticker pass calls
:meth:`Effects.run_pending` after commit. Handlers register once per kind.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Callable

#: Consecutive failures of one row before its kind reports an error state
#: (donor parity: a kind is healthy again after its next success).
FAILURES_BEFORE_ERROR = 3


class Later(Exception):
    """Signals "an effect cannot run right now" without counting a failure.

    Raise from a handler when the condition is temporary (paused processing,
    all slots busy). The row is rescheduled after the kind's initial retry delay
    and its attempts counter is left untouched.
    """


@dataclass(frozen=True)
class _Handler:
    run: Callable[..., None]
    limit: Callable[[], int]
    retry_initial: float
    retry_max: float
    ordered: bool


def _default_limit() -> int:
    return 1


def _json_or_empty(payload) -> str:
    return json.dumps(payload, ensure_ascii=False) if payload is not None else ""


def _unjson_or_text(text: str):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return text


class Effects:
    """SQLite-backed durable queue; every method takes/returns plain data.

    One instance wraps one open connection (same transactional authority as the
    controller's other writers). Handlers run synchronously inside
    ``run_pending`` — a pass owns its side effects end to end, exactly one
    process drives them.
    """

    def __init__(self, conn, now=None):
        self.conn = conn
        self._now = now or time.time
        self._handlers: dict[str, _Handler] = {}
        row = self.conn.execute("SELECT COALESCE(MAX(seq),0) AS s FROM ctl_effect").fetchone()
        self.seq = int(row["s"])

    # -- registration ---------------------------------------------------------

    def handle(self, kind: str, run, *, limit=None,
               retry=(2.0, 60.0), ordered: bool = False) -> None:
        """Register the handler for a kind. ``run(payload, key)`` does the work."""
        if kind in self._handlers:
            raise ValueError(f"the {kind} effects already have a handler")
        self._handlers[kind] = _Handler(
            run=run, limit=limit or _default_limit,
            retry_initial=float(retry[0]), retry_max=float(retry[1]), ordered=ordered)

    # -- producers ------------------------------------------------------------

    def enqueue(self, kind: str, key: str, payload=None) -> None:
        """Upsert an effect, coalescing with any waiting row of the same kind+key.

        Call inside your own transaction to make the state write and the effect
        atomic; this method only writes and leaves dispatching to the pass.
        """
        self.seq += 1
        now = self._now()
        self.conn.execute(
            "INSERT INTO ctl_effect (kind,key,value,seq,due,since) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(kind,key) DO UPDATE SET value=excluded.value,"
            " generation=generation+1, attempts=0, error='', due=excluded.due",
            (kind, key, _json_or_empty(payload), self.seq, now, now))

    def cancel(self, kind: str, key: str) -> None:
        """Drop a queued row by kind+key; an already-running attempt finishes."""
        self.conn.execute("DELETE FROM ctl_effect WHERE kind=? AND key=?", (kind, key))

    def pending(self, kind: str, key: str | None = None) -> bool:
        if key is None:
            row = self.conn.execute(
                "SELECT 1 FROM ctl_effect WHERE kind=? LIMIT 1", (kind,)).fetchone()
        else:
            row = self.conn.execute(
                "SELECT 1 FROM ctl_effect WHERE kind=? AND key=?", (kind, key)).fetchone()
        return row is not None

    # -- consumer (donor dispatch semantics on a synchronous core) -------------

    def kick(self) -> None:
        """Back-compat no-op: rows dispatch on the next pass's ``run_pending()``."""

    def run_pending(self, kind: str | None = None) -> int:
        """Dispatch everything that can start now; returns rows executed.

        Ordered kinds block behind their oldest row even when it is not yet due,
        and honor ``limit`` as one-at-a-time (donor parity).
        """
        done = 0
        for k, h in self._handlers.items():
            if kind is not None and k != kind:
                continue
            now = self._now()
            rows = self.conn.execute(
                "SELECT * FROM ctl_effect WHERE kind=? AND due<=? ORDER BY seq"
                + (" LIMIT 1" if h.ordered else " LIMIT 32"),
                (k, now)).fetchall()
            lim = h.limit()
            for row in rows:
                if done >= max(1, lim):
                    break
                if self._attempt(k, h, row):
                    done += 1
                elif h.ordered:
                    break  # oldest row holds an ordered kind: single-flight
        return done

    def drain(self, kind: str | None = None) -> None:
        """Run pending rows repeatedly until none remain (test convenience)."""
        while self.run_pending(kind):
            pass  # rows may re-enqueue same-kind work or unblock an ordered barrier

    # -- internals ------------------------------------------------------------

    def _attempt(self, kind: str, h: _Handler, row) -> bool:
        """Run one row once; bookkeeping mirrors donor ``attempt()``.

        Returns whether this pass spent its dispatch slot here — any started
        handler did, including one that raised ``Later``.
        """
        try:
            h.run(_unjson_or_text(row["value"]), row["key"])
        except Later:
            self._reschedule(kind, row, self._now() + h.retry_initial)
            return True
        except Exception as exc:  # any failure books a capped-retry slot
            attempts = int(row["attempts"]) + 1
            delay = min(h.retry_max, h.retry_initial * 2 ** min(attempts, 16))
            self._reschedule(kind, row, self._now() + delay, str(exc)[:1000], attempts)
            return True
        self.conn.execute(
            "DELETE FROM ctl_effect WHERE kind=? AND key=? AND seq=? AND generation=?",
            (kind, row["key"], row["seq"], row["generation"]))
        # A zero-row DELETE means the row was replaced while running: the
        # newer payload stays queued with a fresh generation — nothing to do.
        return True

    def _reschedule(self, kind: str, row, due: float,
                    error: str = "", attempts=None) -> None:
        if attempts is None:
            attempts = int(row["attempts"])
        self.conn.execute(
            "UPDATE ctl_effect SET due=?, attempts=?, error=?"
            " WHERE kind=? AND key=? AND seq=? AND generation=?",
            (due, attempts, error, kind, row["key"], row["seq"], row["generation"]))

    # -- introspection ----------------------------------------------------------

    def status(self, kind: str) -> dict:
        """Waiting count and last failure detail for one kind (health view)."""
        row = self.conn.execute(
            "SELECT COUNT(*) AS waiting, MIN(since) AS since, MAX(attempts) AS attempts,"
            " (SELECT e2.error FROM ctl_effect e2 WHERE e2.kind=e.kind AND e2.attempts>0"
            "   ORDER BY seq DESC LIMIT 1) AS err"
            " FROM ctl_effect e WHERE kind=? GROUP BY kind", (kind,)).fetchone()
        if row is None:
            return {"waiting": 0, "since": None, "attempts": 0,
                    "error": "", "failing": False}
        attempts = int(row["attempts"] or 0)
        return {"waiting": int(row["waiting"]), "since": row["since"],
                "attempts": attempts, "error": row["err"] or "",
                "failing": attempts >= FAILURES_BEFORE_ERROR}
