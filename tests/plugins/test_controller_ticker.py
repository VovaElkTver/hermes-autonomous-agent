"""Regression tests for controller.ticker hardening (audit 2026-10-28).

The ticker runs real daemon threads against a temp state.db: ``model.connect``
is patched to one shared file so the pump thread, the watchdog thread and the
scratch connections all see the same rows (raw SCHEMA_SQL path).
"""
import sqlite3
import threading
import time

import pytest


@pytest.fixture()
def db_connect(tmp_path, monkeypatch):
    """controller.model.connect() -> fresh connection on one temp state.db."""
    from controller import model
    real = model.connect
    db = tmp_path / "state.db"

    def connect(db_path=None):
        return real(db)

    monkeypatch.setattr(model, "connect", connect)
    return connect


def _rows(connect, sql):
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def _wait_for(predicate, timeout=10.0):
    """Poll until predicate() is truthy (threads + temp DB are real)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_watchdog_ticks_and_alerts_while_pump_wedged(db_connect, monkeypatch):
    """Audit blocker 1: a drain wedged inside run_cycle never returns to the
    scheduler — the watchdog must live on another thread and still tick, still
    age out the last 'pump' heartbeat and alert."""
    from controller import ticker

    monkeypatch.setattr(ticker, "PUMP_STALE_S", 0.1)
    lines = []
    wedged = threading.Event()
    state = {"pump_hb_ts": None}

    t = ticker.Ticker(intervals={"pump": 0.05, "watchdog": 0.1},
                      deliver_fn=lines.append, gates_path=None)

    def fake_pump(conn):
        # First tick lands its heartbeat, then wedges forever (run_cycle that
        # never returns). Every later pump tick waits in the same wedge.
        if state["pump_hb_ts"] is None:
            t._heartbeat(conn, "pump", {"tick": 1})
            state["pump_hb_ts"] = time.time()
        assert wedged.wait(30), "test teardown never released the wedge"

    monkeypatch.setattr(t, "run_pump", fake_pump)
    t.start()
    try:
        assert _wait_for(lambda: state["pump_hb_ts"] is not None), \
            "pump tick never landed"
        ok = _wait_for(lambda: len(_rows(
            db_connect, "SELECT ts FROM ctl_ticker_heartbeat WHERE kind='watchdog'"
            " AND ts >= " + str(state["pump_hb_ts"]))) >= 2
            and _rows(db_connect, "SELECT alert_key FROM ctl_alert_state"
                       " WHERE alert_key='pump-stale'"))
        assert ok, "watchdog thread did not keep ticking / alert while pump wedged"
        assert any("PUMP TICK ЗАМОЛЧАЛ" in line for line in lines)
    finally:
        wedged.set()
        t.stop()

class _LockedWrites:
    """Connection proxy whose heartbeat INSERTs always lose the write race —
    the documented WAL-hog failure mode of this node (marathon turn holding
    the write lock)."""

    def __init__(self, real):
        self._real = real

    def execute(self, sql, *args):
        if "INSERT INTO ctl_ticker_heartbeat" in sql:
            raise sqlite3.OperationalError("database is locked")
        return self._real.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_ticker_survives_a_locked_database(db_connect, monkeypatch):
    """Audit blocker 2: 'database is locked' on write (second exception out
    of the except-block) used to kill the thread forever. The thread stays
    alive, keeps ticking, and the half-executed cycle is rolled back, not
    committed as a done fact."""
    from controller import model, ticker

    inner = model.connect
    monkeypatch.setattr(model, "connect", lambda db_path=None: _LockedWrites(inner()))
    calls = {"pump": 0}
    t = ticker.Ticker(intervals={"pump": 0.05, "watchdog": 3600}, gates_path=None)

    def locked_pump(conn):
        calls["pump"] += 1
        conn.execute("INSERT INTO ctl_event(event_id, ts, kind, origin, payload)"
                     " VALUES (?,?,?,?,?)",
                     (f"half-{calls['pump']}", 0, "half-cycle", "test", "{}"))
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(t, "run_pump", locked_pump)
    t.start()
    try:
        assert _wait_for(lambda: calls["pump"] >= 3), \
            "the tick cadence died with the locked writer"
        assert t.alive()
    finally:
        t.stop()
    rows = _rows(inner, "SELECT COUNT(*) c FROM ctl_alert_state"
                        " WHERE alert_key='tick-error:pump'")[0]["c"]
    assert rows == 1  # reported exactly once (deduped), despite every tick failing
    events = _rows(inner, "SELECT COUNT(*) c FROM ctl_event"
                          " WHERE kind='half-cycle'")[0]["c"]
    assert events == 0  # rolled back — never committed as a done fact
    hbs = _rows(inner, "SELECT COUNT(*) c FROM ctl_ticker_heartbeat")[0]["c"]
    assert hbs == 0  # every heartbeat degraded to skip, none raised


def test_heartbeat_retries_then_skips_without_raising(caplog):
    """Item 3: failing heartbeat = skip + debug line, never an exception; a
    transient lock is retried within the bounded budget."""
    import logging
    from controller import ticker

    t = ticker.Ticker(gates_path=None)

    class Flaky:
        def __init__(self, fail_times):
            self.fail_times, self.attempts, self.committed = fail_times, 0, False

        def execute(self, sql, *args):
            if sql.startswith("INSERT"):
                self.attempts += 1
                if self.attempts <= self.fail_times:
                    raise sqlite3.OperationalError("database is locked")

        def commit(self):
            self.committed = True

        def rollback(self):
            pass

    transient = Flaky(ticker.HB_RETRIES - 1)
    t._heartbeat(transient, "pump", {})
    assert transient.committed and transient.attempts == ticker.HB_RETRIES

    permanent = Flaky(99)
    with caplog.at_level(logging.DEBUG, logger="controller.ticker"):
        t._heartbeat(permanent, "pump", {})  # must not raise
    assert not permanent.committed
    assert permanent.attempts == ticker.HB_RETRIES
    assert "skipped" in caplog.text


def test_first_pump_tick_waits_half_an_interval(db_connect, monkeypatch):
    """Item 6: the boot tick must not storm a full drain into the gateway
    warm-up — first pump tick lands at interval/2, then the full cadence."""
    from controller import ticker

    start = time.monotonic()
    first = []
    t = ticker.Ticker(intervals={"pump": 0.6, "watchdog": 3600}, gates_path=None)

    def fake_pump(conn):
        if not first:
            first.append(time.monotonic() - start)

    monkeypatch.setattr(t, "run_pump", fake_pump)
    t.start()
    try:
        time.sleep(0.15)
        assert first == [], "drain started immediately at boot"
        assert _wait_for(lambda: bool(first), timeout=3), "pump never ticked"
        assert first[0] >= 0.25  # interval/2 = 0.3 minus jitter margin
    finally:
        t.stop()
