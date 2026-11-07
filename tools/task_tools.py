"""Agent-facing tool for tasks_kernel: create queues, inspect, cancel, emit events.

One tool, ``task_queue``, action-discriminated — the queue semantics live in
``tasks_kernel`` (state.db ``tk_task``), not in the model's memory.
"""
from __future__ import annotations

import json
from typing import Any

from tasks_kernel import (
    cancel, create_batch, emit, skip, status,
)

QUEUE_SCHEMA = {
    "name": "task_queue",
    "description": (
        "Operate the durable task queue (state machine ready→running→"
        "wait_human/wait_event→done). Actions: create_batch (ordered list of "
        "{title, body}; first item is delivered at once), status, skip "
        "(complete current head, start next), emit (fire a named event that "
        "waiting tasks subscribed to via wait_event), cancel."),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": [
                "create_batch", "status", "skip", "emit", "cancel"]},
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
    check_fn=lambda args: _check_args(args),
    handler=lambda args, **kw: task_queue(args or {}, **kw),
    emoji="🧾")

