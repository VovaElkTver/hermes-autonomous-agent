"""Regression tests for controller.ticker hardening (audit 2026-10-28).

The ticker runs real daemon threads against a temp state.db: ``model.connect``
is patched to one shared file so the pump thread, the watchdog thread and the
scratch connections all see the same rows (raw SCHEMA_SQL path).
"""
import json
import os
import sqlite3
import subprocess
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


def _pump_threads():
    return [th for th in threading.enumerate() if th.name == "controller-ticker"]


@pytest.fixture()
def singleton(db_connect):
    """controller.ticker with the process singleton torn down after the test."""
    from controller import ticker
    yield ticker
    ticker.stop_default()
    ticker._default = None
    ticker._release_ownership()


def test_start_default_double_call_one_thread(singleton):
    t1 = singleton.start_default()
    t2 = singleton.start_default()
    assert t1 is not None
    assert t2 is t1
    assert len(_pump_threads()) == 1


def test_start_default_after_reimport_one_thread(singleton):
    """Hot-reload path: module globals reset, the old thread keeps running —
    only the DB owner row can keep the re-imported start_default honest."""
    import importlib
    t1 = singleton.start_default()
    assert t1 is not None and len(_pump_threads()) == 1
    leaked_fd = singleton._ownership_fd
    mod = importlib.reload(singleton)
    try:
        assert mod.start_default() is None  # owner row: live pid, fresh ts
        assert len(_pump_threads()) == 1    # the reload started nothing
    finally:
        mod._ownership_fd = leaked_fd  # hand the pre-reload flock back
        t1.stop()


def test_start_default_race_one_thread(singleton):
    barrier = threading.Barrier(2)
    results = []

    def go():
        barrier.wait()
        results.append(singleton.start_default())

    ths = [threading.Thread(target=go) for _ in range(2)]
    for th in ths:
        th.start()
    for th in ths:
        th.join(timeout=10)
    assert len(results) == 2 and all(r is not None for r in results)
    assert results[0] is results[1]
    assert len(_pump_threads()) == 1


def test_owner_claim_race_exactly_one_winner(db_connect):
    """The CAS itself: three starters, no row yet — INSERT-then-CAS lets one
    win regardless of thread interleaving."""
    from controller import ticker
    barrier = threading.Barrier(3)
    won = []

    def go(i):
        c = db_connect()  # sqlite thread affinity: one connection per starter
        c.execute("PRAGMA busy_timeout=5000")
        barrier.wait()
        try:
            won.append(ticker._claim_owner(c, f"tok{i}", 60.0) is not None)
        finally:
            c.close()

    ths = [threading.Thread(target=go, args=(i,)) for i in range(3)]
    for th in ths:
        th.start()
    for th in ths:
        th.join(timeout=10)
    assert len(won) == 3
    assert sum(won) == 1


def test_owner_row_gating_rules(db_connect):
    from controller import ticker
    conn = db_connect()
    assert ticker._claim_owner(conn, "t1", 60.0)          # no row -> INSERT wins
    assert ticker._claim_owner(conn, "t2", 60.0) is None  # live fresh owner (us)
    child = subprocess.Popen(["true"])
    child.wait()
    ts = time.time()
    conn.execute("UPDATE ctl_ticker_heartbeat SET ts=?, payload=?"
                 " WHERE kind='owner'",
                 (ts, ticker._owner_payload_str(child.pid, "dead", ts)))
    conn.commit()
    assert ticker._claim_owner(conn, "t3", 60.0)          # dead pid -> take now
    conn.execute("UPDATE ctl_ticker_heartbeat SET ts=?, payload=?"
                 " WHERE kind='owner'",
                 (ts - 121, ticker._owner_payload_str(os.getpid(), "t3", ts)))
    conn.commit()
    assert ticker._claim_owner(conn, "t4", 60.0)          # live but stale -> take
    conn.close()


def test_owner_row_refreshed_each_tick(db_connect):
    from controller import ticker
    conn = db_connect()
    payload = ticker._claim_owner(conn, "tok", 60.0)
    t = ticker.Ticker(gates_path=None)
    t._owner_payload = payload
    time.sleep(0.01)
    t._owner_refresh(conn)
    row = conn.execute("SELECT ts, payload FROM ctl_ticker_heartbeat"
                       " WHERE kind='owner'").fetchone()
    assert row["ts"] > json.loads(payload)["ts"]
    assert row["payload"] == t._owner_payload
    # someone else's row (payload moved on) refreshes nothing of theirs
    t._owner_payload = ticker._owner_payload_str(os.getpid(), "stale-token", 1.0)
    t._owner_refresh(conn)
    assert conn.execute("SELECT payload FROM ctl_ticker_heartbeat"
                        " WHERE kind='owner'").fetchone()["payload"] == row["payload"]
    conn.close()


