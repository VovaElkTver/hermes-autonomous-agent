"""Event processor: the tick body + execution queue.

Per-tick contract (agreed 26.10, the anti-cron law):
  1. deterministic scan only — ``now() after t`` over the registry;
  2. firing enqueues, it does not execute;
  3. queue dedups per event id (in_flight -> mark wait_again, never clone);
  4. PERIODIC rearms from COMPLETION time, never from the planned instant;
  5. ONCE is removed (journal-traced) after completion;
  6. SERVICE runs inline in the tick, outside the queue;
  7. default output is silence: nothing enters the room or agent memory;
     a jsonl journal is the only trail.
"""

from __future__ import annotations

import json
import queue
import threading
from pathlib import Path
from typing import TextIO

from hermes_events.registry import EventKind, Registry


class EventProcessor:
    def __init__(
        self, *, registry: Registry, clock, journal_path: Path | TextIO,
        wake_handler=None,
    ) -> None:
        """``wake_handler(event, fired_at)`` is the seam where a due event may
        wake the LLM — the processor itself never talks to the agent."""
        self._registry = registry
        self._clock = clock
        self._journal: TextIO | None = (
            journal_path if hasattr(journal_path, "write")
            else open(Path(journal_path), "a", encoding="utf-8")  # type: ignore[call-overload,arg-type]
        )
        self._wake = wake_handler
        self._queue: queue.Queue[str] = queue.Queue()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()

    # -- journaling ---------------------------------------------------------
    def _log(self, action: str, ev_id: str, **extra) -> None:
        if self._journal is None:
            return
        rec = {"ts": self._clock.now(), "action": action, "event": ev_id, **extra}
        self._journal.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._journal.flush()

    # -- tick ---------------------------------------------------------------
    def tick(self) -> int:
        """One processor pass. Returns how many events fired (not executed)."""
        now_ts = self._clock.now()
        fired = 0
        for ev in self._registry.due(now_ts):
            if ev.kind is EventKind.SERVICE:
                self._log("service-inline", ev.id)
                ev.handler(now_ts)
                ev.t = now_ts + (ev.period_s or 0.0) if ev.period_s else ev.t
                continue
            with self._lock:
                if ev.in_flight:
                    ev.wait_again = True
                    self._log("refire-suppressed", ev.id)
                else:
                    ev.in_flight = True
                    ev.t = float("inf")  # disarm until completion rearms
                    self._queue.put(ev.id)
                    self._log("enqueued", ev.id, fired_at=now_ts)
                fired += 1
        return fired

    # -- execution ----------------------------------------------------------
    def _run_one(self, ev_id: str) -> None:
        ev = next((e for e in self._registry.all() if e.id == ev_id), None)
        if ev is None:
            return
        started = self._clock.now()
        try:
            ev.handler(started)
            self._log("executed", ev_id)
        except Exception as exc:  # the tick must survive handlers
            self._log("failed", ev_id, error=repr(exc))
        done_ts = self._clock.now()
        with self._lock:
            if ev.kind is EventKind.ONCE:
                self._registry.unregister(ev_id)
                self._log("removed-after-exec", ev_id)
            elif ev.kind is EventKind.PERIODIC:
                # auto-rearm from COMPLETION — the core anti-avalanche law
                ev.t = done_ts + (ev.period_s or 0.0)
                self._log("rearmed", ev_id, next_t=ev.t)
            ev.in_flight = False
            if ev.wait_again and ev.kind is not EventKind.ONCE:
                ev.wait_again = False
                ev.in_flight = True
                self._queue.put(ev_id)
                self._log("requeued-wait-again", ev_id)
        if self._wake is not None and ev.kind is not EventKind.SERVICE:
            self._wake(ev, started)

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                ev_id = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._run_one(ev_id)
            finally:
                self._queue.task_done()

    def start(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._stop.clear()
            self._worker = threading.Thread(
                target=self._worker_loop, name="hermes-events-exec", daemon=True)
            self._worker.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=timeout)
            self._worker = None
