"""Worker process supervision (incidents 23-24.10):

- ctl_run carries the worker's pid/pgid the moment the spawn returns;
- reconcile.recover tree-kills the live process tree of a run it must mark
  'unknown' (a proven verdict never triggers a kill);
- the pump's single-flight lock takes over STALE holders (a wedged/dead holder
  no longer silences the emitter: no record + live old fd-holder, dead recorded
  holder, or alive-but-silent holder are all broken with a journal row), while
  a live, recent holder is left alone;
- a worker outcome landing on a task that parked mid-turn is honored as
  interrupted, never a transition attempt that kills the pump tick;
- the dispatch-time memory gate defers (not drops, not fails) a worker spawn
  when MemAvailable is below the floor, journaling every tick.
"""
import json
import os
import stat
import subprocess
import sys
import textwrap
import time

import pytest

from controller import loop, model, pump_cli, pump_lock, reconcile
from controller.workers import make_worker


@pytest.fixture()
def env(tmp_path):
    c = model.connect(tmp_path / "c.db")
    wd = tmp_path / "work"
    wd.mkdir()
    model.set_permission(c, str(wd), enabled=True, granted_via="user-command")
    yield c, str(wd)
    c.close()


def _task(c, wd, state="running"):
    tid = model.create_task(c, owner="v", workdir=wd, goal="X")
    model.agree_contract(c, tid)
    sid = model.add_step(c, tid, "impl", "code")
    c.execute("UPDATE ctl_task SET state=? WHERE task_id=?", (state, tid))
    c.commit()
    return tid, sid


def _alive(pid) -> bool:
    if pid is None:
        return False
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError, TypeError):
        return False
    except PermissionError:
        return True
    # A zombie (signaled, not yet reaped by its parent) counts as dead: the kill
    # landed, the test harness just has not called wait() yet.
    try:
        with open(f"/proc/{pid}/stat") as fh:
            state = fh.read().rsplit(")", 1)[1].split()[0]
        if state == "Z":
            return False
    except OSError:
        pass
    return True


