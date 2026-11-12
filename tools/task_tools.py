"""Agent-facing tool for tasks_kernel: create queues, inspect, cancel, emit events.

One tool, ``task_queue``, action-discriminated — the queue semantics live in
``tasks_kernel`` (state.db ``tk_task``), not in the model's memory.
"""
from __future__ import annotations

import json
from typing import Any

from tasks_kernel import (
    cancel, create_batch, emit, skip, status,
    fanout, annotate, add_question, drop_question, set_wake_check,
)

QUEUE_SCHEMA = {
    "name": "task_queue",
    "description": (
        "Operate the durable task queue (state machine ready→running→"
        "wait_human/wait_event→done). Actions: create_batch (ordered list of "
        "{title, body}; first item is delivered at once), status, skip "
        "(complete current head, start next), emit (fire a named event that "
        "waiting tasks subscribed to via wait_event), cancel, fanout (N "
        "competing branch proposals + a final JOIN digest row; near-duplicate "
        "annotate (attach a branch's verdict for the "
        "join), add_question / drop_question (open-question register: a task "
        "with open questions cannot be ack-closed), set_check (bind a "
        "machine validity command: at every delivery the kernel runs it and "
        "a non-zero exit converts the wake-up into a rework order instead "
        "of a reportable result)."),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": [
                "create_batch", "status", "skip", "emit", "cancel",
                "fanout", "annotate", "add_question", "drop_question",
                "set_check"]},
            "chat_id": {"type": "string",
                        "description": "Messaging chat bound to the queue."},
            "items": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["title"]}},
            "deliver_target": {"type": "string",
                               "description": "hermes send -t target; default 'matrix:<chat_id>'."},
            "task_id": {"type": "string"},
            "event_id": {"type": "string"},
            "goal": {"type": "string",
                     "description": "fanout: the research goal the branches compete on."},
            "branches": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["title"]},
                "description": "fanout: >=2 competing proposals (deduped lexically)."},
            "criteria": {"type": "string",
                         "description": "fanout: decision criteria surfaced in the join digest."},
            "text": {"type": "string",
                     "description": "annotate: the branch verdict / conclusion."},
            "question": {"type": "string",
                         "description": "add_question: open assumption; drop_question: exact text or 1-based number."},
        },
        "required": ["action"],
    },
}


def _check_args(args: Any) -> bool:
    return isinstance(args, dict) and isinstance(args.get("action"), str)


def task_queue(args: dict | None = None, **_kw) -> dict:
    args = args or {}
    action = args.get("action")
    chat_id = args.get("chat_id") or ""
    try:
        if action == "create_batch":
            items = [{"title": it.get("title", ""), "body": it.get("body", "")}
                     for it in (args.get("items") or []) if isinstance(it, dict)]
            if not chat_id or not items:
                return {"ok": False, "error": "create_batch needs chat_id and items"}
            target = args.get("deliver_target") or f"matrix:{chat_id}"
            ids = create_batch(chat_id, items, deliver_target=target)
            from tasks_kernel import start
            started = start(chat_id)
            return {"ok": True, "task_ids": ids, "started": started}
        if action == "status":
            if not chat_id:
                return {"ok": False, "error": "status needs chat_id"}
            return {"ok": True, **status(chat_id)}
        if action == "skip":
            result = skip(chat_id)
            if result is None:
                return {"ok": False, "error": "no wait_human head to skip"}
            return {"ok": True, "skipped_id": result["skipped"]["task_id"],
                    "delivered": (result["delivered"] or {}).get("title")}
        if action == "emit":
            ev = args.get("event_id") or ""
            if not ev:
                return {"ok": False, "error": "emit needs event_id"}
            return {"ok": True, "resumed": emit(ev)}
        if action == "cancel":
            tid = args.get("task_id") or ""
            return {"ok": True, "cancelled": cancel(tid)}
        if action == "fanout":
            goal = args.get("goal") or ""
            brs = [{k: v for k, v in it.items()}
                   for it in (args.get("branches") or []) if isinstance(it, dict)]
            if not chat_id or not goal or len(brs) < 2:
                return {"ok": False, "error": "fanout needs chat_id, goal and >=2 branches"}
            target = args.get("deliver_target") or f"matrix:{chat_id}"
            res = fanout(chat_id, goal, brs, criteria=args.get("criteria") or "",
                         deliver_target=target)
            from tasks_kernel import start
            started = start(chat_id)
            return {"ok": True, **res, "started": bool(started)}
        if action == "annotate":
            tid = args.get("task_id") or ""
            text = args.get("text") or ""
            if not tid or not text:
                return {"ok": False, "error": "annotate needs task_id and text"}
            return {"ok": annotate(tid, text)}
        if action == "add_question":
            tid = args.get("task_id") or ""
            q = args.get("question") or ""
            if not tid or not q:
                return {"ok": False, "error": "add_question needs task_id and question"}
            return {"ok": True, "open": add_question(tid, q)}
        if action == "drop_question":
            tid = args.get("task_id") or ""
            q = args.get("question") or ""
            if not tid or not q:
                return {"ok": False, "error": "drop_question needs task_id and question"}
            qv = int(q) if q.isdigit() else q
            return {"ok": True, "open": drop_question(tid, qv)}
        if action == "set_check":
            tid = args.get("task_id") or ""
            cmd = args.get("text") or ""
            if not tid or not cmd:
                return {"ok": False, "error": "set_check needs task_id and text (shell cmd)"}
            return {"ok": set_wake_check(tid, cmd)}
        return {"ok": False, "error": f"unknown action {action!r}"}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


# Module-level registration is REQUIRED: discover_builtin_tools() AST-scans for a
# top-level ``registry.register(...)`` statement (the receiver NAME must literally
# be ``registry``) and skips helpers-only modules.
from tools.registry import registry

registry.register(
    name="task_queue",
    toolset="tasks",
    schema=QUEUE_SCHEMA,
    check_fn=lambda *_a, **_kw: True,  # tasks toolset: static availability (queue builtins always resolvable)
    handler=lambda args, **kw: task_queue(args or {}, **kw),
    emoji="🧾")

