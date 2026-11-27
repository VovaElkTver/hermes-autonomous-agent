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


def managed_scope(conn, cwd: str) -> str | None:
    """The nearest ``ctl_autonomy_permission`` row on *cwd*'s tree (including
    *cwd* itself); None when nothing up the tree is managed at all — the
    closed world: the path itself is the scope and the barrier fails
    'never enabled'. Distinguishing 'row found up here' from 'no row up
    here' is what makes a dispatch-block log readable without re-inference
    (claim rtx5000 stage C, acceptance #5)."""
    path = Path(cwd)
    for candidate in (path, *path.parents):
        row = conn.execute(
            "SELECT 1 FROM ctl_autonomy_permission WHERE scope=? LIMIT 1", (str(candidate),)
        ).fetchone()
        if row:
            return str(candidate)
    return None


def nearest_permission_scope(conn, cwd: str) -> str:
    """Nearest ancestor of *cwd* that has a ``ctl_autonomy_permission`` row
    (including *cwd* itself); *cwd* when nothing up the tree is managed —
    the caller's ``get_permission`` then fails closed with enabled=0."""
    return managed_scope(conn, cwd) or cwd


def resolve_session_scope(conn, task_id: str, default: str) -> str:
    """A session's autonomy scope: its live terminal cwd record (the workdir
    truth — seeded with the child's spawn workdir, refreshed after every
    command) resolved to the nearest managed scope up the tree; when the
    session has no cwd record yet, *default* (the process cwd) resolved the
    SAME way — never returned raw, so the dispatch barrier and the spawn
    preflight (which always resolve via :func:`nearest_permission_scope`)
    name one scope for the same workdir (claim rtx5000 stage C: a lost or
    unwritten seed must not surface 'never enabled' where a managed ancestor
    exists — the barrier degrades to the ancestor's paused state, not to a
    scope row that does not exist)."""
    cwd = None
    try:
        from tools.terminal_tool import get_session_cwd
        cwd = get_session_cwd(task_id)
    except Exception:
        cwd = None
    base = str(Path(cwd).resolve()) if cwd else str(Path(default).resolve())
    try:
        return nearest_permission_scope(conn, base)
    except Exception:
        return base