def _wait_dead(pid, timeout=10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and _alive(pid):
        time.sleep(0.05)
    return not _alive(pid)


def _lock_json(path):
    with open(path) as fh:
        return json.load(fh)


# --- pid/pgid on the run row (acceptance #1) ---------------------------------


def test_record_run_process_persists_pid_pgid(env):
    c, wd = env
    tid, sid = _task(c, wd)
    rid = model.start_run(c, task_id=tid, step_id=sid, contract_version=1,
                          execution_epoch=1)
    row = c.execute("SELECT worker_pid, worker_pgid FROM ctl_run WHERE run_id=?",
                    (rid,)).fetchone()
    assert row["worker_pid"] is None and row["worker_pgid"] is None
    model.record_run_process(c, rid, pid=1234, pgid=5678)
    row = c.execute("SELECT worker_pid, worker_pgid FROM ctl_run WHERE run_id=?",
                    (rid,)).fetchone()
    assert row["worker_pid"] == 1234 and row["worker_pgid"] == 5678


def test_worker_spawn_records_live_pid_pgid_on_run_row(env, tmp_path):
    """End-to-end: make_worker records the REAL pid/pgid; the worker got its own
    session (its own pgid), which is what a tree-kill targets."""
    c, wd = env
    _tid, sid = _task(c, wd)
    sleeper = tmp_path / "hermes"
    sleeper.write_text("#!/bin/sh\nsleep 0.4\nexit 0\n")
    sleeper.chmod(sleeper.stat().st_mode | stat.S_IEXEC)
    loop.run_cycle(c, scope=wd,
                   worker_fn=make_worker(c, hermes_bin=str(sleeper)),
                   review_fn=lambda *a: {"verdict": "pass", "detail": "ok"})
    row = c.execute("SELECT worker_pid, worker_pgid, run_status"
                    " FROM ctl_run WHERE step_id=?", (sid,)).fetchone()
    assert row["run_status"] == "finished"
    pid, pgid = row["worker_pid"], row["worker_pgid"]
    assert pid is not None and pgid is not None
    assert pid != os.getpid() and pgid != os.getpgrp(), \
        "recorded ids must be the worker's own tree, not the pump's"


# --- recover tree-kills unknown runs (acceptance #2) --------------------------


def _spawn_live_tree():
    """A session leader whose child also sleeps: the grandchild shape that
    'keeps writing into the workdir'."""
    parent = subprocess.Popen(
        [sys.executable, "-c",
         "import subprocess, sys, time;"
         "child = subprocess.Popen([sys.executable, '-c',"
         " 'import time; time.sleep(300)']);"
         "print(child.pid, flush=True); time.sleep(300)"],
        stdout=subprocess.PIPE, text=True, start_new_session=True)
    assert parent.stdout is not None
    child_pid = int(parent.stdout.readline())
    return parent, child_pid


@pytest.mark.platforms("linux")
def test_recover_kills_live_tree_under_unknown_run(env):
    c, wd = env
    tid, sid = _task(c, wd)
    rid = model.start_run(c, task_id=tid, step_id=sid, contract_version=1,
                          execution_epoch=1)
    model.set_run_status(c, rid, "running")
    parent, child = _spawn_live_tree()
    model.record_run_process(c, rid, pid=parent.pid, pgid=os.getpgid(parent.pid))

    rep = reconcile.recover(c, verify_fn=lambda r: "not_started")

    assert rep and rep[0]["run_id"] == rid
    assert rep[0]["action"] == "manual_or_new_attempt"
    assert rep[0]["worker_tree_killed"] is True
    row = c.execute("SELECT run_status FROM ctl_run WHERE run_id=?", (rid,)).fetchone()
    assert row is not None and row["run_status"] == "unknown"
    assert _wait_dead(parent.pid), "unknown-run worker survived recover"
    assert _wait_dead(child), "unknown-run grandchild survived recover"


@pytest.mark.platforms("linux")
def test_recover_keeps_tree_when_verdict_proves_it(env):
    """A verified verdict is proof: the run is reconciled, never tree-killed."""
    c, wd = env
    tid, sid = _task(c, wd)
    rid = model.start_run(c, task_id=tid, step_id=sid, contract_version=1,
                          execution_epoch=1)
    model.set_run_status(c, rid, "running")
    parent, child = _spawn_live_tree()
    model.record_run_process(c, rid, pid=parent.pid, pgid=os.getpgid(parent.pid))

    rep = reconcile.recover(c, verify_fn=lambda r: "finished")

    assert rep[0]["action"] == "reconciled" and rep[0]["verdict"] == "finished"
    row = c.execute("SELECT run_status FROM ctl_run WHERE run_id=?", (rid,)).fetchone()
    assert row is not None and row["run_status"] == "finished"
    assert _alive(parent.pid), "proven-finished run must not be killed"
    from controller.workers import kill_worker_tree
    assert kill_worker_tree(parent.pid, os.getpgid(parent.pid))
    assert _wait_dead(parent.pid) and _wait_dead(child)


def test_recover_without_recorded_process_reports_no_kill(env):
    """Pre-migration rows (no pid on the run) still reconcile; the kill no-ops."""
    c, wd = env
    tid, sid = _task(c, wd)
    rid = model.start_run(c, task_id=tid, step_id=sid, contract_version=1,
                          execution_epoch=1)
    model.set_run_status(c, rid, "running")
    rep = reconcile.recover(c, verify_fn=lambda r: "partial")
    assert rep[0]["run_id"] == rid
    assert rep[0]["action"] == "manual_or_new_attempt"
    assert rep[0]["worker_tree_killed"] is False


# --- parked-task outcome must not kill the pump -------------------------------


def test_worker_outcome_on_parked_task_failed_is_interrupted_not_crash(env):
    """The 23-24.10 crash: operator pause lands between step claim and worker
    return; the failed outcome used to raise 'illegal transition paused ->
    failed' inside the tick, which held the pump lock forever. Now: honored as
    interrupted, task stays paused, the step claim is released for re-dispatch,
    and the run row keeps the worker's real outcome."""
    c, wd = env
    tid, sid = _task(c, wd)

    def worker(run, task, step):
        from controller import transitions
        transitions.transition(c, tid, "paused", basis="operator pause")
        return {"status": "failed", "artifacts": [], "error": "boom"}

    log = loop.run_cycle(c, scope=wd, worker_fn=worker,
                         review_fn=lambda *a: {"verdict": "pass", "detail": "ok"})

    entry = log[-1]
    assert entry["to"] == "paused" and entry["interrupted"] is True
    assert entry["worker_status"] == "failed"
    assert model.get_task(c, tid)["state"] == "paused"
    assert model.get_step(c, sid)["status"] == "pending"
    row = c.execute("SELECT run_status FROM ctl_run WHERE step_id=?", (sid,)).fetchone()
    assert row is not None and row["run_status"] == "failed"
    row = c.execute("SELECT COUNT(*) n FROM ctl_review WHERE step_id=?", (sid,)).fetchone()
    assert row is not None and row["n"] == 0


def test_worker_outcome_on_parked_task_finished_is_interrupted_not_reviewed(env):
    """A 'finished' outcome on a parked task is equally honored without moving
    the task or running a review over parked work."""
    c, wd = env
    tid, sid = _task(c, wd)

    def worker(run, task, step):
        from controller import transitions
        transitions.transition(c, tid, "paused", basis="operator pause")
        return {"status": "finished", "artifacts": ["a"]}

    log = loop.run_cycle(c, scope=wd, worker_fn=worker,
                         review_fn=lambda *a: {"verdict": "pass", "detail": "ok"})

    entry = log[-1]
    assert entry["to"] == "paused" and entry["interrupted"] is True
    assert entry["worker_status"] == "finished"
    assert model.get_task(c, tid)["state"] == "paused"
    assert model.get_step(c, sid)["status"] == "pending"
    row = c.execute("SELECT run_status FROM ctl_run WHERE step_id=?", (sid,)).fetchone()
    assert row is not None and row["run_status"] == "finished"
    row = c.execute("SELECT COUNT(*) n FROM ctl_review WHERE step_id=?", (sid,)).fetchone()
    assert row is not None and row["n"] == 0


def test_unparked_outcome_still_moves_task_to_failed(env):
    """The guard is parked-task-only: an ordinary failed worker still fails the
    task (no behavior change on the happy path)."""
    c, wd = env
    tid, _sid = _task(c, wd)
    log = loop.run_cycle(c, scope=wd,
                         worker_fn=lambda *a: {"status": "failed", "artifacts": []},
                         review_fn=lambda *a: {"verdict": "pass", "detail": "ok"})
    assert any(e.get("to") == "failed" for e in log)
    assert model.get_task(c, tid)["state"] == "failed"


# --- memory-pressure dispatch gate (incident 24.10 09:07) ----------------------

GIB = 1024 * 1024  # KiB


def test_memory_pressure_defers_spawn_and_retries(env, monkeypatch):
    from controller import memory_gate
    c, wd = env
    tid, sid = _task(c, wd)
    monkeypatch.setattr(memory_gate, "system_memory_sample",
                        lambda: {"mem_total_kib": 93 * GIB, "mem_available_kib": 2 * GIB})
    calls = []

    log = loop.run_cycle(c, scope=wd,
                         worker_fn=lambda *a: calls.append(1),
                         review_fn=lambda *a: {"verdict": "pass", "detail": "ok"})

    assert log == [{"stop": "deferred: memory pressure"}]
    assert not calls, "worker must not spawn under pressure"
    row = c.execute("SELECT COUNT(*) n FROM ctl_run").fetchone()
    assert row is not None and row["n"] == 0
    assert model.get_step(c, sid)["status"] == "pending"
    # deferred, not dropped and not failed: the task parks in running, retryable
    assert model.get_task(c, tid)["state"] == "running"

    # pressure clears: the next tick dispatches the same step
    monkeypatch.setattr(memory_gate, "system_memory_sample",
                        lambda: {"mem_total_kib": 93 * GIB, "mem_available_kib": 40 * GIB})
    log = loop.run_cycle(c, scope=wd,
                         worker_fn=lambda *a: {"status": "finished", "artifacts": []},
                         review_fn=lambda *a: {"verdict": "pass", "detail": "ok"})
    assert any(e.get("review") == "pass" for e in log)


def test_memory_gate_journals_on_every_deferred_tick(env, monkeypatch):
    """The journal line is the tick's only signal while deferred: two ticks in a
    row both emit it (a once-only journal would hide a sustained OOM)."""
    from controller import memory_gate
    c, wd = env
    _task(c, wd)
    monkeypatch.setattr(memory_gate, "system_memory_sample",
                        lambda: {"mem_available_kib": 1 * GIB})
    assert loop.run_cycle(c, scope=wd, worker_fn=lambda *a: None,
                          review_fn=lambda *a: None)[-1]["stop"] == "deferred: memory pressure"
    assert loop.run_cycle(c, scope=wd, worker_fn=lambda *a: None,
                          review_fn=lambda *a: None)[-1]["stop"] == "deferred: memory pressure"


def test_memory_gate_fails_open_when_sample_unknown(env, monkeypatch):
    """No /proc (or a bogus sample) must never be the thing that silences the
    queue: the spawn proceeds."""
    from controller import memory_gate
    c, wd = env
    _tid, sid = _task(c, wd)
    monkeypatch.setattr(memory_gate, "system_memory_sample", lambda: dict())
    log = loop.run_cycle(c, scope=wd,
                         worker_fn=lambda *a: {"status": "finished", "artifacts": []},
                         review_fn=lambda *a: {"verdict": "pass", "detail": "ok"})
    assert any(e.get("step") == sid and e.get("review") == "pass" for e in log)


def test_memory_gate_boundary_values():
    from controller import memory_gate
    assert memory_gate.pressure_reason({"mem_available_kib": memory_gate.DEFER_BELOW_KIB}) is None
    assert memory_gate.pressure_reason(
        {"mem_available_kib": memory_gate.DEFER_BELOW_KIB - 1}) == "deferred: memory pressure"
    assert memory_gate.pressure_reason({}) is None
    assert memory_gate.pressure_reason({"mem_available_kib": None}) is None
    assert memory_gate.pressure_reason({"mem_available_kib": "lots"}) is None


# --- pump lock: stale-holder takeover (acceptance #3) --------------------------

HOLD_SCRIPT = textwrap.dedent("""
    import fcntl, sys, time
    fh = open(sys.argv[1], "a+b")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    print("LOCKED", flush=True)
    time.sleep(300)
""")

ORPHAN_SCRIPT = textwrap.dedent("""
    import fcntl, os, subprocess, sys, time
    fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"],
                              pass_fds=(fd,), close_fds=True)
    print(child.pid, flush=True)
    time.sleep(300)
""")


@pytest.fixture()
def holder(tmp_path):
    lock = tmp_path / "pump.lock"
    proc = subprocess.Popen([sys.executable, "-c", HOLD_SCRIPT, str(lock)],
                            stdout=subprocess.PIPE, text=True, start_new_session=True)
    assert proc.stdout is not None
    line = proc.stdout.readline()
    assert "LOCKED" in line, "holder did not acquire the flock"
    yield lock, proc
    if proc.poll() is None:
        proc.kill()
    proc.wait()


@pytest.mark.platforms("linux")
def test_pump_lock_acquired_free_and_released(env, tmp_path):
    c, _ = env
    lock = tmp_path / "free.lock"
    res = pump_lock.acquire(str(lock), conn=c)
    assert res["status"] == "acquired" and res["holder_pid"] == os.getpid()
    assert _lock_json(lock)["pid"] == os.getpid()
    pump_lock.release(res["handle"])
    with open(lock) as fh:
        assert fh.read() == "", "release must erase our record (no false stall)"
    res2 = pump_lock.acquire(str(lock), conn=c)
    assert res2["status"] == "acquired"
    pump_lock.release(res2["handle"])


@pytest.mark.platforms("linux")
def test_pump_lock_live_recent_holder_is_held(env, holder):
    """A live, recently-active holder is a normal concurrent tick: back off,
    never kill."""
    c, _ = env
    lock, proc = holder
    now = time.time()
    with open(lock, "w") as fh:
        json.dump({"pid": proc.pid, "pgid": os.getpgid(proc.pid),
                   "started_at": now - 60, "last_active": now - 60}, fh)
    res = pump_lock.acquire(str(lock), conn=c)
    assert res["status"] == "held" and res["handle"] is None
    assert res["holder_pid"] == proc.pid
    assert _alive(proc.pid), "a healthy holder must never be killed"


@pytest.mark.platforms("linux")
def test_pump_lock_stale_holder_is_killed_and_journaled(env, holder):
    """Acceptance #3: a stall-loker with no activity for > 60 min is killed,
    the lock is taken over, and the takeover is journaled (never silent)."""
    c, _ = env
    lock, proc = holder
    now = time.time()
    with open(lock, "w") as fh:  # what a 2h-silent holder's record looks like
        json.dump({"pid": proc.pid, "pgid": os.getpgid(proc.pid),
                   "started_at": now - 7200, "last_active": now - 7200}, fh)
    res = pump_lock.acquire(str(lock), conn=c)
    assert res["status"] == "taken_over" and res["holder_pid"] == proc.pid
    assert _wait_dead(proc.pid), "stale holder must be tree-killed"
    ev = c.execute("SELECT payload FROM ctl_event WHERE kind='pump_lock_takeover'"
                   ).fetchone()
    assert ev is not None, "takeover must be journaled"
    assert json.loads(ev["payload"])["holder_pid"] == proc.pid
    assert _lock_json(lock)["pid"] == os.getpid(), "the taker stamps its own record"
    pump_lock.release(res["handle"])


@pytest.mark.platforms("linux")
def test_pump_lock_dead_holder_orphaned_fd_is_broken(env, tmp_path):
    """The 13-hour shape: the recorded holder died but a forked child inherited
    the lock fd (the kernel keeps the flock until every description closes).
    The /proc sweep finds the live child and breaks the deadlock."""
    c, _ = env
    lock = tmp_path / "orphan.lock"
    lock.write_text("")
    parent = subprocess.Popen([sys.executable, "-c", ORPHAN_SCRIPT, str(lock)],
                              stdout=subprocess.PIPE, text=True, start_new_session=True)
    assert parent.stdout is not None
    child_pid = int(parent.stdout.readline())
    now = time.time()
    with open(lock, "w") as fh:
        json.dump({"pid": parent.pid, "pgid": parent.pid,
                   "started_at": now - 7200, "last_active": now - 7200}, fh)
    parent.kill()
    parent.wait()
    assert _alive(child_pid), "precondition: orphaned child still holds the fd"

    res = pump_lock.acquire(str(lock), conn=c)

    assert res["status"] == "taken_over"
    assert _wait_dead(child_pid), "orphaned fd-holder must be tree-killed"
    pump_lock.release(res["handle"])


@pytest.mark.platforms("linux")
def test_pump_lock_heartbeat_refreshes_own_record_only(env, tmp_path):
    c, _ = env
    lock = tmp_path / "hb.lock"
    res = pump_lock.acquire(str(lock), conn=c)
    with open(lock, "r+") as fh:  # backdate our own heartbeat
        rec = json.load(fh)
        rec["last_active"] = time.time() - 7200
        fh.seek(0)
        fh.write(json.dumps(rec))
        fh.truncate()
    pump_lock.heartbeat(str(lock))
    assert time.time() - _lock_json(lock)["last_active"] < 5
    with open(lock, "w") as fh:  # a foreign record must never be clobbered
        json.dump({"pid": 99999, "pgid": 99999, "started_at": 1, "last_active": 1}, fh)
    pump_lock.heartbeat(str(lock))
    assert _lock_json(lock)["pid"] == 99999
    pump_lock.release(res["handle"])


@pytest.mark.platforms("linux")
def test_pump_cli_reports_held_lock_and_healthy_holder_survives(env, tmp_path,
                                                                monkeypatch, holder):
    """Wiring: a contended-but-healthy lock makes the tick exit 0 with the
    'lock held' stop line (the cron script keeps it silent), and touches no
    process."""
    c, wd = env
    lock, proc = holder
    monkeypatch.setattr(model, "connect", lambda *a, **k: c)
    monkeypatch.setattr(pump_lock, "default_lock_path", lambda: lock)
    assert pump_cli.main(["--scope", wd]) == 0
    assert _alive(proc.pid), "the healthy holder must survive a contended tick"


@pytest.mark.platforms("linux")
def test_pump_cli_releases_lock_after_drain(env, tmp_path, monkeypatch):
    c, wd = env
    lock = tmp_path / "cli.lock"
    monkeypatch.setattr(model, "connect", lambda *a, **k: c)
    monkeypatch.setattr(pump_lock, "default_lock_path", lambda: lock)
    assert pump_cli.main(["--scope", wd]) == 0
    # main() closes its connection: verify the lock with a fresh handle
    res = pump_lock.acquire(str(lock), conn=None)  # a next tick must not see us holding
    assert res["status"] == "acquired"
    pump_lock.release(res["handle"])
