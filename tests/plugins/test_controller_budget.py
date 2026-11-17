"""S1.4 (TЗ thinking-enable): the cost of an action.

The dispatch context carries controller_task_id (worker env HERMES_CONTROLLER_TASK_ID),
so the tool_calls budget is accounted against the RIGHT task; a spent limit is a
visible stop (running -> blocked + ctl_event alert), and the autonomous period is
bounded by a default wall budget."""
import os

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from controller import model
    conn = model.connect(tmp_path / "state.db")
    import plugins.controller as pc
    monkeypatch.setattr(pc, "_enabled", lambda: True)
    monkeypatch.setattr(pc, "_db", lambda: conn)
    monkeypatch.setattr(pc, "_origin_of", lambda task_id, session_id: "autonomous")
    monkeypatch.setattr(pc, "_default_scope", lambda: "/w")
    monkeypatch.setitem(pc._scope_by_session, "s1", "/w")
    yield conn, model
    conn.close()


def _running_task(conn, model, workdir="/w"):
    tid = model.create_task(conn, owner="o", workdir=workdir, goal="g")
    model.agree_contract(conn, tid)
    model.set_permission(conn, workdir, enabled=True, granted_via="user-command")
    conn.execute("UPDATE ctl_task SET state='running' WHERE task_id=?", (tid,))
    conn.commit()
    return tid


def test_post_hook_accounts_from_dispatch_env(env, monkeypatch):
    conn, model = env
    tid = _running_task(conn, model)
    monkeypatch.setenv("HERMES_CONTROLLER_TASK_ID", tid)
    import plugins.controller as pc
    pc._on_post_tool_call(tool_name="terminal", args={}, status="ok")
    row = conn.execute("SELECT used FROM ctl_budget_counter"
                      " WHERE task_id=? AND metric='tool_calls'", (tid,)).fetchone()
    assert row and row["used"] >= 1


def test_post_hook_without_context_is_strict_noop(env, monkeypatch):
    conn, model = env
    tid = _running_task(conn, model)
    monkeypatch.delenv("HERMES_CONTROLLER_TASK_ID", raising=False)
    import plugins.controller as pc
    pc._on_post_tool_call(tool_name="terminal", args={}, status="ok",
                          session_id="s9")  # no scope fallback for accounting
    assert conn.execute("SELECT COUNT(*) c FROM ctl_budget_counter WHERE task_id=?",
                        (tid,)).fetchone()["c"] == 0


def test_pre_hook_spent_budget_blocks_and_parks_task(env, monkeypatch):
    conn, model = env
    tid = _running_task(conn, model)
    model.set_budget(conn, tid, "tool_calls", 3)
    for _ in range(3):
        model.add_budget(conn, tid, "tool_calls", 1)
    monkeypatch.setenv("HERMES_CONTROLLER_TASK_ID", tid)
    import plugins.controller as pc
    out = pc._on_pre_tool_call(tool_name="terminal", args={}, session_id="s1")
    assert out and out["action"] == "block"
    assert "budget" in out["message"]
    assert model.get_task(conn, tid)["state"] == "blocked"
    events = conn.execute(
        "SELECT kind, payload FROM ctl_event WHERE task_id=? AND kind='budget-blocked'",
        (tid,)).fetchall()
    assert len(events) == 1
    # the idempotent duplicate must not re-alert (a second call keeps the task
    # blocked — already blocked — without a second event row)
    out2 = pc._on_pre_tool_call(tool_name="terminal", args={}, session_id="s1")
    assert out2 and out2["action"] == "block"
    assert conn.execute(
        "SELECT COUNT(*) c FROM ctl_event WHERE task_id=? AND kind='budget-blocked'",
        (tid,)).fetchone()["c"] == 1


def test_gates_snapshot_announces_budget_blocked(env):
    conn, model = env
    tid = _running_task(conn, model)
    model.set_budget(conn, tid, "tool_calls", 3)
    model.add_budget(conn, tid, "tool_calls", 3)
    assert model.block_on_budget_exhausted(conn, tid, "tool_calls") is True
    # idempotent: not running anymore -> no second block
    assert model.block_on_budget_exhausted(conn, tid, "tool_calls") is False
    from controller import alerts
    gates = alerts.render_gates_snapshot(conn)
    assert tid in gates and "budget" in gates[tid]


def test_worker_budget_default_rules():
    from controller.ticker import Ticker
    t = Ticker()
    assert t._worker_budget_s({}) == 3600.0
    assert t._worker_budget_s({"worker_budget_default_s": 0}) is None
    assert t._worker_budget_s({"worker_budget_default_s": 1200}) == 1200.0
    assert t._worker_budget_s({"worker_budget_default_s": None}) == 3600.0
