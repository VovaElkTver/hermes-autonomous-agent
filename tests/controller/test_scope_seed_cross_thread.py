"""Seed → dispatch-barrier chain across the real thread hops (claim rtx5000
stage C, acceptance #1/#3/#4): the workdir seed is written on the dispatch
thread (the async-delegation pool or the batch runner) and the barrier read
happens on the child's worker thread, which runs under a COPIED context —
the seed must survive that hop; the spawn preflight and the dispatch barrier
must name one scope for the same child; and a lost seed must degrade to the
nearest managed scope, not to the raw process cwd ('never enabled' where a
managed ancestor exists)."""
import contextlib
import contextvars
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from controller import barrier, model, scope as S
import tools.terminal_tool as tt
from tools.delegate_tool_child_run import _ChildRun


@pytest.fixture()
def rtx5000_layout(tmp_path, monkeypatch):
    """The field layout: process cwd WITHOUT a permission row, a managed
    ancestor (paused) and an enabled sub-scope (the fan-out target)."""
    home = tmp_path / "home"
    sub = home / "gepa" / "router-week"
    sub.mkdir(parents=True)
    beta = home / "beta"          # sibling under home, no row of its own
    beta.mkdir()
    conn = model.connect(tmp_path / "state.db")
    model.set_permission(conn, str(home), enabled=False, granted_via="user-command")
    model.set_permission(conn, str(sub), enabled=True, granted_via="user-command")
    monkeypatch.chdir(beta)  # process cwd: no row of its own, managed ancestor above
    yield {"conn": conn, "db": tmp_path / "state.db", "home": str(home), "sub": str(sub), "beta": str(beta)}
    conn.close()


def test_seed_survives_thread_hop_to_barrier(rtx5000_layout, monkeypatch):
    """Acceptance #1 one-to-one with deleg_b4189297: two children with an
    explicit workdir in the enabled sub-scope; each child's worker thread
    (copied context, like ``await_child``'s daemon worker) must resolve the
    child's scope to the sub-scope and pass the barrier — while the process
    cwd itself has no row and the ancestor is paused."""
    conn, sub = rtx5000_layout["conn"], rtx5000_layout["sub"]
    parent = SimpleNamespace(_current_task_id="parent-task")
    tt.record_session_cwd("parent-task", rtx5000_layout["home"])  # the gateway's record

    def spawn_child(index: int) -> str:
        # The dispatch side: seed on THIS thread (the pool/runner thread),
        # exactly as _run_single_child does before await_child.
        child = SimpleNamespace(
            _subagent_id=f"deleg_c-{index}", _delegate_workdir=sub,
        )
        run = _ChildRun(child, parent, index, "run pwd", f"deleg_c-{index}", None)
        run.seed_workspace()
        return run.child_task_id

    results = {}

    def worker(thread_task_id: str):
        # The child's worker thread runs under a COPIED context (exactly the
        # shape of propagate_context_to_thread + executor.submit(
        # contextvars.copy_context().run, ...) — and its OWN DB connection
        # (sqlite thread affinity; the plugin's _db() does the same, per
        # worker thread).
        def work():
            wconn = model.connect(rtx5000_layout["db"])
            try:
                scope = S.resolve_session_scope(wconn, thread_task_id, str(Path.cwd().resolve()))
                verdict = barrier.check(wconn, scope=scope, origin="autonomous")
                results[thread_task_id] = (scope, verdict)
            finally:
                wconn.close()

        return work

    for i in range(2):
        tid = spawn_child(i)
        # The seed was written on THIS thread; capture its context exactly the
        # way propagate_context_to_thread / await_child's submit site do.
        ctx = contextvars.copy_context()
        t = threading.Thread(target=lambda: ctx.run(worker(tid)))
        t.start()
        t.join()

    for i in range(2):
        scope, (ok, why) = results[f"deleg_c-{i}"]
        assert scope == sub, f"child {i}: scope {scope!r} != {sub!r} ({why})"
        assert ok, f"child {i}: barrier blocked: {why}"


def test_spawn_preflight_and_barrier_name_one_scope(rtx5000_layout, monkeypatch):
    """Acceptance #3: for the same child, the spawn gate's scope (its target
    workdir resolved) and the dispatch barrier's scope (the seed resolved)
    are the same scope string."""
    conn, sub = rtx5000_layout["conn"], rtx5000_layout["sub"]
    spawn_scope = S.nearest_permission_scope(conn, str(Path(sub).resolve()))
    parent = SimpleNamespace(_current_task_id="parent-task")
    child = SimpleNamespace(_subagent_id="deleg_c-p", _delegate_workdir=sub)
    run = _ChildRun(child, parent, 0, "run pwd", "deleg_c-p", None)
    run.seed_workspace()
    barrier_scope = S.resolve_session_scope(conn, "deleg_c-p", str(Path.cwd().resolve()))
    assert spawn_scope == barrier_scope == sub


def test_lost_seed_degrades_to_nearest_managed_scope(rtx5000_layout, monkeypatch):
    """Acceptance #4: with NO cwd record for the session, the fallback (the
    process cwd) must resolve to the nearest managed scope up the tree — the
    paused ancestor — instead of the raw cwd, so the barrier reports
    'paused/off' where a managed scope exists, and only 'never enabled'
    when nothing up the tree is managed."""
    conn, home, beta = rtx5000_layout["conn"], rtx5000_layout["home"], rtx5000_layout["beta"]
    # no seed for this task: the process cwd (beta) is the fallback base
    scope = S.resolve_session_scope(conn, "no-seed-task", str(Path.cwd().resolve()))
    assert scope == home  # the managed ancestor, not beta
    ok, why = barrier.check(conn, scope=scope, origin="autonomous")
    assert not ok and "paused/off" in why


def test_lost_seed_unmanaged_tree_stays_never_enabled(tmp_path, monkeypatch):
    """Closed world: no rows anywhere up the tree — the fallback keeps the
    resolved process cwd and the barrier's 'never enabled' reason."""
    conn = model.connect(tmp_path / "c.db")
    try:
        unmanaged = tmp_path / "nowhere"
        unmanaged.mkdir()
        monkeypatch.chdir(unmanaged)
        scope = S.resolve_session_scope(conn, "no-seed-task", str(Path.cwd().resolve()))
        assert scope == str(unmanaged)
        ok, why = barrier.check(conn, scope=scope, origin="autonomous")
        assert not ok and "never enabled" in why
    finally:
        conn.close()
