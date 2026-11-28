"""Д1b-(а): pause-for-update ACK must carry the gateway's real deferral budget.

Incident 28.11 (Win): gateway logged "deferring stop() ... cap=1800s" while the ACK told the
updater ~30s; the updater force-killed the turn it was mid-wait on → unknown_execution.
The kill side may only outrun the budget the knowing side declared.
"""
import asyncio
import threading

from gateway.run_control_handlers import pause_for_update_verb


class _StubRunner:
    def __init__(self, budget, active):
        self._restart_after_turn_timeout = budget
        self._active = active
        self.restart_calls = []

    def _active_work_count(self):
        return self._active

    def request_restart(self, *, detached, via_service):
        self.restart_calls.append((detached, via_service))
        return True


def _call_handler(runner):
    loop = asyncio.new_event_loop()
    started = threading.Event()

    def _loop_thread():
        asyncio.set_event_loop(loop)
        started.set()
        loop.run_forever()

    t = threading.Thread(target=_loop_thread, daemon=True)
    t.start()
    started.wait(5)
    handler = pause_for_update_verb(runner, loop)
    box: dict = {}

    def _worker():
        box["ack"] = handler()

    w = threading.Thread(target=_worker)
    w.start()
    w.join(10)
    loop.call_soon_threadsafe(loop.stop)
    t.join(5)
    return box["ack"]


def test_ack_carries_active_turn_deferral_budget():
    runner = _StubRunner(budget=1800.0, active=1)
    ack = _call_handler(runner)
    assert ack["pausing"] is True
    assert ack["active_work"] is True
    assert ack["drain_timeout"] >= 1800.0
    assert runner.restart_calls == [(False, True)]


def test_ack_without_active_work_keeps_teardown_drain_only():
    runner = _StubRunner(budget=1800.0, active=0)
    ack = _call_handler(runner)
    assert ack["pausing"] is True
    assert ack["active_work"] is False
    # No active units: must NOT advertise the 1800s defer budget as if a turn needed it.
    assert ack["drain_timeout"] < 1800.0
