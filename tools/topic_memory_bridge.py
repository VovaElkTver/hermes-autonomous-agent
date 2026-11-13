#!/usr/bin/env python3
"""Stage003 steps 3–4 bridge: the ``memory`` tool's third target ``task`` and the
prompt-injection block for controller-managed worker sessions.

Everything here is ENV-GATED: a session only sees target 'task' and the topic
memory block when it was spawned by the controller with
``HERMES_SCOPE_KIND/HERMES_SCOPE_ID`` set (controller.workers.scope_env, step 2).
Without those variables this module is inert — normal interactive sessions get
byte-identical tool schemas and prompts (минимум поверхности).

Write rule (Вл): «отсутствие указанного scope не является разрешением на
глобализацию». The default source inside a worker run is 'agent-proposal':
the agent records what it saw; a live human directive recorded verbatim may
pass source='user-directive' explicitly. Proposals never retire standing rules
(controller.topic_memory.add_item semantics) — errors i/ii are dearer than iii.
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

_ITEM_KINDS = ("fact", "constraint", "decision", "preference")
_SOURCES = ("user-directive", "tool-result", "agent-proposal", "hypothesis")
_MAX_PROMPT_ITEMS = 40


def task_scope_from_env() -> dict[str, str] | None:
    """The controller-assigned memory scope of THIS turn, or None.

    Read through agent.session_client_env.session_env: inside a gateway-hosted
    turn this returns the client_env the spawning worker process froze into its
    session (stage003 step 2); in a plain process it falls back to os.environ,
    so CLI/scheduled/direct callers keep the pre-bridge behaviour.
    """
    from agent.session_client_env import session_env
    kind = session_env("HERMES_SCOPE_KIND") or ""
    scope_id = session_env("HERMES_SCOPE_ID") or ""
    if kind != "task" or not scope_id:
        return None
    return {"kind": kind, "scope_id": scope_id,
            "project_id": session_env("HERMES_PROJECT_ID") or "",
            "run_id": session_env("HERMES_CONTROLLER_RUN_ID") or ""}


def available() -> bool:
    return task_scope_from_env() is not None


def restrict_globals_for_scoped_workers(config: dict | None = None) -> bool:
    """Config flag (memory.task_restrict_globals, default False): when True,
    a scoped worker's built-in memory/user targets become read-only at the
    tool gate. Off during shadow mode — it flips only after Вл's acceptance."""
    try:
        if config is None:
            from hermes_cli.config import load_config
            config = load_config() or {}
        mem = config.get("memory") if isinstance(config, dict) and isinstance(config.get("memory"), dict) else {}
        return bool(mem.get("task_restrict_globals", False))
    except Exception:  # fail-open: config trouble must not brick writes
        return False


def _connect():
    from controller import model as ctl_model
    return ctl_model.connect()


def _task_scope_id(conn, scope: dict[str, str]) -> str:
    from controller import topic_memory as tm
    sid = tm.ensure_scope(conn, "task", scope["scope_id"])
    if scope.get("project_id"):
        project_sid = tm.ensure_scope(conn, "project", scope["project_id"])
        tm.bind_parent(conn, sid, project_sid)
    return sid


def apply_task_ops(ops: list[dict[str, Any]]) -> dict[str, Any]:
    """Execute memory-tool ops against the task scope. Never raises for
    data-shaped problems — returns a JSON-able failure instead."""
    scope = task_scope_from_env()
    if scope is None:
        return {"success": False, "target": "task",
                "error": ("No task scope in this process: HERMES_SCOPE_KIND/HERMES_SCOPE_ID "
                          "are absent, so there is no local write target. Global stores stay "
                          "unscoped on purpose — «отсутствие указанного scope не является "
                          "разрешением на глобализацию».")}
    applied: list[dict[str, Any]] = []
    conn = _connect()
    try:
        from controller import topic_memory as tm
        sid = _task_scope_id(conn, scope)
        for index, op in enumerate(ops):
            op = op or {}
            action = op.get("action")
            content = op.get("content")
            # Subject IS the in-scope identity: explicit subject → the old_text
            # (the span being patched) → first line of the new content.
            subject = (op.get("subject") or "").strip()
            if not subject and op.get("old_text"):
                subject = str(op["old_text"]).strip().splitlines()[0][:80]
            if not subject and content:
                subject = content.strip().splitlines()[0][:80]
            if action == "add" or (action == "patch" and content):
                if not content or not content.strip():
                    return {"success": False, "target": "task",
                            "error": f"op {index}: content is required."}
                kind = op.get("kind") or "fact"
                source = op.get("source") or "agent-proposal"
                confirmed = op.get("confirmed")
                if kind not in _ITEM_KINDS:
                    return {"success": False, "target": "task",
                            "error": f"op {index}: unknown kind {kind!r}; expected one of {list(_ITEM_KINDS)}."}
                if source not in _SOURCES:
                    return {"success": False, "target": "task",
                            "error": f"op {index}: unknown source {source!r}; expected one of {list(_SOURCES)}."}
                item_id = tm.add_item(conn, sid, kind, subject, content.strip(), source,
                                      run_id=scope["run_id"] or None,
                                      confirmed=confirmed)
                applied.append({"action": action, "item_id": item_id, "subject": subject})
                continue
            if action == "remove":
                if not subject:
                    return {"success": False, "target": "task",
                            "error": f"op {index}: remove needs subject (or old_text naming one)."}
                retired = tm.supersede_subject(conn, sid, subject)
                applied.append({"action": "remove", "subject": subject, "retired": retired})
                continue
            return {"success": False, "target": "task",
                    "error": f"op {index}: unsupported action {action!r} for target='task' "
                             f"(use add/patch-with-content/remove)."}
        return {"success": True, "target": "task", "applied": applied}
    finally:
        conn.close()


