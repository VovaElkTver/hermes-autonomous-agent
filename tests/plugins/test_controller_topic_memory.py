"""Stage003 steps 1–2: topic-memory layer contract tests.

Pins the ToDo stage003 semantics against controller.topic_memory and the
worker spawn env (controller.workers.scope_env):
- the acceptance red thread from Вл's wording: an item written in task A is
  invisible to task B at every read surface (effective_items), a background
  task A import into B carries provenance, global stays read-only;
- supersede only on equal subject AND scope; proposals stay invisible until
  confirmed and never retire a standing rule by being written.
"""
import sqlite3

import pytest


@pytest.fixture()
def conn(tmp_path):
    from controller import model
    c = model.connect(tmp_path / "state.db")
    yield c
    c.close()


def _scopes(conn, tm):
    task_a = tm.ensure_scope(conn, "task", "task-aaa")
    task_b = tm.ensure_scope(conn, "task", "task-bbb")
    project = tm.ensure_scope(conn, "project", "proj-x")
    return task_a, task_b, project


def test_tables_exist_in_canonical_schema(tmp_path):
    """The canonical SCHEMA_SQL really provisions ctl_mem_* on connect."""
    from controller import model
    conn = model.connect(tmp_path / "state.db")
    try:
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'ctl_mem_%'").fetchall()}
        assert {"ctl_mem_scope", "ctl_mem_item", "ctl_mem_binding",
                "ctl_mem_import"} <= names
    finally:
        conn.close()


def test_item_in_task_a_is_invisible_to_task_b(conn):
    """Приёмка: в теме A записали «для этого прототипа Redis запрещён» —
    в теме B этой записи нет ни в выдаче effective_items, ни где-либо ещё."""
    from controller import topic_memory as tm
    task_a, task_b, _ = _scopes(conn, tm)
    tm.add_item(conn, task_a, "constraint", "storage choice",
                "для этого прототипа Redis запрещён", source="user-directive")

    in_a = tm.effective_items(conn, task_a)
    in_b = tm.effective_items(conn, task_b)
    assert len(in_a) == 1 and in_a[0]["subject"] == "storage choice"
    assert in_b == []


def test_chain_task_to_project_to_global_with_global_last(conn):
    from controller import topic_memory as tm
    task_a, _, project = _scopes(conn, tm)
    g = tm.global_scope(conn)
    tm.bind_parent(conn, task_a, project)
    tm.add_item(conn, project, "preference", "naming", "project uses kebab-case",
                source="user-directive")
    tm.add_item(conn, g, "constraint", "no secrets",
                "never put secrets in artifacts", source="user-directive")
    got = tm.effective_items(conn, task_a)
    kinds = [i["from_kind"] for i in got]
    # nearest-first, global LAST, and both visible from inside the task scope
    assert kinds == ["project", "global"]
    assert [i["subject"] for i in got] == ["naming", "no secrets"]
    assert tm.resolve_chain(conn, task_a)[-1]["kind"] == "global"


def test_supersede_on_same_subject_and_scope_only(conn):
    from controller import topic_memory as tm
    task_a, task_b, _ = _scopes(conn, tm)
    first = tm.add_item(conn, task_a, "decision", "api shape", "REST v1",
                        source="user-directive")
    second = tm.add_item(conn, task_a, "decision", "api shape", "REST v2 + ETags",
                         source="user-directive")
    rows = tm.effective_items(conn, task_a)
    assert [r["item_id"] for r in rows] == [second]
    old = conn.execute("SELECT status, superseded_by FROM ctl_mem_item"
                       " WHERE item_id=?", (first,)).fetchone()
    assert old["status"] == "superseded" and old["superseded_by"] == second
    # same subject, different scope: coexists («предмет И область действия»)
    tm.add_item(conn, task_b, "decision", "api shape", "REST v1 (B)",
                source="user-directive")
    assert len(tm.effective_items(conn, task_b)) == 1


def test_proposal_is_not_a_rule_until_confirmed_and_never_hides_the_rule(conn):
    from controller import topic_memory as tm
    task_a, _, _ = _scopes(conn, tm)
    rule = tm.add_item(conn, task_a, "constraint", "dep policy", "pin all deps",
                       source="user-directive")
    proposal = tm.add_item(conn, task_a, "fact", "dep policy", "maybe float deps?",
                           source="hypothesis")
    # unconfirmed: invisible; the standing rule must still be found (ошибка iii дешевле…)
    visible = tm.effective_items(conn, task_a)
    assert [r["item_id"] for r in visible] == [rule]
    tm.confirm(conn, proposal)
    visible = tm.effective_items(conn, task_a)
    assert [r["item_id"] for r in visible] == [proposal]  # now it acts as a rule


def test_import_crosses_topics_with_provenance_and_stays_unconfirmed(conn):
    from controller import topic_memory as tm
    task_a, task_b, _ = _scopes(conn, tm)
    src = tm.add_item(conn, task_a, "fact", "root cause", "race in barrier.check",
                      source="tool-result", run_id="run-x")
    imp = tm.import_item(conn, src, task_b, run_id="run-y")
    # the copy starts UNCONFIRMED in the destination: adoption is a decision there
    raw_b = tm.effective_items(conn, task_b, include_unconfirmed=True)
    assert len(raw_b) == 1
    row_b = raw_b[0]
    assert row_b["subject"] == "root cause" and row_b["confirmed"] == 0
    prov = dict(conn.execute(
        "SELECT * FROM ctl_mem_import WHERE dst_item_id=?", (imp,)).fetchone())
    assert prov["src_item_id"] == src and prov["by_run_id"] == "run-y"
    # the source item is untouched and still active in A
    src_row = dict(conn.execute("SELECT * FROM ctl_mem_item WHERE item_id=?",
                                (src,)).fetchone())
    assert src_row["status"] == "active"


def test_global_write_guard_for_scoped_writers(conn):
    """Global stays readonly for imports (the tool-level guard mirrors this)."""
    from controller import topic_memory as tm
    task_a, _, _ = _scopes(conn, tm)
    item = tm.add_item(conn, task_a, "fact", "s", "b", source="user-directive")
    with pytest.raises(ValueError, match="global"):
        tm.import_item(conn, item, tm.global_scope(conn))
    # direct global writes remain possible for the unscoped (central) path
    gid = tm.ensure_scope(conn, "global", "")
    tm.add_item(conn, gid, "constraint", "g", "policy text", source="user-directive")
    assert len(tm.effective_items(conn, task_a)) >= 1


def test_binding_cycle_refused(conn):
    from controller import topic_memory as tm
    a = tm.ensure_scope(conn, "task", "t-a")
    b = tm.ensure_scope(conn, "task", "t-b")
    tm.bind_parent(conn, b, a)
    with pytest.raises(ValueError, match="cycle"):
        tm.bind_parent(conn, a, b)


def test_worker_scope_env_task_not_global(tmp_path, monkeypatch):
    """Step 2 plumbing: worker env names the TASK scope + project passthrough."""
    from controller import workers as W
    env = W.scope_env({"task_id": "task-zz", "project_id": "proj-q"})
    assert env["HERMES_SCOPE_KIND"] == "task"
    assert env["HERMES_SCOPE_ID"] == "task-zz"
    assert env["HERMES_PROJECT_ID"] == "proj-q"
    empty = W.scope_env({"task_id": "task-zz", "project_id": ""})
    assert empty["HERMES_PROJECT_ID"] == ""
