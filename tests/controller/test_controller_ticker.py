"""controller.ticker — the resident in-thread tick loop (task-ticker01).

The acceptance rows, in test form:
- gateway-internal thread: the plugin starts it in-process; the interval
  table runs on a re-anchored clock; run_cycle is called in-thread; the
  shared pump lock keeps single-flight against manual CLI / the residual
  cron tick (a contended lock skips, never blocks).
- workers stay tracked children: the pgid registry is thread state, anchored
  on the run row's recorded pid/pgid (adopt), reconciled on death (reap).
- alerts are deterministic template strings deduped in state.db; human-facing
  lines only on state CHANGE.
- shutdown joins the thread.
"""
import fcntl
import json
import os
import subprocess
import threading
import time

import pytest

from controller import alerts, model, pump_lock, ticker as CT, transitions
from controller.ticker import Ticker


@pytest.fixture()
def conn(tmp_path):
    c = model.connect(tmp_path / "c.db")
    yield c
    c.close()


@pytest.fixture()
def wd(tmp_path):
    d = tmp_path / "work"
    d.mkdir()
    return str(d)


@pytest.fixture(autouse=True)
def _no_real_pump_lock(tmp_path, monkeypatch):
    """run_pump takes the shared pump lock: tests must never contend with (or
    take over) the LIVE cron tick's lock, so point it at a tmp file."""
    monkeypatch.setattr(pump_lock, "default_lock_path",
                        lambda: tmp_path / "run" / "pump-tick.lock")
    (tmp_path / "run").mkdir(parents=True, exist_ok=True)


def mk_ready(c, wd, goal="step task"):
    tid = model.create_task(c, owner="v", workdir=wd, goal=goal)
    model.agree_contract(c, tid)
    model.add_step(c, tid, "impl", "do the thing", checkpoint=False)
    model.set_permission(c, wd, enabled=True, granted_via="user-command")
    c.execute("UPDATE ctl_task SET state='ready' WHERE task_id=?", (tid,))
    c.commit()
    return tid


def _pump_heartbeats(c):
    return c.execute("SELECT payload FROM ctl_ticker_heartbeat WHERE kind='pump'").fetchall()


def _no_delivery():
    class _D:
        def __enter__(self):
            self._orig = alerts.deliver
            alerts.deliver = lambda *a, **kw: False
            return self

        def __exit__(self, *exc):
            alerts.deliver = self._orig
    return _D()


# ---- pump tick (in-thread drain) --------------------------------------------------

def test_pump_tick_drains_the_queue_in_thread(conn, wd):
    """run_cycle runs IN the calling thread — no subprocess spawn, no cron —
    and the tick is journaled as a heartbeat."""
    mk_ready(conn, wd)
    seen = []

    def worker(run, task, step):
        seen.append(threading.current_thread().name)
        return {"status": "finished", "artifacts": [{"file": "x"}]}

    def review(run, step, arts):
        return {"verdict": "pass"}

    t = Ticker(now_fn=lambda: 0.0, gates_path=None)
    t._worker_fn = lambda conn: worker
    t._review_fn = lambda conn: review
    t.run_pump(conn)
    assert seen == ["MainThread"]
    assert _pump_heartbeats(conn)
    assert conn.execute("SELECT status FROM ctl_step WHERE task_id="
                        " (SELECT task_id FROM ctl_task WHERE workdir=?)",
                        (wd,)).fetchone()["status"] == "done"


def test_pump_tick_skips_when_the_lock_is_held(conn, wd):
    """A contended lock skips the drain: no double drain, journal line only."""
    mk_ready(conn, wd)
    lock_path = pump_lock.default_lock_path()
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        called = {"n": 0}

        def worker(run, task, step):
            called["n"] += 1
            return {"status": "finished", "artifacts": []}

        t = Ticker(now_fn=lambda: 0.0, gates_path=None)
        t._worker_fn = lambda conn: worker
        t._review_fn = lambda conn: (lambda run, step, arts: {"verdict": "pass"})
        t.run_pump(conn)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert called["n"] == 0
    skipped = [json.loads(r["payload"]) for r in _pump_heartbeats(conn)]
    assert skipped and skipped[0].get("skipped") == "lock held"


def test_pump_tick_respects_permission_off(conn, wd):
    mk_ready(conn, wd)
    model.set_permission(conn, wd, enabled=False, granted_via="user-command")
    called = {"n": 0}

    def worker(run, task, step):
        called["n"] += 1
        return {"status": "finished", "artifacts": []}

    t = Ticker(now_fn=lambda: 0.0, gates_path=None)
    t._worker_fn = lambda conn: worker
    t._review_fn = lambda conn: (lambda run, step, arts: {"verdict": "pass"})
    t.run_pump(conn)
    assert called["n"] == 0  # a paused queue no-ops, tick still journaled
    assert _pump_heartbeats(conn)