def task_goal_topic(*, max_chars: int = 300) -> str | None:
    """Stage003 step 5: compaction focus ← the managed task's own goal.

    In a controller run the topic of record is ctl_task.goal, NOT the most
    recent user turns (a worker's chat noise should not redefine priority).
    Returns a compact prefixed string the summarizer can quote; None outside
    controller scopes, on missing rows, or on any read failure."""
    scope = task_scope_from_env()
    if scope is None:
        return None
    try:
        conn = _connect()
        try:
            row = conn.execute("SELECT goal FROM ctl_task WHERE task_id=?",
                               (scope["scope_id"],)).fetchone()
        finally:
            conn.close()
        goal = (row["goal"] or "").strip() if row else ""
        if not goal:
            return None
        goal = " ".join(goal.split())
        if len(goal) > max_chars:
            goal = goal[: max_chars - 1].rstrip() + "…"
        return f"Managed task goal: {goal}"
    except Exception as exc:
        logger.warning("task-goal focus topic skipped: %s", exc)
        return None


def render_prompt_block(*, include_unconfirmed_note: bool = True) -> str | None:
    """The volatile-band topic-memory block; None outside controller scopes or
    on any internal failure (prompt build must never crash over SQL)."""
    scope = task_scope_from_env()
    if scope is None:
        return None
    try:
        conn = _connect()
        try:
            from controller import topic_memory as tm
            sid = _task_scope_id(conn, scope)
            items = tm.effective_items(conn, sid)
            n_hidden = len(tm.effective_items(conn, sid, include_unconfirmed=True)) - len(items)
            chain = [s["kind"] + (f" {s['ref']}" if s["ref"] else "")
                     for s in tm.resolve_chain(conn, sid)]
            lines = []
            for item in items[:_MAX_PROMPT_ITEMS]:
                origin = {"task": "this task", "project": "project policy"}.get(
                    item["from_kind"], "global policy")
                lines.append(f"- [{item['kind']}|{origin}] {item['subject']} — {item['body']}")
            if len(items) > _MAX_PROMPT_ITEMS:
                lines.append(f"- …and {len(items) - _MAX_PROMPT_ITEMS} more earlier items")
                # step 6: confirmed rules known to the chain but not rendered —
                # the mechanical slice of error (iii) «при возврате к теме не найдено».
                from controller import topic_memory_shadow as shadow
                shadow.record("missing_decision", sid,
                              {"total": len(items), "rendered": _MAX_PROMPT_ITEMS},
                              conn=conn)
            header = ("## Topic memory\n"
                      f"Applicable scope chain: {' → '.join(chain)}. Written where it applies; "
                      "cross-topic records do NOT leak here (a semantic match grants no read "
                      "access). Propose generalizations via memory(target='task') — do not raise "
                      "the scope silently.")
            if include_unconfirmed_note and n_hidden > 0:
                lines.append(f"(+{n_hidden} unconfirmed proposals hidden until accepted)")
            return "\n".join([header] + lines) if lines else None
        finally:
            conn.close()
    except Exception as exc:  # fail-open, log loudly enough to be measurable
        logger.warning("topic-memory prompt block skipped: %s", exc)
        return None