def test_watchdog_prunes_old_heartbeats_keeps_owner(db_connect):
    """Item 9: a week-old heartbeat history is dropped each watchdog cycle;
    the owner row (CAS token) and fresh rows stay."""
    from controller import ticker
    conn = db_connect()
    old = time.time() - ticker.HEARTBEAT_RETENTION_S - 60
    fresh = time.time()
    for ts, kind in [(old, "pump"), (old, "watchdog"), (old, "ticker-fatal"),
                     (fresh, "pump")]:
        conn.execute("INSERT INTO ctl_ticker_heartbeat(ts, kind, payload)"
                     " VALUES (?,?,?)", (ts, kind, "{}"))
    conn.execute("INSERT INTO ctl_ticker_heartbeat(ts, kind, payload)"
                 " VALUES (?,?,?)", (old, "owner", "{}"))  # ancient owner too
    conn.commit()
    t = ticker.Ticker(gates_path=None, deliver_fn=lambda line: None)
    t.run_watchdog(conn)
    left = {(r["ts"] < fresh - 1, r["kind"]) for r in conn.execute(
        "SELECT ts, kind FROM ctl_ticker_heartbeat").fetchall()}
    assert (True, "owner") in left      # never pruned
    assert (True, "pump") not in left   # old pump gone
    assert (True, "watchdog") not in left and (True, "ticker-fatal") not in left
    assert (False, "pump") in left      # fresh pump kept
    assert (False, "watchdog") in left  # this very tick's heartbeat
    conn.close()


def test_gates_file_written_atomically_reader_degrades(tmp_path, monkeypatch, caplog):
    """Item 10: the snapshot is published by os.replace (a SIGTERM mid-write
    can never leave the cron reader a torn JSON file); a broken file reads as
    degraded None with a log, not an exception."""
    import logging
    from controller import ticker
    moves = []
    path = tmp_path / "run" / "pump-gates.json"
    _real_replace = os.replace
    monkeypatch.setattr(os, "replace",
                        lambda src, dst: (moves.append((src, dst)),
                                          _real_replace(src, dst))[-1])
    t = ticker.Ticker(gates_path=str(path))
    t._write_gates_file({"task-1": "needs_human: x"})
    assert moves == [(str(path) + ".tmp", str(path))]
    snap = json.loads(path.read_text())
    assert snap["gates"]["task-1"] == "needs_human: x"
    assert not (tmp_path / "run" / "pump-gates.json.tmp").exists()
    path.write_text('{"digest": "abc", "gate')  # a torn survivor
    with caplog.at_level(logging.WARNING, logger="controller.ticker"):
        assert ticker.read_gates_file(str(path)) is None
    assert "unreadable" in caplog.text
    assert ticker.read_gates_file(str(tmp_path / "gone.json")) is None


def test_schedule_cadence_survives_frozen_wall_clock(db_connect):
    """Item 11: the cadence runs on the monotonic clock — an NTP step or a
    frozen wall clock can neither skip nor boost ticks — while the DB ts
    columns keep real wall-clock time."""
    from controller import ticker
    t = ticker.Ticker(intervals={"pump": 0.05, "watchdog": 3600},
                      now_fn=lambda: 0.0, gates_path=None)
    calls = []
    t.run_pump = lambda conn: (calls.append(1),
                               t._heartbeat(conn, "pump", "{}"))
    t.start()
    try:
        deadline = time.monotonic() + 3.0
        while len(calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(calls) >= 2  # a wall-clock schedule sticks at tick one
    finally:
        t.stop()
    conn = db_connect()
    ts = conn.execute("SELECT MAX(ts) m FROM ctl_ticker_heartbeat"
                      " WHERE kind='pump'").fetchone()["m"]
    assert ts is not None and ts > 1_000_000  # heartbeat ts = wall clock, not 0.0
    conn.close()


def test_stop_locked_idempotent_restartable(db_connect):
    """Item 12: concurrent stop() calls cannot deadlock (all take the start
    lock), threads are joined and cleared, and start() after stop() works."""
    import threading as th
    from controller import ticker
    t = ticker.Ticker(intervals={"pump": 0.05, "watchdog": 0.2}, gates_path=None)
    t.start()
    first = t._thread
    storm = [th.Thread(target=lambda: t.stop(timeout_s=5)) for _ in range(4)]
    for x in storm:
        x.start()
    for x in storm:
        x.join(15)
        assert not x.is_alive()  # no deadlock on the stop path
    assert not first.is_alive()
    assert t._thread is None and t._watchdog_thread is None
    t.stop()  # idempotent: stopped ticker stops again without raising
    t.start()
    assert t._thread is not None and t._thread is not first
    assert t._thread.is_alive()
    t.stop()
    assert not t.alive()

def test_watchdog_connect_failure_is_loud_not_silent(db_connect, monkeypatch, caplog):
    """Re-check finding 2.1: a model.connect() failure at watchdog startup
    must not be a silent thread death (24.10 in miniature): the thread logs
    the failure loudly and exits clean; the pump thread is unaffected."""
    import logging
    from controller import model, ticker

    t = ticker.Ticker(intervals={"pump": 3600, "watchdog": 0.05},
                      deliver_fn=lambda s: None, gates_path=None)
    calls = {"n": 0}
    real = model.connect

    def flaky(db_path=None):
        calls["n"] += 1
        if calls["n"] == 1:  # the watchdog's own connect is the first one here
            raise sqlite3.OperationalError("database is locked")
        return real(db_path)

    monkeypatch.setattr(model, "connect", flaky)
    with caplog.at_level(logging.ERROR):
        t._start_watchdog()
        t._watchdog_thread.join(5)
    assert not t._watchdog_thread.is_alive(), "watchdog thread hung, not exited"
    assert any("watchdog thread cannot connect" in r.getMessage()
               for r in caplog.records), "silent watchdog death"
    t.stop()
