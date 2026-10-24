"""controller.reconcile — reconcile orphan runs on controller start.

Reconcile is the anti-orphan rule: a run that was starting/running when the
process went away (gateway restart, pump crash) is re-checked against reality
by verify_fn (artifact scan / git diff / workdir mtime); the run row then
carries the real verdict, never 'running' forever. A run that lands on
'unknown' — we cannot prove what its worker did — must not leave a live
process tree behind: the worker's pid/pgid are recorded on the run row at
spawn, so recover tree-kills it here. Without that, the orphaned worker keeps
writing into the workdir until someone notices (23-24.10: thirteen hours).
"""
from __future__ import annotations

from . import model
from .workers import kill_worker_tree


def recover(conn, *, verify_fn, tree_kill: bool = True) -> list[dict]:
    """Run at controller start (after commands.on_restart). Returns a report per run.

    ``tree_kill=False`` (tests, exotic platforms) skips the kill; the verdict
    semantics are unchanged. Rows without a recorded worker process (pre-migration
    DBs) simply get worker_tree_killed=False."""
    out = []
    rows = conn.execute("SELECT * FROM ctl_run WHERE run_status IN"
                        " ('starting','running','interrupt_requested')").fetchall()
    for r in rows:
        model.set_run_status(conn, r["run_id"], "unknown")
        verdict = verify_fn(dict(r))  # 'finished'|'interrupted'|'partial'|'not_started'
        if verdict in {"finished", "interrupted"}:
            model.set_run_status(conn, r["run_id"], verdict)
            out.append({"run_id": r["run_id"], "action": "reconciled", "verdict": verdict})
        else:
            killed = bool(tree_kill) and kill_worker_tree(r["worker_pid"], r["worker_pgid"])
            out.append({"run_id": r["run_id"], "action": "manual_or_new_attempt",
                        "verdict": verdict, "worker_tree_killed": killed})
    return out
