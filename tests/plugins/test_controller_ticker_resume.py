"""Regression: watchdog resume-interrupted repair (go Вл 13.11).

'Worker activated but never reported' must return to the queue on its own:
the pause basis 'worker interrupted (resume pending)' is the only mechanically
resumable class; preempt/budget/human pauses stay parked. The step is re-armed
BEFORE the task flips to ready (22.10 false-gate lesson).
"""
import os
import time

import pytest


@pytest.fixture()
def db_connect(tmp_path, monkeypatch):
    from controller import model
    real = model.connect
    db = tmp_path / "state.db"

    def connect(db_path=None):
        return real(db)

    monkeypatch.setattr(model, "connect", connect)
    return connect


def _seed_parked(db_connect, *, basis, step_status="paused", with_open_run=False,
                 open_run_pid=None):
    """Task parked via `basis`; returns (conn, ticker, task_id, step_id)."""
    from controller import model, ticker
    conn = db_connect()
    workdir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tid = model.create_task(conn, owner="tester", workdir=workdir, goal="g")
    sid = model.add_step(conn, tid, "implement", "do the work")
    if step_status:
        conn.execute("UPDATE ctl_step SET status=? WHERE step_id=?", (step_status, sid))
    model.set_task_state(conn, tid, "paused")
    conn.execute("INSERT INTO ctl_state_transition(ts,task_id,from_state,to_state,basis)"
                 " VALUES (?,?,?,?,?)",
                 (time.time() - 400, tid, "running", "paused", basis))
    if with_open_run:
        conn.execute("INSERT INTO ctl_run(run_id,task_id,step_id,attempt,worker_id,"
                     "contract_version,execution_epoch,input_artifacts,authority,"
                     "run_status,worker_pid,worker_pgid) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("run-1", tid, sid, 1, None, 1, 0, "[]", "[]",
                      "running", open_run_pid or os.getpid(), None))
    conn.commit()
    t = ticker.Ticker(gates_path=None, deliver_fn=lambda line: None)
    return conn, t, tid, sid


def _events(conn, kind):
    return conn.execute("SELECT * FROM ctl_event WHERE kind=?", (kind,)).fetchall()


def test_watchdog_resumes_interrupted_task(db_connect):
    from controller import model
    conn, t, tid, sid = _seed_parked(
        db_connect, basis="worker interrupted (resume pending)")
    t.run_watchdog(conn)
    assert model.get_task(conn, tid)["state"] == "ready"
    assert conn.execute("SELECT status FROM ctl_step WHERE step_id=?",
                        (sid,)).fetchone()["status"] == "pending"
    assert len(_events(conn, "ticker-resume")) == 1
    row = conn.execute("SELECT basis FROM ctl_state_transition WHERE task_id=?"
                       " AND to_state='ready' ORDER BY ts DESC LIMIT 1",
                       (tid,)).fetchone()
    assert row and row["basis"].startswith("ticker-resume")


def test_preempt_park_stays_paused(db_connect):
    """'out-of-band preempt' is a scheduler decision, not a lost report."""
    from controller import model
    conn, t, tid, _sid = _seed_parked(
        db_connect, basis="out-of-band preempt")
    t.run_watchdog(conn)
    assert model.get_task(conn, tid)["state"] == "paused"
    assert not _events(conn, "ticker-resume")


def test_open_live_run_blocks_resume(db_connect):
    conn, t, tid, _sid = _seed_parked(
        db_connect, basis="worker interrupted (resume pending)",
        with_open_run=True, open_run_pid=os.getpid())
    t.run_watchdog(conn)
    from controller import model
    assert model.get_task(conn, tid)["state"] == "paused"


def test_nothing_dispatchable_stays_parked_and_alerts(db_connect):
    """Re-arm yields no dispatchable step (all done): the watchdog must NOT
    invent a ready task — that is the 22.10 false-gate class."""
    from controller import model
    conn, t, tid, _sid = _seed_parked(
        db_connect, basis="worker interrupted (resume pending)",
        step_status="done")
    t.run_watchdog(conn)
    assert model.get_task(conn, tid)["state"] == "paused"
    assert not _events(conn, "ticker-resume")
    # an alert row exists (stuck condition is visible, not silent)
    assert conn.execute("SELECT 1 FROM ctl_alert_state WHERE alert_key=?",
                        (f"resume-interrupted:{tid}",)).fetchone()


def test_grace_window_protects_fresh_pause(db_connect):
    from controller import model, ticker
    conn, t, tid, _sid = _seed_parked(
        db_connect, basis="worker interrupted (resume pending)")
    conn.execute("UPDATE ctl_state_transition SET ts=? WHERE task_id=?",
                 (time.time(), tid))
    conn.commit()
    t.run_watchdog(conn)
    assert model.get_task(conn, tid)["state"] == "paused"
    assert ticker.RESUME_GRACE_S > 0
