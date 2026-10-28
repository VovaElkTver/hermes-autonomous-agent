"""Regression tests for controller.ticker hardening (audit 2026-10-28).

The ticker runs real daemon threads against a temp state.db: ``model.connect``
is patched to one shared file so the pump thread, the watchdog thread and the
scratch connections all see the same rows (raw SCHEMA_SQL path).
"""
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
