"""Stage003 steps 3–4: memory-tool third target ``task`` + prompt injection.

Contract (ToDo stage003): the third target exists ONLY inside a
controller-managed run (HERMES_SCOPE_* from workers.scope_env); writes land in
ctl_mem_item through the controller-package writers with provenance; the
prompt carries global policy + project/task blocks AFTER the built-in memory
blocks and NOTHING for ordinary sessions. Вл's rule holds at every surface:
«отсутствие указанного scope не является разрешением на глобализацию».
"""
import json

import pytest


@pytest.fixture()
def scoped_home(tmp_path, monkeypatch):
    """Isolated Hermes home + task-scope env of ONE worker process."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_SCOPE_KIND", "task")
    monkeypatch.setenv("HERMES_SCOPE_ID", "task-xyz")
    monkeypatch.setenv("HERMES_PROJECT_ID", "proj-q")
    monkeypatch.setenv("HERMES_CONTROLLER_RUN_ID", "run-1")
    return tmp_path


@pytest.fixture()
def no_scope(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for var in ("HERMES_SCOPE_KIND", "HERMES_SCOPE_ID", "HERMES_PROJECT_ID"):
        monkeypatch.delenv(var, raising=False)


def test_no_env_makes_task_target_inert(no_scope):
    """Without controller env: no availability, no prompt bytes, clear refusal."""
    from tools.topic_memory_bridge import available, render_prompt_block
    assert not available()
    assert render_prompt_block() is None
    result = json.loads(_tool_entry(action="add", target="task", content="x", store=None))
    assert result["success"] is False
    assert "HERMES_SCOPE" in result["error"] or "controller-managed" in result["error"]


def _tool_entry(**kwargs):
    from tools.memory_tool import memory_tool
    return memory_tool(**kwargs)


def test_add_and_render_roundtrip(scoped_home):
    from tools.topic_memory_bridge import available, render_prompt_block
    assert available()
    result = json.loads(_tool_entry(
        target="task", store=None,
        operations=[{"action": "add", "content": "для этого прототипа Redis запрещён",
                     "source": "user-directive"}]))
    assert result["success"] is True, result
    block = render_prompt_block()
    assert block is not None
    assert "Redis запрещён" in block


def test_writes_stay_in_task_scope_and_do_not_leak(scoped_home, monkeypatch):
    from tools.topic_memory_bridge import apply_task_ops, render_prompt_block
    out = apply_task_ops([{"action": "add", "content": "только для темы A: шлюз 10.0.0.9",
                           "source": "user-directive"}])
    assert out["success"], out
    # switch the PROCESS to another task's scope: A's records are invisible in B
    monkeypatch.setenv("HERMES_SCOPE_ID", "task-other")
    assert render_prompt_block() is None  # no items, no leakage into B's prompt
    assert apply_task_ops([{"action": "add", "subject": "s", "content": "c"}])["success"]
    monkeypatch.setenv("HERMES_SCOPE_ID", "task-xyz")
    restored = render_prompt_block()
    assert restored and "шлюз 10.0.0.9" in restored
    # provenance rides with the run id from the env
    from controller import model as ctl_model
    rows = ctl_model.connect().execute(
        "SELECT created_run_id FROM ctl_mem_item WHERE subject='s'").fetchall()
    assert rows[0]["created_run_id"] == "run-1"


def test_proposal_default_needs_confirmation_before_the_block(scoped_home):
    from tools.topic_memory_bridge import apply_task_ops, render_prompt_block
    out = apply_task_ops([{"action": "add", "content": "может быть floating deps?",
                           "kind": "fact"}])
    assert out["success"]
    item_id = out["applied"][0]["item_id"]
    assert item_id.startswith("memitem-")
    # an unconfirmed proposal must NOT act as a rule in any prompt… (note shows the channel)
    hidden = render_prompt_block()
    assert hidden is not None and "+1 unconfirmed" in hidden
    assert "floating deps" not in hidden
    # …and confirm() is the deferred half of supersede (controller writers own it)
    from controller import model as ctl_model, topic_memory as tm
    conn = ctl_model.connect()
    try:
        tm.confirm(conn, item_id)
    finally:
        conn.close()
    assert "floating deps" in (render_prompt_block() or "")


def test_batch_patch_and_remove_map_to_supersede(scoped_home):
    from controller import model as ctl_model
    from tools.topic_memory_bridge import apply_task_ops
    out = apply_task_ops([
        {"action": "add", "subject": "api shape", "content": "v1", "source": "user-directive"},
        {"action": "patch", "subject": "api shape", "old_text": "v1", "content": "v2",
         "source": "user-directive"},
    ])
    assert [a["action"] for a in out["applied"]] == ["add", "patch"], out
    rm = apply_task_ops([{"action": "remove", "subject": "api shape"}])
    assert rm["applied"][0]["retired"] is True
    statuses = {r["status"] for r in ctl_model.connect().execute(
        "SELECT status FROM ctl_mem_item")}
    assert "active" not in statuses


def test_schema_overrides_gain_task_only_when_scoped(no_scope, monkeypatch):
    from tools import memory_tool as mt
    mt._memory_surface_flags.set((True, True))
    base = mt._build_memory_schema_overrides()
    assert "task" not in base["parameters"]["properties"]["target"]["enum"]
    # flip to scoped process
    monkeypatch.setenv("HERMES_SCOPE_KIND", "task")
    monkeypatch.setenv("HERMES_SCOPE_ID", "task-zz")
    monkeypatch.delenv("HERMES_PROJECT_ID", raising=False)
    mt._memory_surface_flags.set((True, True))
    extended = mt._build_memory_schema_overrides()
    assert extended["parameters"]["properties"]["target"]["enum"] == ["memory", "user", "task"]
    assert "TARGET 'task'" in extended["description"]


def test_restrict_globals_flag_gates_scoped_worker_globals(scoped_home, monkeypatch):
    """memory.task_restrict_globals=True → scoped worker's builtin stores read-only;
    default stays write-enabled during shadow mode."""
    from tools.topic_memory_bridge import restrict_globals_for_scoped_workers
    assert restrict_globals_for_scoped_workers({"memory": {}}) is False
    from tools.memory_tool import _memory_target_error
    store = type("S", (), {"target_enabled": staticmethod(lambda t: True)})()

    monkeypatch.setattr("tools.topic_memory_bridge.restrict_globals_for_scoped_workers",
                        lambda *a, **k: True)
    blocked = _memory_target_error(store, "memory", None)
    assert blocked is not None and "read-only policy" in blocked["error"]
    allowed = _memory_target_error(store, "task", None)
    assert allowed is None


def test_system_prompt_part_equals_bridge_render(scoped_home):
    from tools.topic_memory_bridge import apply_task_ops, render_prompt_block
    apply_task_ops([{"action": "add", "content": "seed fact", "source": "user-directive"}])
    from agent.system_prompt import _topic_memory_part
    part = _topic_memory_part()
    assert part is not None
    assert part == render_prompt_block()
    assert "## Topic memory" in part


def test_task_goal_topic_from_ctl_task(scoped_home):
    """Step 5: focus topic of a managed run = its ctl_task.goal, not chat noise."""
    from controller import model as ctl_model
    conn = ctl_model.connect()
    try:
        conn.execute(
            "INSERT INTO ctl_task VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("task-xyz", "owner", "proj-q", "/tmp/wd", "починить шлюз; Redis не использовать",
             "draft", 1, None, 0.0, 0.0))
        conn.commit()
    finally:
        conn.close()
    from tools.topic_memory_bridge import task_goal_topic
    got = task_goal_topic()
    assert got == "Managed task goal: починить шлюз; Redis не использовать"


def test_task_goal_topic_none_without_scope(no_scope):
    from tools.topic_memory_bridge import task_goal_topic
    assert task_goal_topic() is None
