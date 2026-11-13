"""controller.reconcile — post-restart / post-crash truth check (spec §43, acceptance rows
'процесс завершился, но результат не записан' and 'после паузы старое run_finished').

Blind retry is forbidden: unfinished runs become 'unknown' first; the caller must pass a
verify_fn that inspects actual side effects (audit log, git state, artifact hashes) before
any attempt>1 is allowed.

Liveness first (incident 02.11 20:49): EVERY process that registers this plugin calls
recover() — periodic ticks, CLI boots, the worker itself. Without a pid check that stamped a
live external worker's run 'unknown' seconds after spawn. An open run whose recorded
worker_pid is not provably dead is left alone; liveness is fail-closed ('unknown' counts
as alive, same contract as pump_lock._pid_liveness).
"""
from __future__ import annotations

import time

from . import model, pump_lock

#: The worker's session row and the run row are born in the same second; 5 s
#: absorbs interpreter startup. The join is an observability aid, never a
#: correctness claim: no match reports session=None rather than a guess.
SESSION_MATCH_S = 5.0

OPEN_RUN_WHERE = "run_status IN ('starting','running','interrupt_requested','unknown')"


def recover(conn, *, verify_fn) -> list[dict]:
    """Run at controller start (after commands.on_restart). Returns a report per run."""
    out = []
    rows = conn.execute("SELECT * FROM ctl_run WHERE run_status IN"
                        " ('starting','running','interrupt_requested')").fetchall()
    for r in rows:
        cols = set(r.keys())
        pid = r["worker_pid"] if "worker_pid" in cols else None
        if pid is not None and pump_lock._pid_liveness(pid) != "dead":
            out.append({"run_id": r["run_id"], "action": "left_alive", "pid": pid})
            continue
        model.set_run_status(conn, r["run_id"], "unknown")
        verdict = verify_fn(dict(r))            # 'finished'|'interrupted'|'partial'|'not_started'
        if verdict in {"finished", "interrupted"}:
            model.set_run_status(conn, r["run_id"], verdict)
            out.append({"run_id": r["run_id"], "action": "reconciled", "verdict": verdict})
        else:
            out.append({"run_id": r["run_id"], "action": "manual_or_new_attempt",
                        "verdict": verdict})   # new attempt only with basis recorded
    return out


def stale_event(conn, *, task_id: str, run_id: str, perm: dict) -> bool:
    """True when an incoming run_finished belongs to a dead epoch or a task that already
    moved on — the result may be stored, but MUST NOT advance the queue (spec §24)."""
    row = conn.execute("SELECT execution_epoch FROM ctl_run WHERE run_id=?", (run_id,)).fetchone()
    if row is None or row["execution_epoch"] != perm["execution_epoch"]:
        return True
    task = model.get_task(conn, task_id)
    return bool(task and task["state"] not in {"running"})


def run_progress(conn, run_id: str | None = None) -> list[dict]:
    """Real ticks for every open run — what 'unknown' never told.

    Ticks come from the worker's own session row (same state.db): api_call_count
    is LLM turns, plus tool calls / messages / last activity. The run<->session
    join is (cwd, started_at within SESSION_MATCH_S) — reported, not trusted:
    no match yields session=None.
    """
    args: tuple = ()
    sql = ("SELECT r.run_id, r.task_id, r.run_status, r.worker_pid, r.started_at,"
           " t.workdir FROM ctl_run r LEFT JOIN ctl_task t ON t.task_id = r.task_id"
           " WHERE r." + OPEN_RUN_WHERE)
    if run_id:
        sql += " AND r.run_id = ?"
        args = (run_id,)
    now = time.time()
    out = []
    for r in conn.execute(sql, args).fetchall():
        pid = r["worker_pid"]
        live = pump_lock._pid_liveness(pid) if pid is not None else "no-pid"
        s = None
        if r["workdir"] is not None:
            try:
                s = conn.execute(
                    "SELECT id, api_call_count, tool_call_count, message_count,"
                    " last_activity_at FROM sessions"
                    " WHERE cwd = ? AND started_at >= ? AND started_at <= ?"
                    " ORDER BY ABS(started_at - ?) LIMIT 1",
                    (r["workdir"], r["started_at"] - SESSION_MATCH_S,
                     now, r["started_at"])).fetchone()
            except Exception:      # sessions missing/locked: progress degrades, never crashes
                s = None
        out.append({
            "run_id": r["run_id"], "task_id": r["task_id"],
            "run_status": r["run_status"], "worker": live, "pid": pid,
            "elapsed_s": round(now - r["started_at"], 1),
            "session": s["id"] if s else None,
            "ticks": s["api_call_count"] if s else None,
            "tool_calls": s["tool_call_count"] if s else None,
            "messages": s["message_count"] if s else None,
            "last_activity": s["last_activity_at"] if s else None,
        })
    return out