# ---- worker pgid registry: adopt + reap --------------------------------------------

def test_registry_adopts_open_runs_and_reaps_on_death(conn, wd):
    """Restart-safe adoption from state.db; a dead tracked worker is
    reconciled: run -> interrupted, step claim released, registry emptied,
    the reap journaled."""
    tid = mk_ready(conn, wd)
    sid = conn.execute("SELECT step_id FROM ctl_step WHERE task_id=?", (tid,)).fetchone()["step_id"]
    run_id = model.start_run(conn, task_id=tid, step_id=sid,
                             contract_version=1, execution_epoch=0)
    model.set_run_status(conn, run_id, "running")
    model.mark_step_in_progress(conn, sid)

    proc = subprocess.Popen(["sleep", "30"])
    try:
        model.record_run_process(conn, run_id, pid=proc.pid, pgid=proc.pid)
        t = Ticker(now_fn=lambda: 0.0, gates_path=None)
        t._adopt_workers(conn)
        assert t.registry()[run_id]["pid"] == proc.pid
        assert t.registry()[run_id]["adopted"]
        t._reap_workers(conn)  # alive: no-op
        assert run_id in t.registry()
    finally:
        proc.kill()
        proc.wait()
    t._reap_workers(conn)
    assert run_id not in t.registry()
    assert conn.execute("SELECT run_status FROM ctl_run WHERE run_id=?",
                        (run_id,)).fetchone()[0] == "interrupted"
    assert conn.execute("SELECT status FROM ctl_step WHERE step_id=?",
                        (sid,)).fetchone()[0] == "pending"
    assert conn.execute("SELECT 1 FROM ctl_event WHERE kind='worker-reaped'"
                        " AND task_id=?", (tid,)).fetchone()


def test_registry_ignores_closed_runs(conn):
    t = Ticker(now_fn=lambda: 0.0, gates_path=None)
    t._adopt_workers(conn)  # empty DB: nothing
    assert t.registry() == {}


# ---- alerts: deterministic templates, state.db dedupe -------------------------------

def test_maybe_alert_fires_once_per_state_change(conn):
    line = alerts.maybe_alert(conn, "k", "machine-park", task_id="task-a", basis="verify: x")
    assert line and "task-a" in line
    assert alerts.maybe_alert(conn, "k", "machine-park", task_id="task-a",
                              basis="verify: x") is None  # identical state: silent
    assert alerts.maybe_alert(conn, "k", "machine-park", task_id="task-b",
                              basis="verify: x") is not None  # changed state: fires


def test_clear_alert_rearms_the_condition(conn):
    alerts.maybe_alert(conn, "k", "machine-park", task_id="task-a", basis="v")
    alerts.clear_alert(conn, "k")
    assert alerts.maybe_alert(conn, "k", "machine-park", task_id="task-a", basis="v")


def test_gate_change_lines_only_on_set_change(conn):
    snap1 = {"task-x": "needs_human: g"}
    snap2 = {"task-x": "needs_human: g",
             "task-y": "contract MATERIAL not agreed (state=ready)"}
    l1 = alerts.gate_change_lines(conn, snap1, None)
    assert l1 and "task-x" in l1[0]
    assert alerts.gate_change_lines(conn, snap1, snap1) == []  # no change: silent
    l2 = alerts.gate_change_lines(conn, snap2, snap1)
    assert l2 and "task-y" in l2[0]
    l3 = alerts.gate_change_lines(conn, {}, snap2)
    assert any("Гейт закрыт" in x for x in l3)


def test_deliver_cli_invokes_hermes_send(monkeypatch):
    argv = {}

    class _P:
        returncode = 0

    def fake_run(args, **kw):
        argv["args"] = args
        return _P()

    monkeypatch.setattr(alerts.subprocess, "run", fake_run)
    assert alerts.deliver("hello", "matrix:!R:H", hermes_bin="hermes-test")
    assert argv["args"] == ["hermes-test", "send", "-t", "matrix:!R:H", "-q", "hello"]
    assert alerts.deliver("hello", "") is False  # no target: records, no chat


# ---- watchdog: pump staleness + machine-park repair ----------------------------------

