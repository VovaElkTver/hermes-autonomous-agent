"""Alert dedup/backoff contract tests (audit 2026-10-28 item 8)."""
import sqlite3
import time

import pytest


@pytest.fixture()
def conn(tmp_path):
    from controller import model
    c = model.connect(tmp_path / "state.db")
    yield c
    c.close()


def _fire(conn, backoff_s=60.0):
    from controller import alerts
    return alerts.maybe_alert(conn, "k", "tick-error", backoff_s=backoff_s,
                              kind="p", error="module.Error")


def _age(conn, seconds):
    conn.execute("UPDATE ctl_alert_state SET last_ts=?"
                 " WHERE alert_key='k'", (time.time() - seconds,))
    conn.commit()


def test_same_error_different_tracebacks_one_alert(tmp_path, monkeypatch, conn):
    """The digest key must see the exception IDENTITY (module.class), never a
    variable message/traceback tail — the same error at a new line number is
    one alert, not an infinite pinger."""
    from controller import model, ticker
    real = model.connect
    monkeypatch.setattr(model, "connect", lambda db_path=None: real(tmp_path / "state.db"))
    lines = []
    t = ticker.Ticker(gates_path=None, deliver_fn=lines.append)
    t._tick_error(conn, "pump",
                  sqlite3.OperationalError("database is locked\n  File a.py:12 in f"))
    t._tick_error(conn, "pump",
                  sqlite3.OperationalError("disk I/O error\n  File b.py:99 in g"))
    assert len(lines) == 1
    assert "sqlite3.OperationalError" in lines[0]
    assert "locked" not in lines[0] and "disk" not in lines[0]


def test_repeat_suppressed_until_the_growing_window(conn):
    assert _fire(conn)
    assert _fire(conn) is None  # inside the 1x window
    _age(conn, 61)
    assert _fire(conn)  # the 60 s window elapsed
    assert _fire(conn) is None  # next window is 120 s
    _age(conn, 121)
    assert _fire(conn)
    n = conn.execute("SELECT count AS n FROM ctl_alert_state"
                     " WHERE alert_key='k'").fetchone()["n"]
    assert n == 2  # 1 -> 2 -> 4 ... doubling


def test_backoff_caps_at_one_day(conn):
    _fire(conn)
    conn.execute("UPDATE ctl_alert_state SET count=40, last_ts=?"
                 " WHERE alert_key='k'", (time.time() - 86399,))
    conn.commit()
    assert _fire(conn) is None
    conn.execute("UPDATE ctl_alert_state SET last_ts=?"
                 " WHERE alert_key='k'", (time.time() - 86401,))
    conn.commit()
    assert _fire(conn)


def test_clear_alert_resets_the_ladder(conn):
    from controller import alerts
    _fire(conn)
    _age(conn, 61)
    assert _fire(conn)  # count -> 1, window now 120 s
    alerts.clear_alert(conn, "k")
    assert _fire(conn)  # cleared condition re-alerts immediately, count resets
    n = conn.execute("SELECT count AS n FROM ctl_alert_state"
                     " WHERE alert_key='k'").fetchone()["n"]
    assert n == 0


def test_healthy_tick_clears_the_error_ledger(conn):
    """Recovery is a state change too: the next failure must re-alert at once
    (and the backoff ladder restarts)."""
    from controller import alerts, ticker
    alerts.maybe_alert(conn, "tick-error:pump", "tick-error",
                       kind="pump", error="module.Error")
    t = ticker.Ticker(gates_path=None)
    t._guard("pump", conn, lambda c: None)
    assert conn.execute("SELECT COUNT(*) c FROM ctl_alert_state"
                        " WHERE alert_key='tick-error:pump'").fetchone()["c"] == 0
