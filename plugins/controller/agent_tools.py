"""Agent-facing tool surface of the controller (spec: docs/controller.md).

The passive layer (hook gates + skill) stops agents from doing the wrong thing;
these tools let a healthy agent DO controller work itself: see the queue,
propose tasks (which park in draft until a human "иди" agrees the contract),
close its own steps with read-back, and report a block through the authoritative
transition path instead of raw SQL.

All handlers go through controller.model / controller.transitions — the same
functions the pump and the operator commands use. No handler writes ctl_* SQL
directly except read-back SELECTs. Registration is gated by controller.enabled
(check_fn), so the tools are invisible to non-enabled profiles.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("controller.tools")

_lock = threading.RLock()


def _out(**payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _err(msg: str) -> str:
    return _out(ok=False, error=msg)


def _conn():
    from controller import model
    return model.connect()


def _task_row(conn, task_id: str) -> dict | None:
    row = conn.execute(
        "SELECT task_id, owner, project_id, workdir, goal, state, contract_version,"
        " updated_at FROM ctl_task WHERE task_id=?", (task_id,)).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------- handlers ----

def ctl_status(args: dict, **_: Any) -> str:
    """Queue snapshot: tasks (+steps when a task_id is given) and the epoch."""
    task_id = str(args.get("task_id") or "").strip()
    scope = str(args.get("workdir") or "").strip()
    with _lock, _conn() as conn:
        from controller import model
        # One deferred read transaction: every row below comes from a single
        # consistent snapshot; as_of stamps the moment that snapshot opened.
        conn.execute("BEGIN")
        as_of = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if task_id:
            task = _task_row(conn, task_id)
            if task is None:
                return _err(f"unknown task_id {task_id!r}")
            steps = conn.execute(
                "SELECT step_id, kind, title, status FROM ctl_step"
                " WHERE task_id=? ORDER BY rowid", (task_id,)).fetchall()
            events = conn.execute(
                "SELECT ts, kind, origin, payload FROM ctl_event WHERE task_id=?"
                " ORDER BY ts DESC LIMIT 8", (task_id,)).fetchall()
            return _out(ok=True, as_of=as_of,
                        permission=model.get_permission(conn, task["workdir"]),
                        task=task,
                        contract=model.current_contract(conn, task_id),
                        steps=[dict(s) for s in steps],
                        recent_events=[dict(e) for e in events],
                        events_note=("recent_events is the newest 8 rows; an empty list "
                                     "means no events exist OR they failed to load — "
                                     "treat as absence of evidence, not evidence of absence"))
        perms = [dict(r) for r in conn.execute(
            "SELECT scope, enabled, execution_epoch FROM ctl_autonomy_permission")]
        sql = ("SELECT task_id, owner, workdir, goal, state, contract_version,"
               " updated_at FROM ctl_task")
        params: list = []
        if scope:
            sql += " WHERE workdir=?"
            params.append(str(Path(scope).resolve()))
        # Fixed head of the queue (newest 50) plus one probe row for has_more:
        # an instructional tool gets an honest truncation flag, not page cursors.
        sql += " ORDER BY updated_at DESC LIMIT 51"
        rows = conn.execute(sql, params).fetchall()
        has_more = len(rows) > 50
        return _out(ok=True, as_of=as_of, permissions=perms,
                    tasks=[dict(r) for r in rows[:50]], has_more=has_more)


def ctl_enqueue(args: dict, **_: Any) -> str:
    """Propose a task. Lands as draft with an unagreed MATERIAL contract: the
    pump will NOT dispatch it until a human "иди" agrees — agent enqueue is a
    proposal, never a self-start."""
    goal = str(args.get("goal") or "").strip()
    workdir = str(args.get("workdir") or "").strip()
    if not goal or not workdir:
        return _err("'goal' and 'workdir' are required")
    if not Path(workdir).expanduser().exists():
        return _err(f"workdir does not exist: {workdir}")
    owner = str(args.get("owner") or "agent").strip()
    position = "head" if str(args.get("priority") or "").lower() == "head" else "tail"
    acceptance = args.get("acceptance") or []
    dependencies = args.get("dependencies") or []
    steps = args.get("steps") or []
    if not isinstance(acceptance, list) or not isinstance(steps, list):
        return _err("'acceptance' and 'steps' must be arrays")
    with _lock, _conn() as conn:
        from controller import model
        deps = [str(d) for d in dependencies]
        unknown = [d for d in deps
                   if conn.execute("SELECT 1 FROM ctl_task WHERE task_id=?",
                                   (d,)).fetchone() is None]
        if unknown:
            return _err(f"unknown dependency task_id(s): {unknown}")
        tid = model.create_task(conn, owner=owner, workdir=workdir, goal=goal,
                                position=position)
        if acceptance or deps:
            fields = model.current_contract(conn, tid)
            if acceptance:
                fields["acceptance"] = [str(a) for a in acceptance]
            if deps:
                fields["dependencies"] = deps
            model.revise_contract(conn, tid, fields, "MATERIAL")
        step_ids = []
        for s in steps:
            if not isinstance(s, dict) or not s.get("title"):
                return _err(f"each step needs a 'title' (got {s!r})")
            step_ids.append(model.add_step(conn, tid, str(s.get("kind") or "generic"),
                                           str(s["title"])))
        return _out(ok=True, task_id=tid, state="draft", steps=step_ids,
                    note="queued as draft; dispatch waits for human agreement ('иди')")


def ctl_step_done(args: dict, **_: Any) -> str:
    """Finish one of the caller's steps, then READ BACK the stored status —
    a step is done only when the row says so. expected_status guards against a
    concurrent session having already moved the step."""
    step_id = str(args.get("step_id") or "").strip()
    status = str(args.get("status") or "done").strip()
    expected = str(args.get("expected_status") or "pending").strip()
    if not step_id:
        return _err("'step_id' is required")
    if status not in ("done", "blocked"):
        return _err("status must be 'done' or 'blocked'")
    if expected not in ("pending", "done", "blocked", "failed"):
        return _err("expected_status must be one of: pending, done, blocked, failed")
    with _lock, _conn() as conn:
        from controller import model
        # Write transaction opened BEFORE the check: the SELECT-then-update pair
        # is atomic across processes (sessions are separate connections), which
        # a threading lock alone cannot promise.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT step_id, task_id, status FROM ctl_step WHERE step_id=?",
                           (step_id,)).fetchone()
        if row is None:
            return _err(f"unknown step_id {step_id!r}")
        if row["status"] != expected:
            return _out(ok=False, step_id=step_id, stored=row["status"], expected=expected,
                        error=f"precondition failed: stored={row['status']!r} != "
                              f"expected={expected!r} — re-read with ctl_status; pass "
                              f"expected_status={row['status']!r} only if re-closing is intended")
        model.finish_step(conn, step_id, status)
        after = conn.execute("SELECT status FROM ctl_step WHERE step_id=?",
                             (step_id,)).fetchone()
        actual = after["status"] if after else "?"
        if actual != status:
            return _out(ok=False, step_id=step_id, requested=status, stored=actual,
                        error="finish_step did not stick — read-back mismatch")
        return _out(ok=True, step_id=step_id, status=actual, task_id=row["task_id"])


def ctl_block(args: dict, **_: Any) -> str:
    """Report that a running task is stuck: transitions to 'blocked' through the
    authoritative guard path (audited in ctl_state_transition), with the reason
    in the event log. Use this instead of silently stopping."""
    task_id = str(args.get("task_id") or "").strip()
    reason = str(args.get("reason") or "").strip()
    if not task_id or not reason:
        return _err("'task_id' and 'reason' are required")
    with _lock, _conn() as conn:
        from controller import model, transitions
        task = _task_row(conn, task_id)
        if task is None:
            return _err(f"unknown task_id {task_id!r}")
        try:
            res = transitions.transition(conn, task_id, "blocked", basis=reason)
        except ValueError as exc:
            return _err(str(exc))
        model.emit(conn, kind="agent_block", origin="agent", task_id=task_id,
                   payload={"reason": reason})
        return _out(ok=True, **res)


def _ctl_todo(args: dict, **kw: Any) -> str:
    """Intent notes on the ledger (S2.2): add/list/drop. Never pump-selected —
    only a human materializes a todo into a real contract."""
    op = str(args.get("op") or "").strip()
    with _lock, _conn() as conn:
        from controller import model
        if op == "add":
            text = str(args.get("text") or "").strip()
            if not text:
                return _err("add requires text")
            owner = str(args.get("owner") or kw.get("session_id") or "agent")
            tid = model.todo_add(conn, owner=owner, text=text,
                                 origin_ref=args.get("origin_ref"),
                                 deadline_hint=args.get("deadline_hint"))
            return _out(ok=True, todo_id=tid, status="open")
        if op == "list":
            rows = model.todo_list(conn, owner=args.get("owner") or None,
                                   status=args.get("status") or None,
                                   include_text=True)
            return _out(ok=True, todos=rows, count=len(rows),
                        note="the pump never selects from ctl_todo; only a human "
                             "materializes a todo into a contracted task")
        if op == "drop":
            todo_id = str(args.get("todo_id") or "").strip()
            if not todo_id:
                return _err("drop requires todo_id")
            try:
                model.todo_drop(conn, todo_id)
            except LookupError as exc:
                return _err(str(exc))
            except ValueError as exc:
                return _err(str(exc))
            return _out(ok=True, todo_id=todo_id, status="dropped")
        return _err(f"unknown op {op!r} (use add/list/drop)")


def _ctl_followup(args: dict, **kw: Any) -> str:
    """Due-date wake notes (S2.2): the ticker delivers them as events — an agent
    cannot wake itself with model text, only register a due row here."""
    import os
    op = str(args.get("op") or "").strip()
    with _lock, _conn() as conn:
        from controller import followups
        if op == "add":
            text = str(args.get("text") or "").strip()
            kind = str(args.get("kind") or "").strip()
            if not (text and kind):
                return _err("add requires kind and text")
            scope_task = str(args.get("scope_task") or "").strip() \
                or os.environ.get("HERMES_CONTROLLER_TASK_ID", "") \
                or "session:" + str(kw.get("session_id") or "unknown")
            try:
                fid = followups.add(conn, kind=kind, scope_task=scope_task,
                                    text=text,
                                    due_at=args.get("due_at"),
                                    period_s=args.get("period_s"),
                                    rrule=args.get("rrule"),
                                    recipient=str(args.get("recipient") or "agent"),
                                    created_by="agent-tool")
            except ValueError as exc:
                return _err(str(exc))
            return _out(ok=True, followup_id=fid, scope_task=scope_task)
        if op == "list":
            rows = followups.list_open(conn, scope_task=args.get("scope_task") or None)
            return _out(ok=True, followups=rows, count=len(rows))
        if op == "cancel":
            fid = str(args.get("followup_id") or "").strip()
            if not fid:
                return _err("cancel requires followup_id")
            done = followups.cancel(conn, fid, str(args.get("reason") or ""))
            return _out(ok=done, followup_id=fid, cancelled=done)
        return _err(f"unknown op {op!r} (use add/list/cancel)")


# ------------------------------------------------------------------ schema ----

def _str(desc: str) -> dict:
    return {"type": "string", "description": desc}


_TOOLS: dict[str, tuple[Any, str, dict, list[str]]] = {
    "ctl_status": (ctl_status,
                   "Show the controller queue: all tasks with their state and contract "
                   "version, plus the autonomy permission/epoch per scope. Pass task_id for "
                   "one task's steps, contract fields and recent events (newest 8). Use this "
                   "BEFORE talking or writing about queue state — never recall it. A "
                   "point-in-time snapshot only (as_of marks when): it cannot answer "
                   "historical questions like 'what ran yesterday', and the task list is the "
                   "newest 50 (has_more says if more exist). Does not modify anything and "
                   "cannot approve or start tasks.",
                   {"task_id": _str("Optional: drill into one task"),
                    "workdir": _str("Optional: only tasks queued for this workdir scope")},
                   []),
    "ctl_enqueue": (ctl_enqueue,
                    "Propose a new controller task (goal + workdir + optional steps, "
                    "acceptance criteria, dependencies on completed task_ids). The task "
                    "lands as draft with an unagreed contract: it is NOT dispatched until "
                    "a human explicitly agrees it. Use when you want work queued for later "
                    "or a follow-up recorded — not to start work yourself and not to "
                    "change an already-agreed contract (that is an operator command). "
                    "Enqueueing is proposing, never starting.",
                    {"goal": _str("One-line objective of the task"),
                     "workdir": _str("Absolute queue scope (project directory)"),
                     "steps": {"type": "array",
                               "items": {"type": "object",
                                         "properties": {"kind": _str("step kind"),
                                                        "title": _str("step title")},
                                         "required": ["title"]},
                               "description": "Optional plan: ordered checklist steps"},
                     "acceptance": {"type": "array", "items": {"type": "string"},
                                    "description": "Verifiable acceptance criteria"},
                     "dependencies": {"type": "array", "items": {"type": "string"},
                                      "description": "task_ids that must complete first"},
                     "priority": {"type": "string", "enum": ["tail", "head"],
                                  "description": "Queue position among live tasks (default tail)"}},
                    ["goal", "workdir"]),
    "ctl_step_done": (ctl_step_done,
                      "Close one of your controller steps (done or blocked) and VERIFY it "
                      "landed: the reply reports the status actually stored. Use it only "
                      "for steps of the task you are executing. It does not review, approve "
                      "or complete the task — a passing review is still separate. If the "
                      "step is not in expected_status (default pending), nothing is "
                      "written: re-read with ctl_status instead of blind-retrying.",
                      {"step_id": _str("Step id to finish"),
                       "status": {"type": "string", "enum": ["done", "blocked"],
                                  "description": "Default: done"},
                       "expected_status": {"type": "string",
                                           "description": "Precondition; default 'pending'."}},
                      ["step_id"]),
    "ctl_block": (ctl_block,
                  "Report a running task as blocked, with a reason, through the audited "
                  "transition path (illegal transitions are rejected). Use this when you "
                  "are stuck instead of ending the turn silently. It only blocks — it "
                  "never cancels, unblocks, or revives a task, and cannot be called on a "
                  "draft (use ctl_status to see the real state first).",
                  {"task_id": _str("Task to block"),
                   "reason": _str("Why it cannot proceed")},
                  ["task_id", "reason"]),
    "ctl_todo": (_ctl_todo,
                 "Intent notes on your own ledger (cognitive class: allowed even while "
                 "autonomy is paused). add: a thing you intend to do later, with optional "
                 "deadline_hint (free text) and origin_ref; list: your open notes; drop: "
                 "abandon one (soft delete, audit kept). The queue pump NEVER selects "
                 "from these — only a human materializes a todo into a contracted task, "
                 "so writing one is recording, not committing. A todo is a durable "
                 "intention; an intention you never write down is not durable.",
                 {"op": {"type": "string", "enum": ["add", "list", "drop"]},
                  "text": _str("The intention, one line (add)"),
                  "deadline_hint": _str("Optional free-text when it matters (e.g. 'before S2 acceptance')"),
                  "origin_ref": _str("Optional pointer to what prompted it (event/journal path)"),
                  "todo_id": _str("Target for drop"),
                  "owner": _str("Optional owner filter/label (default: this session)"),
                  "status": {"type": "string", "enum": ["open", "materialized", "dropped"],
                             "description": "list filter (default: all)"}},
                 ["op"]),
    "ctl_followup": (_ctl_followup,
                     "Register a due-date wake note (cognitive class): the ticker "
                     "delivers it as an event when it comes due — you cannot wake "
                     "yourself with model text, only with a row like this. kind='reminder' "
                     "needs due_at (epoch seconds); kind='review' needs period_s or rrule "
                     "(periodic re-check). Fires at most max_repeats times, then sleeps. "
                     "list shows open rows with next_due; cancel retires one. Text goes "
                     "through the scheduled-prompt tripwire at INSERT (a command you "
                     "would not execute verbatim, do not register verbatim).",
                     {"op": {"type": "string", "enum": ["add", "list", "cancel"]},
                      "kind": {"type": "string", "enum": ["reminder", "review"]},
                      "text": _str("What to do when it fires, one line (add)"),
                      "due_at": {"type": "number", "description": "Epoch seconds (reminder)"},
                      "period_s": {"type": "integer", "description": "Review period seconds"},
                      "rrule": _str("Optional RFC-style recurrence for review kind"),
                      "scope_task": _str("Task the wake belongs to (default: running task or session)"),
                      "recipient": {"type": "string", "enum": ["agent", "user"],
                                    "description": "Default agent (self-wake); user pings the owner in chat"},
                      "followup_id": _str("Target for cancel"),
                      "reason": _str("Why cancelling (cancel)")},
                     ["op"]),
}

# Tool classification in the spirit of MCP annotations (borrowed from NVIDIA's
# open-sourced Run:ai MCP server): a declaration the dispatch barrier reads
# instead of guessing mutability from the tool name.
TOOL_META: dict[str, dict[str, bool]] = {
    "ctl_status": {"readOnlyHint": True, "destructiveHint": False},
    "ctl_enqueue": {"readOnlyHint": False, "destructiveHint": False},
    "ctl_step_done": {"readOnlyHint": False, "destructiveHint": False},
    "ctl_block": {"readOnlyHint": False, "destructiveHint": False},
    # S2.2: ledger-writing cognitive tools — mutability lives in the cognitive
    # ledger, not the world; dispatch admission comes from barrier.COGNITIVE_ACTIONS,
    # not from readOnlyHint (only ctl_status keeps the read-only survival path).
    "ctl_todo": {"readOnlyHint": False, "destructiveHint": False},
    "ctl_followup": {"readOnlyHint": False, "destructiveHint": False},
}


def is_read_only(name: str) -> bool:
    """Declared read-only tools keep working while the barrier blocks dispatch:
    a paused or epoch-stale queue still lets an agent look at the queue —
    reading is the recovery path, and the declaration is checked, not the name."""
    return TOOL_META.get(name, {}).get("readOnlyHint", False)

EMOJI = "🎛️"


def tool_defs() -> list[tuple[str, dict, Any]]:
    """(name, schema, handler) triples for ctx.register_tool."""
    out = []
    for name, (handler, description, properties, required) in _TOOLS.items():
        parameters: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            parameters["required"] = required
        out.append((name, {"name": name, "description": description,
                           "parameters": parameters}, handler))
    return out