def test_watchdog_alerts_on_stale_pump(conn):
    now = [1000000.0]
    t = Ticker(now_fn=lambda: now[0], gates_path=None)
    t._heartbeat(conn, "pump", {"scopes": {}})
    now[0] += CT.PUMP_STALE_S + 60
    delivered = []
    t.deliver_fn = delivered.append
    with _no_delivery():
        t.run_watchdog(conn)
    assert delivered and "ЗАМОЛЧАЛ" in delivered[0]
    assert conn.execute("SELECT 1 FROM ctl_event WHERE kind='alert'").fetchone()


def test_watchdog_silent_on_fresh_pump(conn):
    now = [1000000.0]
    t = Ticker(now_fn=lambda: now[0], gates_path=None)
    t._heartbeat(conn, "pump", {"scopes": {}})
    now[0] += 10
    delivered = []
    t.deliver_fn = delivered.append
    with _no_delivery():
        t.run_watchdog(conn)
    assert not delivered


def test_watchdog_requeues_a_machine_park_with_passing_reviews(conn, wd):
    """Watchdog rule 4b (deterministic): verify: basis that is not a revise
    question, all steps done, passing reviews, parked long enough -> ready."""
    tid = mk_ready(conn, wd)
    sid = conn.execute("SELECT step_id FROM ctl_step WHERE task_id=?", (tid,)).fetchone()["step_id"]
    run_id = model.start_run(conn, task_id=tid, step_id=sid,
                             contract_version=1, execution_epoch=0)
    model.set_run_status(conn, run_id, "finished")
    model.finish_step(conn, sid)
    model.add_review(conn, sid, run_id, "h" * 64, "pass", "test", "")
    perm = model.get_permission(conn, wd)
    transitions.transition(conn, tid, "running", basis="test", permission=perm)
    transitions.transition(conn, tid, "verifying", basis="all steps done")
    transitions.transition(conn, tid, "needs_human",
                           basis="verify: missing reviews (stale artifact)")
    conn.execute("UPDATE ctl_state_transition SET ts=? WHERE task_id=? AND to_state='needs_human'",
                 (time.time() - CT.MACHINE_PARK_S - 5, tid))
    conn.commit()
    t = Ticker(now_fn=time.time, gates_path=None)
    with _no_delivery():
        t.run_watchdog(conn)
    assert conn.execute("SELECT state FROM ctl_task WHERE task_id=?",
                        (tid,)).fetchone()[0] == "ready"
    assert conn.execute("SELECT 1 FROM ctl_event WHERE kind='ticker-repair'").fetchone()


def test_watchdog_leaves_human_gates_untouched(conn, wd):
    """A needs_human on a non-verify: basis is a real question: the
    deterministic watchdog must never auto-requeue it."""
    tid = mk_ready(conn, wd)
    sid = conn.execute("SELECT step_id FROM ctl_step WHERE task_id=?", (tid,)).fetchone()["step_id"]
    run_id = model.start_run(conn, task_id=tid, step_id=sid,
                             contract_version=1, execution_epoch=0)
    model.set_run_status(conn, run_id, "finished")
    model.finish_step(conn, sid)
    model.add_review(conn, sid, run_id, "h" * 64, "pass", "test", "")
    perm = model.get_permission(conn, wd)
    transitions.transition(conn, tid, "running", basis="test", permission=perm)
    transitions.transition(conn, tid, "verifying", basis="all steps done")
    transitions.transition(conn, tid, "needs_human", basis="review: merge or fork?")
    conn.execute("UPDATE ctl_state_transition SET ts=? WHERE task_id=? AND to_state='needs_human'",
                 (time.time() - CT.MACHINE_PARK_S - 5, tid))
    conn.commit()
    t = Ticker(now_fn=time.time, gates_path=None)
    with _no_delivery():
        t.run_watchdog(conn)
    assert conn.execute("SELECT state FROM ctl_task WHERE task_id=?",
                        (tid,)).fetchone()[0] == "needs_human"


# ---- epoch re-check per tick ----------------------------------------------------------

def test_epoch_change_is_journalled(conn, wd):
    mk_ready(conn, wd)
    t = Ticker(now_fn=lambda: 0.0, gates_path=None)
    t._check_epochs(conn)
    first = conn.execute("SELECT COUNT(*) c FROM ctl_event"
                         " WHERE kind='ticker-permission-change'").fetchone()["c"]
    assert first >= 1
    row = conn.execute("SELECT execution_epoch FROM ctl_autonomy_permission"
                       " WHERE scope=?", (wd,)).fetchone()
    conn.execute("UPDATE ctl_autonomy_permission SET execution_epoch=? WHERE scope=?",
                 (row["execution_epoch"] + 1, wd))
    conn.commit()
    t._check_epochs(conn)
    second = conn.execute("SELECT COUNT(*) c FROM ctl_event"
                          " WHERE kind='ticker-permission-change'").fetchone()["c"]
    assert second == first + 1  # the changed scope journaled exactly once


