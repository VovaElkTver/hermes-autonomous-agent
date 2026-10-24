"""controller.pump_cli — one-shot queue pump in a detached process.

Run: python -m controller.pump_cli [--scope DIR] (default: every workdir that has
queued tasks). Invoked by the controller plugin after operator sessions (and safe
to cron). One worker at a time per scope by construction: the loop processes
ready->running claims serially and the subprocess dies when the queue empties.

To-do layer subcommand:
  python -m controller.pump_cli todo add --owner OWNER --text TEXT
  python -m controller.pump_cli todo list [--owner O] [--status S] [--full]
  python -m controller.pump_cli todo materialize TODO_ID --workdir DIR
  python -m controller.pump_cli todo drop TODO_ID
"""
from __future__ import annotations

import argparse
import json
import sys


def _todo_cli(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="controller.pump_cli todo")
    sub = ap.add_subparsers(dest="todo_cmd", required=True)

    # add
    p_add = sub.add_parser("add", help="Create a to-do intent-note")
    p_add.add_argument("--owner", required=True)
    p_add.add_argument("--text", required=True)
    p_add.add_argument("--origin-ref", default=None)
    p_add.add_argument("--deadline-hint", default=None)

    # list
    p_list = sub.add_parser("list", help="List to-do entries")
    p_list.add_argument("--owner", default=None)
    p_list.add_argument("--status", default=None, choices=["open", "materialized", "dropped"])
    p_list.add_argument("--full", action="store_true",
                        help="Include internal text (owner-only view)")

    # materialize
    p_mat = sub.add_parser("materialize", help="Promote a to-do into a draft task")
    p_mat.add_argument("todo_id")
    p_mat.add_argument("--workdir", required=True)
    p_mat.add_argument("--owner", default=None)

    # drop
    p_drop = sub.add_parser("drop", help="Mark a to-do as dropped")
    p_drop.add_argument("todo_id")

    a = ap.parse_args(argv)

    from controller import model

    conn = model.connect()
    try:
        handler = _TODO_HANDLERS[a.todo_cmd](conn, a)
    except (LookupError, ValueError) as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 1
    finally:
        conn.close()
    print(json.dumps(handler, ensure_ascii=False))
    return 0


def _todo_add(conn, a) -> dict:
    from controller import model
    return {"ok": True, "todo_id": model.todo_add(
        conn, owner=a.owner, text=a.text,
        origin_ref=a.origin_ref, deadline_hint=a.deadline_hint,
    )}


def _todo_list(conn, a) -> dict:
    from controller import model
    return {"ok": True, "todos": model.todo_list(
        conn, owner=a.owner, status=a.status, include_text=a.full,
    )}


def _todo_materialize(conn, a) -> dict:
    from controller import model
    return {"ok": True, "todo_id": a.todo_id,
            "task_id": model.todo_materialize(conn, a.todo_id, a.workdir, owner=a.owner)}


def _todo_drop(conn, a) -> dict:
    from controller import model
    model.todo_drop(conn, a.todo_id)
    return {"ok": True, "todo_id": a.todo_id, "status": "dropped"}


# name -> handler(conn, parsed_args) -> result dict; the table is the single
# dispatch point (HX011: no if/elif ladder on the subcommand name).
_TODO_HANDLERS = {
    "add": _todo_add,
    "list": _todo_list,
    "materialize": _todo_materialize,
    "drop": _todo_drop,
}


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # If the first positional is 'todo', dispatch to the to-do CLI.
    if argv and argv[0] == "todo":
        return _todo_cli(argv[1:])

    ap = argparse.ArgumentParser(prog="controller.pump_cli")
    ap.add_argument("--scope", action="append", default=None)
    ap.add_argument("--max-steps", type=int, default=50)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    # Single-flight, enforced HERE rather than only in the cron bash wrapper:
    # every dispatcher (cron pump_tick.sh, the controller plugin's post-session
    # spawn, manual runs) enters through main(), and only the flock'd one may
    # run. An unlocked second pump executes reconcile.recover at start and
    # marks the FIRST pump's live runs 'unknown' mid-flight, releasing their
    # claims and dispatching a second worker onto the same workdir — the
    # 13-hour orphan storm of 23.10 (13 live workers, redispatch every tick).
    # The kernel drops the lock with the holder's corpse, so takeover is
    # automatic and there is no stale-lock state to repair here.
    import fcntl
    import os
    lock_path = os.path.join(os.environ.get("HOME", "/"), ".hermes", "run", "pump-tick.lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    _lock_fh = open(lock_path, "a+")
    try:
        fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0  # another pump owns the queue right now; silence is the contract
    _ = _lock_fh  # keep the fd referenced for the process lifetime

    from controller import model
    from controller.loop import run_cycle
    from controller.workers import make_review, make_worker

    conn = model.connect()
    cfg = {}
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
    except (ImportError, OSError) as exc:  # cli/config unreadable -> default routing, never crash the pump
        import logging
        logging.getLogger("controller.pump_cli").warning("config load failed: %s", exc)
    judge_cfg = (cfg.get("plugins", {}).get("entries", {})
                 .get("supervisor", {}).get("settings", {}).get("judge"))
    cfg_ctl = cfg.get("controller", {}) or {}
    scopes = a.scope
    if not scopes:
        scopes = [r["workdir"] for r in conn.execute(
            "SELECT DISTINCT workdir FROM ctl_task"
            " WHERE state IN ('ready','running')")]
    out = {}
    for scope in scopes:
        perm = model.get_permission(conn, scope)
        if not perm["enabled"]:
            out[scope] = [{"stop": "permission off"}]
            continue
        out[scope] = run_cycle(
            conn, scope=scope, max_steps=a.max_steps,
            worker_fn=make_worker(
                conn,
                model_id=(cfg_ctl.get("worker_model") or None),
                provider=(cfg_ctl.get("worker_provider") or None)),
            review_fn=make_review(conn, judge_cfg=judge_cfg))
    print(json.dumps(out, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
