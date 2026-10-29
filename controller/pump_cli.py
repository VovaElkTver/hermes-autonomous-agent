"""controller.pump_cli — one-shot queue pump in a detached process.

Run: python -m controller.pump_cli [--scope DIR] (default: every workdir that has
queued tasks). Invoked by the controller plugin after operator sessions (and safe
to cron). The drain goes through the SAME ``controller.pump_lock`` as the in-thread
ticker: a contender against a live holder prints status «held» and exits without
run_cycle — one worker at a time per scope by construction, the subprocess dies
when the queue empties.

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

    from controller import model, pump_lock
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
    # Same single-flight gate as the ticker's pump path (audit 28.10): a live
    # holder means this invocation exits with status «held», never a second
    # concurrent drain of the same queue.
    lock_path = str(pump_lock.default_lock_path())
    lock = pump_lock.acquire(lock_path, conn=conn)
    if lock["status"] == "held":
        print(json.dumps({"status": "held", "holder_pid": lock["holder_pid"],
                          "detail": lock["detail"]}, ensure_ascii=False))
        conn.close()
        return 0
    out = {}
    try:
        for scope in scopes:
            perm = model.get_permission(conn, scope)
            if not perm["enabled"]:
                out[scope] = [{"stop": "permission off"}]
                pump_lock.heartbeat(lock_path)
                continue
            out[scope] = run_cycle(
                conn, scope=scope, max_steps=a.max_steps,
                worker_fn=make_worker(
                    conn,
                    model_id=(cfg_ctl.get("worker_model") or None),
                    provider=(cfg_ctl.get("worker_provider") or None),
                    on_poll=lambda: pump_lock.heartbeat(lock_path)),
                review_fn=make_review(conn, judge_cfg=judge_cfg))
            pump_lock.heartbeat(lock_path)
    finally:
        pump_lock.release(lock["handle"])
    print(json.dumps(out, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
