"""Д2 integration: a zombie gateway (loop alive, transport dead) self-escalates.

Incident 28.11: a gateway lived 9h15m without processing Matrix traffic; freshness
primitives existed but nothing consumed them into action inside the fork (external
watchdogs are forbidden by the owner regime, ТЗ §4.2). The loop-liveness watchdog only
proves the loop dispatches — it stays silent for exactly this failure shape.

Implementation 28.11: ``GatewaySessionWatchersMixin._stale_transport_guard`` consumes the
Matrix adapter's ``_last_sync_ts`` (advanced by every sync response) and calls
``request_restart(via_service=True)`` past 10 min on two consecutive ticks.
"""

import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


def test_write_loop_heartbeat_primitives(tmp_path, monkeypatch):
    """Green pin: the heartbeat primitive itself — atomic JSON with pid/updated_at."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from gateway.shutdown_watchdog import get_loop_heartbeat_path, write_loop_heartbeat

    path = write_loop_heartbeat(pid=4242, home=tmp_path)
    assert path == get_loop_heartbeat_path(tmp_path)
    payload = json.loads(path.read_text())
    assert payload["pid"] == 4242 and "updated_at" in payload


class _FakeAdapter:
    def __init__(self, age_s: float) -> None:
        self._client = object()  # connected
        self._last_sync_ts = time.time() - age_s


class _StubRunner:
    """Duck of GatewayRunner for the guard surface only; counts restart requests."""

    def __init__(self, adapter) -> None:
        from gateway.run_watchers import GatewaySessionWatchersMixin

        self.__class__ = type("Stub", (GatewaySessionWatchersMixin, _StubRunner), {})
        self.adapters = {"matrix": adapter}
        self.config = None
        self._running = True
        self.restarts = []

    def request_restart(self, **kwargs):
        self.restarts.append(kwargs)
        self._running = False  # stops the supervised loop in test


def _drain(coro_factory, steps: int):
    """Run the guard as a supervised task with real sleeps replaced by one event-loop tick."""
    import gateway.run_watchers as rw

    async def _main():
        calls = []

        async def fake_sleep(owner, seconds):
            calls.append(seconds)
            await asyncio.sleep(0)
            if len(calls) > steps:
                raise asyncio.CancelledError

        orig = rw._interruptible_sleep
        rw._interruptible_sleep = fake_sleep
        try:
            stub = _StubRunner(_FakeAdapter(age_s=9999))
            task = asyncio.ensure_future(stub._stale_transport_guard(
                interval=1, threshold_s=600, startup_grace_s=1))
            try:
                await asyncio.wait_for(task, timeout=5)
                done = True
            except (asyncio.CancelledError, TimeoutError):
                done = task.done() and not task.cancelled()
            return stub, done
        finally:
            rw._interruptible_sleep = orig

    return asyncio.run(_main())


def test_stale_transport_escalates_to_restart_request():
    """Zombie injection (sync freshness frozen): the guard must fire request_restart with
    the service-restart route exactly once, after two stale ticks."""
    stub, completed = _drain(None, steps=10)
    assert completed, "guard did not return after escalating (still looping)"
    assert stub.restarts == [{"via_service": True}]


def test_fresh_transport_never_escalates():
    """A gateway whose sync advances must never be restarted by the guard."""

    class _Fresh(_FakeAdapter):
        pass

    async def _main():
        import gateway.run_watchers as rw
        stub = _StubRunner(_Fresh(age_s=0))
        ticks = 0

        async def fake_sleep(owner, seconds):
            nonlocal ticks
            ticks += 1
            await asyncio.sleep(0)
            if ticks > 6:
                stub._running = False

        orig = rw._interruptible_sleep
        rw._interruptible_sleep = fake_sleep
        try:
            await asyncio.wait_for(
                stub._stale_transport_guard(interval=1, threshold_s=600, startup_grace_s=1),
                timeout=5)
            return stub
        finally:
            rw._interruptible_sleep = orig

    stub = asyncio.run(_main())
    assert stub.restarts == []
