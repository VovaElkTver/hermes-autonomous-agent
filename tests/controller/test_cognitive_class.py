"""S2.1 cognitive class (ТЗ thinking-enable, approved Вл 21.11) — registry and
dispatch behaviour.

Doctrine: 'no model text can approve'. The cognitive class is therefore a
HUMAN-APPROVED constant in controller/barrier.py, and this test is its padlock:
adding a member to COGNITIVE_ACTIONS without a human decision fails right here.
"""
import json
import sqlite3

import pytest

from controller import barrier, model
from hermes_state_common import SCHEMA_SQL


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "state.db")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA_SQL)
    yield c
    c.close()


# ---- the padlock: the registry is frozen by human word, not by convenience ----

def test_registry_is_exactly_the_approved_set():
    assert barrier.COGNITIVE_ACTIONS == frozenset({
        "investigation",   # hypothesis/probe/prediction ledger (sessions toolset)
        "ctl_todo",        # intent notes (never pump-selected)
        "ctl_followup",    # due-date wake rows (ticker delivers)
        "ctl_status",      # read-only queue look (recovery path)
        "ctl_enqueue",     # proposing = drafting, never starting
        "ctl_decision",    # S2.6 wake-turn answer (Вл mandate over S2 block, 21.11)
    }), ("COGNITIVE_ACTIONS changed — extending the cognitive class is a human "
         "decision (Вл), not an agent convenience")


def test_material_mutations_are_not_cognitive():
    for name in ("ctl_step_done", "ctl_block", "terminal", "patch", "write_file",
                 "delegate_task", "memory"):
        assert not barrier.is_cognitive(name), name


# ---- dispatch behaviour: cognitive survives every pause, material does not ----

def test_cognitive_passes_hook_with_autonomy_off(monkeypatch, conn, tmp_path):
    from plugins.controller import _on_pre_tool_call
    monkeypatch.setenv("HERMES_CONTROLLER_TASK_ID", "")
    monkeypatch.setattr("plugins.controller._enabled", lambda: True)
    monkeypatch.setattr("plugins.controller._origin_of",
                        lambda task_id, session_id: "autonomous")
    monkeypatch.setattr("plugins.controller._db", lambda: conn)
    monkeypatch.setattr("plugins.controller._default_scope",
                        lambda: str(tmp_path))
    model.set_permission(conn, str(tmp_path), enabled=False,
                         granted_via="user-command")
    # paused autonomy: a material tool is blocked, thinking is not
    blocked = _on_pre_tool_call(tool_name="terminal", task_id="child-1",
                                session_id="s1")
    assert blocked and blocked["action"] == "block"
    for name in barrier.COGNITIVE_ACTIONS:
        assert _on_pre_tool_call(tool_name=name, task_id="child-1",
                                 session_id="s1") is None, name


def test_interactive_origin_unaffected(conn):
    from plugins.controller import _on_pre_tool_call
    assert _on_pre_tool_call(tool_name="terminal", task_id="user-session",
                             session_id="s1") is None


# ---- S2.2: the tools write their own ledger rows (no row = did not happen) ----

def test_ctl_todo_roundtrip(conn, monkeypatch):
    from plugins.controller import agent_tools
    monkeypatch.setattr(agent_tools, "_conn", lambda: conn)
    add = json.loads(agent_tools._ctl_todo(
        {"op": "add", "text": "сверить M6 после первых S2-задач",
         "deadline_hint": "приёмка S2"}, session_id="sess-A"))
    assert add["ok"] and add["todo_id"].startswith("todo-")
    lst = json.loads(agent_tools._ctl_todo({"op": "list"}, session_id="sess-A"))
    assert lst["ok"] and any(t["todo_id"] == add["todo_id"] for t in lst["todos"])
    drop = json.loads(agent_tools._ctl_todo(
        {"op": "drop", "todo_id": add["todo_id"]}))
    assert drop["ok"] and drop["status"] == "dropped"
    row = conn.execute("SELECT status FROM ctl_todo WHERE todo_id=?",
                       (add["todo_id"],)).fetchone()
    assert row["status"] == "dropped"  # soft delete, audit kept


def test_ctl_followup_roundtrip(conn, monkeypatch):
    from plugins.controller import agent_tools
    monkeypatch.setattr(agent_tools, "_conn", lambda: conn)
    add = json.loads(agent_tools._ctl_followup(
        {"op": "add", "kind": "reminder", "text": "проверить снимок M6",
         "due_at": 4102444800.0}, session_id="sess-B"))
    assert add["ok"] and add["followup_id"].startswith("fup")
    assert add["scope_task"] == "session:sess-B"  # provenance, not None
    lst = json.loads(agent_tools._ctl_followup({"op": "list"}))
    assert lst["count"] == 1
    assert json.loads(agent_tools._ctl_followup(
        {"op": "add", "kind": "reminder", "text": "x"}))["ok"] is False  # no due_at
    assert conn.execute("SELECT created_by FROM ctl_followup").fetchone()[0] \
        == "agent-tool"


def test_todo_and_followup_registered_in_agent_surface():
    from plugins.controller import agent_tools
    names = {n for n, _s, _h in agent_tools.tool_defs()}
    assert {"ctl_todo", "ctl_followup"} <= names
    for name in ("ctl_todo", "ctl_followup"):
        assert name in agent_tools.TOOL_META
        assert not agent_tools.is_read_only(name)  # they write; cognitive class admits them
