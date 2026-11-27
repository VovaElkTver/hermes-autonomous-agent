"""Controller plugin live session→scope (claim rtx5000 delegate-workdir-scope):
pre_tool_call resolves the child's scope from its terminal cwd record (nearest
managed scope up the tree), not the gateway process cwd; spawn preflight in
delegate_task refuses children whose target workdir is in a disabled scope."""
import importlib.util
import sys

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Temp controller DB + plugin module under test (isolated import, as
    test_controller_agent_tools.test_barrier_read_only_passthrough does)."""
    from controller import model
    conn = model.connect(tmp_path / "state.db")

    spec = importlib.util.spec_from_file_location(
        "ctl_live_scope_under_test", "plugins/controller/__init__.py",
        submodule_search_locations=["plugins/controller"])
    plug = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = plug
    spec.loader.exec_module(plug)

    monkeypatch.setattr(plug, "_enabled", lambda: True)
    monkeypatch.setattr(plug, "_db", lambda: conn)
    monkeypatch.setattr(plug, "_origin_of", lambda *a, **k: "autonomous")

    home = tmp_path / "home"
    home.mkdir()
    paused_home = str(home)
    enabled_sub = str(home / "gepa" / "router-week")
    (home / "gepa" / "router-week").mkdir(parents=True)
    model.set_permission(conn, paused_home, enabled=False, granted_via="user-command")
    model.set_permission(conn, enabled_sub, enabled=True, granted_via="user-command")
    yield {"conn": conn, "plug": plug, "home": paused_home, "enabled_sub": enabled_sub, "tmp": tmp_path}
    conn.close()


def _set_cwd(monkeypatch, task_id, cwd):
    """Point the child's terminal cwd record at *cwd* (the record the seed
    writes at spawn, refreshed after every command)."""
    import tools.terminal_tool as tt
    monkeypatch.setattr(tt, "_session_cwd", {task_id: cwd})
    monkeypatch.setattr(tt, "_qualify_task_key", lambda k: k)


def test_child_in_enabled_subscope_passes_while_home_paused(env, monkeypatch):
    """The acceptance scenario: /home paused, a child seeded into
    /home/gepa/router-week (enabled) must NOT be blocked by its first tool
    call — the old code resolved the gateway process cwd and blocked it."""
    plug, enabled_sub = env["plug"], env["enabled_sub"]
    _set_cwd(monkeypatch, "sa-child", enabled_sub + "/repo")
    assert plug._on_pre_tool_call("terminal", task_id="sa-child", session_id="chat-sess") is None


def test_child_in_paused_scope_still_blocked(env, monkeypatch):
    plug, home = env["plug"], env["home"]
    _set_cwd(monkeypatch, "sa-child", home + "/tmp")
    blocked = plug._on_pre_tool_call("terminal", task_id="sa-child", session_id="chat-sess")
    assert blocked["action"] == "block"
    assert "paused/off" in blocked["message"]


def test_child_in_unmanaged_dir_gets_actionable_reason(env, monkeypatch):
    plug = env["plug"]
    _set_cwd(monkeypatch, "sa-child", "/tmp/nowhere-managed")
    blocked = plug._on_pre_tool_call("terminal", task_id="sa-child", session_id="chat-sess")
    assert blocked["action"] == "block"
    assert "never enabled" in blocked["message"]


def test_read_only_tools_survive_a_blocked_dispatch(env, monkeypatch):
    """Looking at the queue is the recovery path when autonomy is paused."""
    plug, home = env["plug"], env["home"]
    _set_cwd(monkeypatch, "sa-child", home + "/tmp")
    assert plug._on_pre_tool_call("ctl_status", task_id="sa-child", session_id="chat-sess") is None


def test_explicit_session_pin_beats_live_record(env, monkeypatch):
    plug, home, enabled_sub = env["plug"], env["home"], env["enabled_sub"]
    _set_cwd(monkeypatch, "sa-child", home + "/tmp")  # live record would block
    plug._scope_by_session["pinned"] = enabled_sub
    assert plug._on_pre_tool_call("terminal", task_id="sa-child", session_id="pinned") is None


def test_no_cwd_record_falls_back_to_process_scope(env, monkeypatch):
    """No record + no pin → legacy process-cwd scope (unchanged behavior)."""
    plug = env["plug"]
    _set_cwd(monkeypatch, "sa-child", None)  # no record
    # process cwd has no permission row → closed world → block, legacy reason
    blocked = plug._on_pre_tool_call("terminal", task_id="sa-child", session_id="chat-sess")
    assert blocked is not None and blocked["action"] == "block"


# ── dispatch diagnostics (claim rtx5000 stage C, acceptance #5) ─────────────

def test_diagnostic_warns_when_recorded_cwd_unmanaged(env, monkeypatch, caplog):
    """Recorded cwd with no permission row up its tree: the log must say
    exactly that (not 'seed lost' — the seed is present, the row is not)."""
    import logging
    plug = env["plug"]
    _set_cwd(monkeypatch, "sa-child", "/tmp/nowhere-managed")
    with caplog.at_level(logging.WARNING, logger="controller"):
        plug._on_pre_tool_call("terminal", task_id="sa-child", session_id="chat-sess")
    warns = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("recorded cwd=/tmp/nowhere-managed" in m and "never enabled" in m
               for m in warns), warns


def test_diagnostic_warns_when_seed_lost_and_unmanaged(env, monkeypatch, caplog):
    """No record AND no managed scope above the process cwd: the log must
    name both missing inputs (seed and row) — the rtx5000 stage C shape."""
    import logging
    plug = env["plug"]
    _set_cwd(monkeypatch, "sa-child", None)
    with caplog.at_level(logging.WARNING, logger="controller"):
        plug._on_pre_tool_call("terminal", task_id="sa-child", session_id="chat-sess")
    warns = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("no session cwd record" in m and "seed lost or never written" in m
               for m in warns), warns


def test_diagnostic_infos_when_seed_lost_but_ancestor_managed(env, monkeypatch, caplog):
    """No record, but a managed (paused) ancestor above the process cwd:
    the fallback resolves to that ancestor and the log says so (acceptance #4
    path made visible)."""
    import logging
    plug, home = env["plug"], env["home"]
    _set_cwd(monkeypatch, "sa-child", None)
    monkeypatch.chdir(home)  # process cwd inside the (paused) managed home
    with caplog.at_level(logging.INFO, logger="controller"):
        plug._on_pre_tool_call("terminal", task_id="sa-child", session_id="chat-sess")
    infos = [r.message for r in caplog.records if r.levelno == logging.INFO]
    assert any("no session cwd record" in m and home in m for m in infos), infos


# ── spawn preflight (claim bonus point) ──────────────────────────────────────

def _ctl_flag_off(monkeypatch):
    import gateway.run_inbound as ri
    monkeypatch.setattr(ri, "_ctl_flag_on", lambda: False)


def test_spawn_preflight_refuses_disabled_scope(env, monkeypatch):
    """Two fan-out tasks: one with a workdir inside the enabled scope, one
    inheriting the (paused) home cwd → exactly one is refused at spawn."""
    import tools.delegate_tool as dt
    from tools.delegate_tool_routes import TaskRoute

    monkeypatch.setattr("gateway.run_inbound._ctl_flag_on", lambda: True)
    import tools.terminal_tool as tt
    import controller.model as cm
    monkeypatch.setattr(tt, "_session_cwd", {"parent-task": env["home"]})
    monkeypatch.setattr(tt, "_qualify_task_key", lambda k: k)
    monkeypatch.setattr(cm, "connect", lambda *a, **k: env["conn"])  # the fixture's temp DB

    routes = [
        TaskRoute(creds={"model": "m"}, routing_cfg={}),
        TaskRoute(creds={"model": "m"}, routing_cfg={}),
    ]
    task_workdirs = [env["enabled_sub"], None]  # task 0 explicit, task 1 inherited
    from types import SimpleNamespace
    parent = SimpleNamespace(_current_task_id="parent-task")  # the fixture's cwd record key
    dt._refuse_disabled_autonomy_scopes(routes, task_workdirs, parent_agent=parent)

    assert routes[0].error is None
    assert routes[1].error is not None and "spawn refused" in routes[1].error
    assert env["home"] in routes[1].error


def test_spawn_preflight_noop_when_controller_off(env, monkeypatch):
    import tools.delegate_tool as dt
    from tools.delegate_tool_routes import TaskRoute

    _ctl_flag_off(monkeypatch)
    routes = [TaskRoute(creds={"model": "m"}, routing_cfg={})]
    dt._refuse_disabled_autonomy_scopes(routes, [env["home"]], parent_agent=object())
    assert routes[0].error is None  # controller inactive: legacy behavior


def test_spawn_preflight_fail_close_on_controller_db_failure(env, monkeypatch):
    """Controller active but its state unreadable: every spawn is refused with a
    clear error (the per-call barrier would fail closed for the children anyway)."""
    import tools.delegate_tool as dt
    from tools.delegate_tool_routes import TaskRoute
    import controller.model as cm

    monkeypatch.setattr("gateway.run_inbound._ctl_flag_on", lambda: True)
    monkeypatch.setattr(cm, "connect", lambda *a, **k: (_ for _ in ()).throw(OSError("db gone")))
    routes = [TaskRoute(creds={"model": "m"}, routing_cfg={})]
    dt._refuse_disabled_autonomy_scopes(routes, [env["home"]], parent_agent=object())
    assert routes[0].error is not None and "spawn preflight unavailable" in routes[0].error
