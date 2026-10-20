"""To-do layer (ctl_todo) — intent-notes over the queue, spec chapter 11.

Architect decision 20.10: the queue (ctl_task/contract/steps/lease) carries
obligations; the to-do layer is a reminder ("не забыть, что придётся заняться")
with NO contract. The pump never selects from ctl_todo — materialization into a
draft task is one human-triggered operation (controller.model.todo_materialize).
Privacy: the text column is the owner's; manager/status views surface
owner/status/deadline_hint only.
"""
import inspect

import pytest

from controller import model, queue as Q


@pytest.fixture()
def env(tmp_path):
    c = model.connect(tmp_path / "c.db")
    wd = tmp_path / "work"
    wd.mkdir()
    yield c, str(wd)
    c.close()


# ---- the acceptance test: the pump's claim path is blind to ctl_todo ----------

def test_pump_claim_does_not_see_ctl_todo(env):
    """pump selection/claim (Q.eligible + Q.next_step + the step CAS claim) reads
    ONLY ctl_task/ctl_step — ctl_todo rows must be invisible to that path,
    by construction (separate table, no shared query) and by behavior."""
    c, wd = env
    todo_id = model.todo_add(c, owner="o", text="remember X", deadline_hint="2026-11-01")
    assert todo_id.startswith("todo-")
    # Construction guard: the selection SQL must not reference ctl_todo anywhere.
    sel_src = "".join(inspect.getsource(fn) for fn in (Q.eligible, Q.next_step, Q.has_open_run))
    assert "ctl_todo" not in sel_src
    # Behavior: a todo-only DB yields an empty queue, not a phantom candidate.
    assert Q.eligible(c, wd) == []
    assert Q.next_step(c, "task-nope") is None
    # Behavior: a real ready task is selected exactly as without todos —
    # the todo row changes neither membership nor order.
    tid = model.create_task(c, owner="o", workdir=wd, goal="g")
    c.execute("UPDATE ctl_task SET state='ready' WHERE task_id=?", (tid,))
    c.commit()
    model.agree_contract(c, tid)
    model.add_step(c, tid, "impl", "do")
    assert [t["task_id"] for t in Q.eligible(c, wd)] == [tid]
    step = Q.next_step(c, tid)
    assert step and step["step_id"]
    # The claim itself (pending -> in_progress CAS) only ever touches ctl_step.
    claim = model.mark_step_in_progress(c, step["step_id"])
    assert claim["ok"] and claim["stored"] == "in_progress"
    assert Q.next_step(c, tid) is None  # the claimed step is gone from dispatch


# ---- lifecycle ----------------------------------------------------------------

def test_todo_add_list_materialize_flow(env):
    """add -> list -> materialize -> draft task in the queue (acceptance demo)."""
    c, wd = env
    todo_id = model.todo_add(
        c, owner="o", text="internal text", origin_ref="task-abc",
        deadline_hint="2026-11-01",
    )
    # manager view: owner/status/deadline, NO internal text
    rows = model.todo_list(c, owner="o")
    assert len(rows) == 1
    row = rows[0]
    assert row["todo_id"] == todo_id and row["status"] == "open"
    assert row["deadline_hint"] == "2026-11-01"
    assert "text" not in row and "origin_ref" not in row
    # owner's own view: full record
    full = model.todo_list(c, owner="o", include_text=True)
    assert full[0]["text"] == "internal text" and full[0]["origin_ref"] == "task-abc"
    # materialize: one operation -> draft task + contract, todo back-linked
    task_id = model.todo_materialize(c, todo_id, wd)
    assert task_id.startswith("task-")
    task = model.get_task(c, task_id)
    assert task["state"] == "draft" and f"todo {todo_id}" in task["goal"]
    assert "internal text" in task["goal"]
    assert model.current_contract(c, task_id)["goal"] == task["goal"]
    todo = model.todo_get(c, todo_id)
    assert todo["status"] == "materialized" and todo["materialized_task_id"] == task_id
    # idempotent: materializing again returns the same task, no second task
    assert model.todo_materialize(c, todo_id, wd) == task_id
    n = c.execute("SELECT COUNT(*) n FROM ctl_task").fetchone()["n"]
    assert n == 1
    # a materialized todo cannot be dropped (cancel the task instead)
    with pytest.raises(ValueError):
        model.todo_drop(c, todo_id)


def test_todo_materialize_unknown_and_dropped(env):
    c, wd = env
    with pytest.raises(LookupError):
        model.todo_materialize(c, "todo-nope", wd)
    d = model.todo_add(c, owner="o", text="x")
    model.todo_drop(c, d)
    with pytest.raises(ValueError):
        model.todo_materialize(c, d, wd)


def test_todo_drop_soft_deletes(env):
    c, _wd = env
    todo_id = model.todo_add(c, owner="o", text="x")
    model.todo_drop(c, todo_id)
    assert model.todo_get(c, todo_id)["status"] == "dropped"  # kept for audit
    model.todo_drop(c, todo_id)  # idempotent on already-dropped
    with pytest.raises(LookupError):
        model.todo_drop(c, "todo-nope")


def test_status_view_includes_open_todos_without_text(env):
    """Status reports surface owner/status/deadline of open todos, never the text."""
    c, wd = env
    from controller import commands
    tid = model.create_task(c, owner="o", workdir=wd, goal="g")
    c.execute("UPDATE ctl_task SET state='ready' WHERE task_id=?", (tid,))
    c.commit()
    model.agree_contract(c, tid)
    secret = model.todo_add(c, owner="o", text="top secret text",
                            deadline_hint="2026-11-01")
    result = commands.apply(c, {"op": "status", "arg": None, "text": ""}, scope=wd)
    assert result["ok"] and len(result["todos"]) == 1
    todo_view = result["todos"][0]
    assert todo_view == {
        "todo_id": secret, "owner": "o", "status": "open", "deadline_hint": "2026-11-01"
    }
    assert "text" not in todo_view and "top secret text" not in str(todo_view)
