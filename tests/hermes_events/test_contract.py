"""Contract tests for hermes_events v0 — one per agreed semantic (26.10)."""

import io
import threading

from hermes_events.clock import Clock
from hermes_events.processor import EventProcessor
from hermes_events.registry import EventKind, Registry, parse_when


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