# ---- scheduler cadence + clean join ----------------------------------------------------

def test_scheduler_re_anchors_the_cadence(conn):
    """A late pump tick (1s drain) must not collapse the cadence: the next
    tick re-anchors from 'now', the clock keeps its relative shape, and the
    watchdog still fires on its own interval."""
    events = []
    base = time.monotonic()
    extra = [0.0]  # simulated time a late tick consumes

    def now_fn():
        return base + (time.monotonic() - base) + extra[0]

    class FakeTicker(Ticker):
        def run_pump(self, conn):
            events.append(("pump", now_fn()))
            extra[0] += 1.0  # this tick ran late

        def run_watchdog(self, conn):
            events.append(("watchdog", now_fn()))

    t = FakeTicker(intervals={"pump": 2.0, "watchdog": 5.0},
                   now_fn=now_fn, gates_path=None)
    th = threading.Thread(target=t._schedule, args=(conn,), daemon=True)
    th.start()
    time.sleep(4.5)
    t.stop(timeout_s=5)
    th.join(5)
    assert not th.is_alive()
    kinds = [k for k, _ in events]
    assert kinds[0] == "pump"                      # first pump tick is immediate
    assert "watchdog" in kinds                     # the watchdog interval fired
    pumps = [e for e in events if e[0] == "pump"]
    assert len(pumps) >= 2
    assert pumps[1][1] - pumps[0][1] >= 1.0        # re-anchored after the late tick


def test_start_stop_joins_cleanly(tmp_path, monkeypatch):
    import controller.ticker as CT_mod
    monkeypatch.setattr(CT_mod, "_ownership_path",
                        lambda: tmp_path / "run" / "ticker.lock")
    monkeypatch.setattr(CT_mod, "_ownership_fd", None)
    db = tmp_path / "t.db"
    orig_connect = model.connect
    monkeypatch.setattr(CT_mod.model, "connect",
                        lambda *a, **k: orig_connect(db))
    t = CT_mod.Ticker(intervals={"pump": 0.02, "watchdog": 300}, gates_path=None)
    t.start()
    try:
        c = model.connect(db)
        deadline = time.time() + 5
        while time.time() < deadline and not c.execute(
                "SELECT 1 FROM ctl_ticker_heartbeat WHERE kind='ticker-start'").fetchone():
            time.sleep(0.02)
        c.close()
    finally:
        t.stop(timeout_s=5)
    assert not t.alive()
    c = model.connect(db)
    kinds = [r[0] for r in c.execute("SELECT kind FROM ctl_ticker_heartbeat")]
    c.close()
    assert "ticker-start" in kinds and "ticker-stop" in kinds


# ---- process ownership (gateway singleton) --------------------------------------------

def test_ownership_lock_is_single_flight(tmp_path, monkeypatch):
    import controller.ticker as CT_mod
    monkeypatch.setattr(CT_mod, "_ownership_path",
                        lambda: tmp_path / "run" / "ticker.lock")
    monkeypatch.setattr(CT_mod, "_ownership_fd", None)
    lockfile = tmp_path / "run" / "ticker.lock"
    lockfile.parent.mkdir(parents=True, exist_ok=True)
    f = open(str(lockfile), "a+b")
    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
    try:
        assert CT_mod._acquire_ownership() is None  # a live process owns it
    finally:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()
    fd = CT_mod._acquire_ownership()
    assert fd is not None
    CT_mod._release_ownership()
    assert CT_mod._acquire_ownership() is not None  # free again after release
    CT_mod._release_ownership()


# ---- plugin wiring: in-thread start, spawn removed -------------------------------------

def test_plugin_register_starts_the_ticker_and_never_spawns(monkeypatch):
    import plugins.controller as P
    monkeypatch.setattr(P, "_enabled", lambda: True)
    monkeypatch.setattr(P, "_db", lambda: None)

    class FakeCtx:
        def __init__(self):
            self.hooks = {}

        def register_hook(self, name, fn):
            self.hooks.setdefault(name, []).append(fn)

        def register_tool(self, **kw):
            pass

    started = []
    import controller.ticker as CT_mod
    monkeypatch.setattr(CT_mod, "start_default", lambda: started.append(1) or object())
    ctx = FakeCtx()
    P.register(ctx)
    assert "post_llm_call" not in ctx.hooks   # the post-session spawn is gone
    assert started                             # the in-thread ticker started
    assert not hasattr(P, "_maybe_pump")      # the spawn path is deleted
    assert not hasattr(P, "_on_post_llm_call")
