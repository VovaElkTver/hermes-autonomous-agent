"""Contract tests for hermes_events v0 — one per agreed semantic (26.10)."""

import io
import threading

from hermes_events.clock import Clock
from hermes_events.processor import EventProcessor
from hermes_events.registry import (
    EventKind, Registry, parse_when, persistable_ref, resolve_ref,
)
from hermes_events.store import EventStore


def _sink(ts):
    """Module-level importable handler for persistence round-trips."""
    return ts


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def now(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def _mk(tmp_path=None):
    reg = Registry(FakeClock())
    j = io.StringIO()
    return reg, j


def test_absolute_and_relative_registration():
    fc = FakeClock(t=5000.0)
    assert parse_when(4200.0, now_fn=fc.now) == 4200.0
    assert parse_when("in 1h", now_fn=fc.now) == 5000.0 + 3600
    assert parse_when("через 30 мин", now_fn=fc.now) == 5000.0 + 1800


def test_once_fires_once_and_is_removed():
    reg, j = _mk()
    p = EventProcessor(registry=reg, clock=reg._clock, journal_path=j)
    hits = []
    reg.register(id="o", kind="once", when=1005.0, handler=hits.append)
    assert p.tick() == 0 and hits == []          # now==t: after is strict
    reg._clock.advance(6)
    assert p.tick() == 1
    p._run_one("o")
    assert hits == [1006.0] and len(reg) == 0
    reg._clock.advance(100)
    assert p.tick() == 0                          # nothing to re-fire


def test_periodic_rearms_from_completion_not_schedule():
    reg, j = _mk()
    p = EventProcessor(registry=reg, clock=reg._clock, journal_path=j)
    seen = []

    def slow(fired_at):
        reg._clock.advance(30)   # the handler itself consumes 30s
        seen.append(fired_at)

    reg.register(id="p", kind="periodic", when=1010.0, handler=slow, period_s=100)
    reg._clock.advance(11)
    assert p.tick() == 1
    p._run_one("p")
    ev = reg.all()[0]
    # completion at 1041 -> next at 1141, NOT 1010+100=1110 (anti-avalanche law)
    assert ev.t == 1141.0


def test_refire_while_in_flight_dedups_via_wait_again():
    reg, j = _mk()
    p = EventProcessor(registry=reg, clock=reg._clock, journal_path=j)
    gate = threading.Event()
    done = []
    reg.register(id="d", kind="periodic", when=1001.0,
                 handler=lambda ts: (gate.wait(2), done.append(ts)), period_s=10)
    reg._clock.advance(1)
    p.tick()                                   # in_flight, queued
    p._worker = None                           # keep the queue manual
    reg._clock.advance(50)
    p.tick()                                   # would fire again while running
    ev = reg.all()[0]
    assert ev.in_flight and ev.wait_again is False  # t=inf disarmed after fire
    # simulate the running completion path instead: pending queue has 1 job only
    assert p._queue.qsize() == 1


def test_service_runs_inline_never_queued():
    reg, j = _mk()
    p = EventProcessor(registry=reg, clock=reg._clock, journal_path=j)
    ran = []
    reg.register(id="s", kind="service", when=1001.0, handler=ran.append, period_s=5)
    reg._clock.advance(2)  # after is strict: now must exceed t
    p.tick()
    assert ran == [1002.0] and p._queue.qsize() == 0
    reg._clock.advance(6)
    p.tick()
    assert ran[-1] == 1008.0 and p._queue.qsize() == 0


def test_handler_exception_keeps_processor_alive_and_journal_records():
    reg, j = _mk()
    p = EventProcessor(registry=reg, clock=reg._clock, journal_path=j)
    reg.register(id="x", kind="once", when=1001.0,
                 handler=lambda ts: (_ for _ in ()).throw(Boom()))
    class Boom(Exception):
        pass
    reg._clock.advance(5)
    assert p.tick() == 1
    p._run_one("x")
    assert "failed" in j.getvalue() and len(reg) == 0  # ONCE removed anyway


def test_silence_is_default_wake_only_on_event():
    reg, j = _mk()
    woken = []
    p = EventProcessor(registry=reg, clock=reg._clock, journal_path=j,
                       wake_handler=lambda ev, at: woken.append(ev.id))
    reg.register(id="w", kind="once", when=1001.0, handler=lambda ts: None)
    p.tick()
    p._run_one("w")
    assert woken == ["w"]


# ---- separate DB (vovak 26.10: "отдельная db") ----------------------------

def test_registry_round_trips_through_its_own_db(tmp_path):
    db = tmp_path / "events.db"
    fc = FakeClock()
    reg = Registry(fc, store=EventStore(db))
    reg.register(id="p", kind="periodic", when=1200.0,
                 handler=_sink, period_s=60, meta={"why": "tick"})
    assert db.exists()                       # its own file, not state.db
    # "restart": fresh store handle + fresh registry over the same file
    reg2 = Registry(FakeClock(1300.0), store=EventStore(db))
    assert reg2.load() == []
    ev = reg2.get("p")
    assert ev is not None and ev.t == 1200.0 and ev.meta == {"why": "tick"}
    assert resolve_ref(ev.handler_ref) is _sink


def test_rearm_and_removal_are_written_through(tmp_path):
    db = tmp_path / "events.db"
    fc = FakeClock()
    reg = Registry(fc, store=EventStore(db))
    p = EventProcessor(registry=reg, clock=fc, journal_path=io.StringIO())
    reg.register(id="p", kind="periodic", when=1010.0,
                 handler=_sink, period_s=100)
    reg.register(id="o", kind="once", when=1010.0, handler=_sink)
    fc.advance(11)
    assert p.tick() == 2
    p._run_one("p")
    p._run_one("o")
    rows = {r["id"]: r for r in reg._store.rows()}
    assert rows["p"]["t"] == 1111.0          # rearm persisted
    assert rows["p"]["in_flight"] == 0       # crash marker cleared
    assert "o" not in rows                   # ONCE removed from the DB too


def test_crash_mid_flight_reloads_at_least_once(tmp_path):
    db = tmp_path / "events.db"
    fc = FakeClock()
    reg = Registry(fc, store=EventStore(db))
    p = EventProcessor(registry=reg, clock=fc, journal_path=io.StringIO())
    reg.register(id="c", kind="periodic", when=1001.0,
                 handler=_sink, period_s=10)
    fc.advance(2)
    assert p.tick() == 1                      # enqueued, in_flight persisted
    # crash: the worker never completes. fresh process, later clock.
    reg2 = Registry(FakeClock(1500.0), store=EventStore(db))
    notes = reg2.load()
    assert any("re-armed after crash" in n for n in notes)
    ev = reg2.get("c")
    assert ev.in_flight is False and ev.t == 1500.0   # due on next tick


def test_non_importable_handler_needs_explicit_ref(tmp_path):
    reg = Registry(FakeClock(), store=EventStore(tmp_path / "events.db"))
    try:
        reg.register(id="l", kind="once", when=1.0, handler=lambda ts: None)
        raise AssertionError("lambda without handler_ref must be refused")
    except ValueError as exc:
        assert "handler_ref" in str(exc)
    reg.register(id="l", kind="once", when=1.0,
                 handler=lambda ts: None, handler_ref="hermes_events.clock:now")
    assert persistable_ref(_sink) == f"{_sink.__module__}:_sink"


def test_unresolvable_ref_stays_in_db_unloaded(tmp_path):
    db = tmp_path / "events.db"
    store = EventStore(db)
    store.upsert(id="gone", kind="once", t=10.0, period_s=None,
                 handler_ref="no.such.module:fn", meta={},
                 in_flight=False, wait_again=False)
    reg = Registry(FakeClock(), store=store)
    notes = reg.load()
    assert any("unresolvable" in n for n in notes)
    assert len(reg) == 0 and store.count() == 1   # kept, not silently deleted
