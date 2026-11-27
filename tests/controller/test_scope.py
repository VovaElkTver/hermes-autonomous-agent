"""controller.scope — live session→scope resolution (claim rtx5000
delegate-workdir-scope, defect 2): the session's terminal cwd record, resolved
to the nearest managed scope up the tree; closed-world for unmanaged paths."""
from controller import barrier, model, scope as S


def test_nearest_permission_scope_walks_up(tmp_path):
    conn = model.connect(tmp_path / "c.db")
    try:
        root = str(tmp_path / "proj")
        model.set_permission(conn, root, enabled=False, granted_via="user-command")
        sub = root + "/router-week"
        model.set_permission(conn, sub, enabled=True, granted_via="user-command")
        # cwd inside the enabled sub-scope's subtree → the sub row, not the paused root
        assert S.nearest_permission_scope(conn, sub + "/deep/dir") == sub
        # cwd in the sub-scope itself → itself
        assert S.nearest_permission_scope(conn, sub) == sub
        # cwd in the root's own (paused) row → the root row
        assert S.nearest_permission_scope(conn, root) == root
        # a sibling without any row up the tree → its own path (fail-closed below)
        assert S.nearest_permission_scope(conn, str(tmp_path / "other")) == str(tmp_path / "other")
    finally:
        conn.close()


def test_nearest_permission_scope_no_rows(tmp_path):
    conn = model.connect(tmp_path / "c.db")
    try:
        p = str(tmp_path / "unmanaged" / "a")
        assert S.nearest_permission_scope(conn, p) == p
    finally:
        conn.close()


def test_managed_scope_distinguishes_row_from_closed_world(tmp_path):
    """Acceptance #5 helper: 'a managed scope exists up here' (returns the
    row) vs 'nothing managed up the tree' (None) — the two log lines a
    dispatch-block must not conflate."""
    conn = model.connect(tmp_path / "c.db")
    try:
        root = str(tmp_path / "proj")
        model.set_permission(conn, root, enabled=False, granted_via="user-command")
        assert S.managed_scope(conn, root + "/a/b") == root
        assert S.managed_scope(conn, str(tmp_path / "elsewhere")) is None
    finally:
        conn.close()


def test_resolve_session_scope_uses_live_cwd_record(tmp_path, monkeypatch):
    import tools.terminal_tool as tt
    conn = model.connect(tmp_path / "c.db")
    try:
        root = str(tmp_path / "home")
        enabled = root + "/proj"
        model.set_permission(conn, root, enabled=False, granted_via="user-command")
        model.set_permission(conn, enabled, enabled=True, granted_via="user-command")
        monkeypatch.setattr(tt, "_session_cwd", {"sa-child": enabled + "/src"})
        monkeypatch.setattr(tt, "_qualify_task_key", lambda k: k)
        # a recorded cwd inside a managed subtree → the managed scope row
        assert S.resolve_session_scope(conn, "sa-child", "process-cwd") == enabled
        # no record yet → the caller's default, resolved to the nearest managed
        # scope up its tree (here: nothing managed above the pytest cwd, so the
        # resolved default itself — scopes are permission-table keys, never a
        # raw relative string)
        from pathlib import Path
        assert S.resolve_session_scope(conn, "sa-other", "process-cwd") == str(Path("process-cwd").resolve())
    finally:
        conn.close()


def test_resolve_session_scope_closes_world(tmp_path, monkeypatch):
    import tools.terminal_tool as tt
    conn = model.connect(tmp_path / "c.db")
    try:
        monkeypatch.setattr(tt, "_session_cwd", {"sa-x": "/tmp/unmanaged"})
        monkeypatch.setattr(tt, "_qualify_task_key", lambda k: k)
        # no row anywhere up the tree: the path itself is the scope; the barrier
        # then fails closed with the 'never enabled' reason
        assert S.resolve_session_scope(conn, "sa-x", "default") == "/tmp/unmanaged"
        ok, why = barrier.check(conn, scope="/tmp/unmanaged", origin="autonomous")
        assert not ok and "never enabled" in why
    finally:
        conn.close()


def test_barrier_distinguishes_paused_from_never_enabled(tmp_path):
    conn = model.connect(tmp_path / "c.db")
    try:
        paused = str(tmp_path / "paused")
        model.set_permission(conn, paused, enabled=False, granted_via="user-command")
        ok, why = barrier.check(conn, scope=paused, origin="autonomous")
        assert not ok and "paused/off" in why
        ok, why = barrier.check(conn, scope=str(tmp_path / "unknown"), origin="autonomous")
        assert not ok and "never enabled" in why
    finally:
        conn.close()


def test_enabled_subscope_passes_while_ancestor_paused(tmp_path):
    """The field scenario (claim rtx5000): parent scope paused, an enabled
    sub-scope's child must pass the dispatch barrier — the whole point of
    per-workdir scoping."""
    conn = model.connect(tmp_path / "c.db")
    try:
        root = str(tmp_path / "home")
        sub = root + "/gepa/router-week"
        model.set_permission(conn, root, enabled=False, granted_via="user-command")
        model.set_permission(conn, sub, enabled=True, granted_via="user-command")
        ok, why = barrier.check(conn, scope=sub, origin="autonomous")
        assert ok, why
    finally:
        conn.close()
