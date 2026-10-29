"""Takeover-policy tests for controller.pump_lock (audit 2026-10-28 item 4).

The stale-holder kill must never target this process itself, must not fire on
a single sighting, and the stale window must clear the longest observed
marathon turn.
"""
import fcntl
import json
import os
import subprocess
import time


def _contended(tmp_path, rec):
    """Simulate a rival: an external fd holds the flock, the record is stamped."""
    path = tmp_path / "pump-tick.lock"
    fh = open(path, "a+b")
    fcntl.flock(fh, fcntl.LOCK_EX)
    fh.seek(0)
    fh.truncate()
    fh.write(json.dumps(rec).encode())
    fh.flush()
    return str(path), fh


def _rec(pid, last_active):
    return {"pid": pid, "pgid": None, "started_at": last_active,
            "last_active": last_active}


def test_stale_window_clears_the_marathon_turn():
    from controller import pump_lock
    # 27.09 observed marathon turn: 3595.8 s — a 3600 s window was a coin flip.
    assert pump_lock.DEFAULT_STALE_S >= 7200.0


def test_self_pid_holder_is_skipped_never_killed(tmp_path, monkeypatch):
    from controller import model, pump_lock, workers

    kills = []
    monkeypatch.setattr(workers, "kill_worker_tree",
                        lambda pid, pgid=None: kills.append(pid) or True)
    conn = model.connect(tmp_path / "state.db")
    stale = _rec(os.getpid(), time.time() - 9 * 3600)  # ancient stamp, our own pid
    path, holder = _contended(tmp_path, stale)
    try:
        out = pump_lock.acquire(path, stale_s=60, conn=conn)
        assert out["status"] == "held" and "self" in out["detail"]
        assert kills == []  # the guardian did not kill the patient
        assert conn.execute("SELECT COUNT(*) c FROM ctl_alert_state"
                            " WHERE alert_key LIKE"
                            " 'pump-lock-stale:%:self-hold'").fetchone()["c"] == 1
        # even on a repeated sighting a self-held lock never reaches the kill path
        out2 = pump_lock.acquire(path, stale_s=60, conn=conn)
        assert out2["status"] == "held" and "self" in out2["detail"]
        assert kills == []
    finally:
        holder.close()
        conn.close()


def test_kill_lands_only_on_the_second_consecutive_sighting(tmp_path, monkeypatch):
    from controller import model, pump_lock, workers

    kills = []
    holder = {}

    def fake_kill(pid, pgid=None):
        kills.append(pid)
        holder["fh"].close()  # the stall's flock dies with the kill
        return True

    monkeypatch.setattr(workers, "kill_worker_tree", fake_kill)
    conn = model.connect(tmp_path / "state.db")
    victim = subprocess.Popen(["sleep", "30"])  # a live foreign pid
    try:
        stale = _rec(victim.pid, time.time() - 9 * 3600)
        path, holder["fh"] = _contended(tmp_path, stale)
        pump_lock._STALE_SUSPECTS.clear()
        first = pump_lock.acquire(path, stale_s=60, conn=conn)
        assert first["status"] == "held"
        assert "stale-suspect" in first["detail"]
        assert kills == []  # first sighting: alert only
        assert conn.execute("SELECT COUNT(*) c FROM ctl_alert_state"
                            " WHERE alert_key LIKE"
                            " 'pump-lock-stale:%:stale-suspect'").fetchone()["c"] == 1
        second = pump_lock.acquire(path, stale_s=60, conn=conn)
        assert second["status"] == "taken_over"
        assert kills == [victim.pid]  # second consecutive sighting: executed
        pump_lock.release(second["handle"])
    finally:
        holder.get("fh", open(os.devnull, "wb")).close()
        victim.kill()
        victim.wait()
        conn.close()
