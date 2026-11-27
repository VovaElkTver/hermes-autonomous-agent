"""delegate_task workdir (claim rtx5000 delegate-workdir-scope, defect 1):
the per-task workdir validation, the schema surface, and the child cwd seed
(explicit workdir beats the inherited parent record; worktree isolation still
wins over both)."""
from types import SimpleNamespace

import pytest

from tools.delegate_tool_tasks import _coerce_task_workdirs


def test_missing_workdir_inherits(tmp_path):
    dirs, err = _coerce_task_workdirs([{"goal": "g1"}, {"goal": "g2", "workdir": None}])
    assert err is None and dirs == [None, None]


def test_valid_workdir_resolves(tmp_path):
    d = (tmp_path / "proj").resolve()
    d.mkdir()
    dirs, err = _coerce_task_workdirs([{"goal": "g", "workdir": str(d)}])
    assert err is None and dirs == [str(d)]


def test_nonexistent_workdir_refused(tmp_path):
    _, err = _coerce_task_workdirs([{"goal": "g", "workdir": str(tmp_path / "nope")}])
    assert err and "not an existing directory" in err


def test_non_directory_workdir_refused(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x")
    _, err = _coerce_task_workdirs([{"goal": "g", "workdir": str(f)}])
    assert err and "not an existing directory" in err


def test_malformed_workdir_refused(tmp_path):
    for bad in ("", "   ", 42, ["x"], 7):
        _, err = _coerce_task_workdirs([{"goal": "g", "workdir": bad}])
        assert err and "non-empty string" in err


def test_schema_advertises_workdir():
    from tools.delegate_tool import DELEGATE_TASK_SCHEMA
    props = DELEGATE_TASK_SCHEMA["parameters"]["properties"]["tasks"]["items"]["properties"]
    assert "workdir" in props and props["workdir"]["type"] == "string"


def _run_factory(workdir, parent_cwd="/parent/cwd"):
    from tools.delegate_tool_child_run import _ChildRun
    child = SimpleNamespace()
    if workdir is not None:
        child._delegate_workdir = workdir
    run = _ChildRun(
        child=child, parent_agent=SimpleNamespace(), task_index=0, goal="g",
        subagent_id="sa-0-test", child_progress_cb=None,
    )
    return run


def test_seed_workspace_prefers_explicit_workdir(monkeypatch, tmp_path):
    import tools.terminal_tool as tt
    import tools.delegate_tool_child_run as dcr
    d = (tmp_path / "proj").resolve()
    d.mkdir()

    calls = []
    monkeypatch.setattr(tt, "record_session_cwd", lambda key, cwd: calls.append((key, cwd)))
    monkeypatch.setattr(tt, "get_session_cwd", lambda key: "/parent/cwd")
    monkeypatch.setattr(tt, "register_container_alias", lambda *a, **k: None)
    monkeypatch.setattr(dcr, "_create_isolated_worktree", lambda *a, **k: None)

    run = _run_factory(str(d))
    run.seed_workspace()
    assert calls == [(run.child_task_id, str(d))]  # explicit workdir, NOT the parent record


def test_seed_workspace_inherits_parent_without_workdir(monkeypatch, tmp_path):
    import tools.terminal_tool as tt
    import tools.delegate_tool_child_run as dcr

    calls = []
    monkeypatch.setattr(tt, "record_session_cwd", lambda key, cwd: calls.append((key, cwd)))
    monkeypatch.setattr(tt, "get_session_cwd", lambda key: "/parent/cwd")
    monkeypatch.setattr(tt, "register_container_alias", lambda *a, **k: None)
    monkeypatch.setattr(dcr, "_create_isolated_worktree", lambda *a, **k: None)

    run = _run_factory(None)
    run.seed_workspace()
    assert calls == [(run.child_task_id, "/parent/cwd")]  # legacy inheritance


def test_worktree_isolation_still_wins(monkeypatch, tmp_path):
    """A worktree (when isolation engages) overrides both the explicit workdir
    and the inherited record — existing behavior preserved."""
    import tools.terminal_tool as tt
    import tools.delegate_tool_child_run as dcr
    d = (tmp_path / "proj").resolve()
    d.mkdir()
    wt = (tmp_path / "wt").resolve()
    wt.mkdir()

    calls = []
    monkeypatch.setattr(tt, "record_session_cwd", lambda key, cwd: calls.append((key, cwd)))
    monkeypatch.setattr(tt, "get_session_cwd", lambda key: "/parent/cwd")
    monkeypatch.setattr(tt, "register_container_alias", lambda *a, **k: None)
    monkeypatch.setattr(dcr, "_create_isolated_worktree",
                        lambda *a, **k: {"path": str(wt), "branch": "b"})

    run = _run_factory(str(d))
    run.seed_workspace()
    assert calls[-1] == (run.child_task_id, str(wt))
    assert run.worktree_info is not None
