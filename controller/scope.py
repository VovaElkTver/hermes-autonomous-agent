"""controller.scope — resolve a session's autonomy scope from its live workdir.

The autonomy gate is per-workdir: ``ctl_autonomy_permission`` rows are keyed by
scope path and a scope without a row fails closed (``get_permission`` →
enabled=0, see model.get_permission). A session's live workdir is its terminal
cwd record — ``terminal_tool.record_session_cwd`` seeds it when a delegated
child is spawned and refreshes it after every completed command — so the
session's scope is the NEAREST ANCESTOR that has a permission row. That is
what makes an enabled sub-scope usable while an ancestor scope stays paused:
the gate follows the session's actual position, not the gateway process cwd
(claim rtx5000 delegate-workdir-scope, defect 2).
"""
from __future__ import annotations

from pathlib import Path


def nearest_permission_scope(conn, cwd: str) -> str:
    """Nearest ancestor of *cwd* that has a ``ctl_autonomy_permission`` row
    (including *cwd* itself); *cwd* when no ancestor has a row — the caller's
    ``get_permission`` then fails closed with enabled=0."""
    path = Path(cwd)
    for candidate in (path, *path.parents):
        row = conn.execute(
            "SELECT 1 FROM ctl_autonomy_permission WHERE scope=? LIMIT 1", (str(candidate),)
        ).fetchone()
        if row:
            return str(candidate)
    return str(cwd)


def resolve_session_scope(conn, task_id: str, default: str) -> str:
    """A session's autonomy scope: its live terminal cwd record (the workdir
    truth — seeded with the child's spawn workdir, refreshed after every
    command) resolved to the nearest managed scope up the tree; *default*
    when the session has no cwd record yet."""
    cwd = None
    try:
        from tools.terminal_tool import get_session_cwd
        cwd = get_session_cwd(task_id)
    except Exception:
        cwd = None
    if not cwd:
        return default
    try:
        return nearest_permission_scope(conn, str(Path(cwd).resolve()))
    except Exception:
        return str(Path(cwd).resolve())
