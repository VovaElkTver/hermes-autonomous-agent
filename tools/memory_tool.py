#!/usr/bin/env python3
"""Memory Tool - persistent curated memory (MEMORY.md = agent notes, USER.md = user
profile). Both enter the system prompt as a FROZEN snapshot at session start;
mid-session writes hit disk but never change the prompt (prefix cache intact).
Single `memory` tool: add/patch/remove or a batch `operations` list."""

import copy
import json
import logging
from contextvars import ContextVar
from pathlib import Path
from hermes_constants import get_hermes_home
from typing import Dict, Any, List, Optional, Tuple

from utils import is_truthy_value
from tools.registry import no_cache_check_fn

# fcntl is Unix-only; Windows uses msvcrt. MemoryStore reads both lazily from
# this module (tests patch ``memory_tool.fcntl``).
msvcrt = None
try:
    import fcntl
except ImportError:
    fcntl = None
    try:
        import msvcrt
    except ImportError:
        pass

logger = logging.getLogger(__name__)

# One tool-definition pass must use ONE config decision for availability and the
# dynamic target schema: the check_fn result flows to the immediately following
# dynamic_schema_overrides call; ContextVar isolates concurrent profile builds.
_memory_surface_flags: ContextVar[Optional[tuple[bool, bool]]] = ContextVar("memory_surface_flags", default=None)


def get_memory_dir() -> Path:
    """Profile-scoped memories dir, resolved per call (HERMES_HOME may switch after import)."""
    return get_hermes_home() / "memories"


from tools.memory_tool_store import (
    ENTRY_DELIMITER, FAILURE_CLASS, MEMORY_BLOCK_HEADERS, PATCH_DROP_HINT, MemoryStore, _scan_memory_content)


def load_on_disk_store() -> MemoryStore:
    """Fresh on-disk MemoryStore with configured limits/flags for contexts with no live
    agent (gateway, Desktop, ``/memory``) so approvals enforce the SAME caps as
    ``agent_init``. Falls back to defaults if config can't load; never raises."""
    try:
        from hermes_cli.config import load_config
        config = load_config() or {}
        mem_cfg = get_builtin_memory_config(config)
        memory_enabled, user_profile_enabled = get_builtin_memory_store_flags(config)
        store = MemoryStore(int(mem_cfg.get("memory_char_limit", 2200)), int(mem_cfg.get("user_char_limit", 1375)),
                            memory_enabled=memory_enabled, user_profile_enabled=user_profile_enabled,
                            topic_gate=bool(mem_cfg.get("topic_gate", False)),
                            topic_registry=mem_cfg.get("topic_registry"))
    except Exception:
        store = MemoryStore()  # config optional — fall back to defaults rather than break /memory
    store.load_from_disk()
    return store


_STORE_ACTIONS = {
    "add": lambda store, target, content, old_text: store.add(target, content),
    "patch": lambda store, target, content, old_text: store.patch(target, old_text, content),
    "remove": lambda store, target, content, old_text: store.remove(target, old_text),
}


def _validate_single_op(store, action, target, content, old_text) -> Optional[str]:
    """Validate BEFORE the gate so an invalid write is rejected now, not at approve time.
    Missing ``old_text`` is recoverable (it can't be schema-required — needs a combinator
    the Codex backend rejects): return the inventory plus a retry instruction."""
    if action == "add" and not content:
        FAILURE_CLASS.set("missing_content")
        return tool_error("Content is required for 'add' action.", success=False)
    if action in ("remove", "patch") and not old_text:
        FAILURE_CLASS.set("missing_old_text")
        return json.dumps({
            "success": False,
            "error": (f"'{action}' needs old_text -- a short unique substring of the entry "
                      f"to {action}. None was provided. Reissue the {action} with old_text "
                      f"set to part of one of the current_entries below."),
            "current_entries": store._entries_for(target), "usage": store._usage(target)}, ensure_ascii=False)
    if action == "patch" and not content:
        FAILURE_CLASS.set("missing_content")
        return tool_error(f"content (the new text for the old_text span) is required for 'patch' action. "
                          f"{PATCH_DROP_HINT}", success=False)
    return None


# 'replace' is no longer offered to the model; it stays here so writes staged before its
# removal still pin, display and replay.
_BG_DELETE_ACTIONS = ("replace", "remove", "patch")
_REPLACE_REMOVED = ("'replace' is not available; use 'patch'. old_text is the exact span to change and "
                    "content is its new text; the rest of the entry is kept. To rewrite a whole entry, "
                    "set old_text to the whole entry. Nothing was written.")


def destructive_ops(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The replace/remove/patch ops of a memory payload, single-op or batch shape."""
    ops = (payload.get("operations") or []) if payload.get("action") == "batch" else [payload]
    return [op for op in ops if (op or {}).get("action") in _BG_DELETE_ACTIONS]


def _background_delete_gate(action, operations, target="memory", content=None,
                            old_text=None) -> Optional[str]:
    """Fail-closed operation gate for unattended background-review forks (#105921):
    ``add`` stays available (it is all any review prompt asks for), while destructive
    ``patch``/``remove`` — single or inside a batch — are denied outright. Memory writes
    are direct: there is no pending-review store to stage them into."""
    from tools.skill_provenance import is_unattended_review

    if not is_unattended_review():
        return None
    payload = ({"action": "batch", "target": target, "operations": operations}
               if operations is not None else
               {"action": action, "target": target, "content": content, "old_text": old_text})
    if not destructive_ops(payload):
        return None
    FAILURE_CLASS.set("gate_refused")
    return tool_error(
        "Background review may not delete memory entries ('patch'/'remove', including in a "
        "batch); 'add' is still available.", success=False)


def memory_tool(action: str | None = None, target: str = "memory", content: str | None = None, old_text: str | None = None,
                new_text: str | None = None, operations: Optional[list[dict[str, Any]]] = None,
                store: Optional[MemoryStore] = None) -> str:
    """Tool entry point; returns a JSON string. Single op (action + content/old_text)
    or batch (``operations``, atomic against the final budget). ``new_text``
    aliases ``content``."""
    if store is None and target != "task":
        return tool_error("Memory is not available. It may be disabled in config or this environment.", success=False)
    token = FAILURE_CLASS.set("other")
    try:
        outcome, result = _memory_tool(action, target, content, old_text, new_text, operations, store)
        failure_class = "none" if outcome == "success" else FAILURE_CLASS.get()
    finally:
        FAILURE_CLASS.reset(token)
    from hermes_cli.observability.shared_metrics_loop import record_builtin_memory_call
    record_builtin_memory_call(action, operations, outcome=outcome, failure_class=failure_class)
    return result


def _applied(result: dict[str, Any]) -> tuple[str, str]:
    return ("success" if result.get("success") else "failed"), json.dumps(result, ensure_ascii=False)


def _invalid(message: str) -> tuple[str, str]:
    FAILURE_CLASS.set("invalid_args")
    return "rejected", tool_error(message, success=False)


def _memory_tool(action, target, content, old_text, new_text, operations, store) -> tuple[str, str]:
    """``(outcome, result_json)``: ``rejected`` when refused or held before touching the store."""
    # An omitted optional string can arrive as "" (#90468): let the new_text alias fill it.
    if not content and new_text:
        content = new_text
    target_error = _memory_target_error(store, target, operations)
    if target_error is not None:
        return "rejected", json.dumps(target_error)
    if operations:
        if not isinstance(operations, list):
            return _invalid("operations must be a list of {action, content?, old_text?} objects.")
        if any(isinstance(op, dict) and op.get("action") == "replace" for op in operations):
            return _invalid(_REPLACE_REMOVED)
        denied = _background_delete_gate(action, operations, target)
        if denied is not None:
            return "rejected", denied
        if target == "task":  # stage003 step 3: SQL-backed topic scope, no char budget
            from tools.topic_memory_bridge import apply_task_ops
            return _applied(apply_task_ops(operations))
        return _applied(store.apply_batch(target, operations))
    # Reject calls with neither operations[] nor the legacy top-level action.
    # Without this guard the dispatch falls through to the generic
    # "Unknown action 'None'" error, which gives the model no signal about
    # *why* the call was malformed and can trigger repeated retries. (#64291)
    if not action and not operations:
        return _invalid(
            "Missing required parameter: 'operations', a list of {action: add/patch/remove, "
            "content?, old_text?} changes; a single change is a one-item list."
        )
    if action == "replace":
        return _invalid(_REPLACE_REMOVED)
    if action not in _STORE_ACTIONS:
        return _invalid(f"Unknown action '{action}'. Use: add, patch, remove")
    if target == "task":  # stage003 step 3: bridge handles its own validation
        from tools.topic_memory_bridge import apply_task_ops
        denied = _background_delete_gate(action, None, target, content, old_text)
        if denied is not None:
            return "rejected", denied
        return _applied(apply_task_ops([{"action": action, "content": content,
                                         "old_text": old_text}]))
    invalid = (_validate_single_op(store, action, target, content, old_text)
               or _background_delete_gate(action, None, target, content, old_text))
    if invalid is not None:
        return "rejected", invalid
    return _applied(_STORE_ACTIONS[action](store, target, content, old_text))


def get_builtin_memory_config(config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Normalized ``memory`` config section ({} when missing/malformed → flags default to
    enabled). ``agent_init`` reads the same section so availability and store cannot diverge."""
    if config is None:
        try:
            from hermes_cli.config import load_config_readonly
            config = load_config_readonly()
        except Exception:
            logger.debug("Could not read memory config for availability", exc_info=True)
            return {}
    section = config.get("memory") if isinstance(config, dict) else None
    return section if isinstance(section, dict) else {}


def get_builtin_memory_store_flags(config: Optional[dict[str, Any]] = None) -> tuple[bool, bool]:
    """Return ``(memory_enabled, user_profile_enabled)`` from resolved config."""
    section = get_builtin_memory_config(config)
    return tuple(is_truthy_value(section.get(k), default=True) for k in ("memory_enabled", "user_profile_enabled"))


@no_cache_check_fn
def check_memory_requirements() -> bool:
    """Snapshot store flags and report whether the built-in tool is available."""
    _memory_surface_flags.set(None)
    flags = get_builtin_memory_store_flags()
    _memory_surface_flags.set(flags)
    return flags[0] or flags[1]


def _shadow_record_scoped(kind: str, detail: dict[str, Any]) -> None:
    """Step 6 shadow helper: resolve the task-scope row id for the current env
    and append a ledger row. Best-effort by contract; callers also guard."""
    from tools.topic_memory_bridge import _connect, task_scope_from_env
    scope = task_scope_from_env()
    if scope is None:
        return
    from controller import topic_memory as tm
    from controller import topic_memory_shadow as shadow
    conn = _connect()
    try:
        sid = tm.get_scope(conn, "task", scope["scope_id"])
        shadow.record(kind, sid, {**detail, "scope_ref": scope["scope_id"]}, conn=conn)
    finally:
        conn.close()


def _memory_target_error(
    store: MemoryStore, target: str | None, operations: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Validate one explicit store before writing, validating a batch."""
    if target is None:
        FAILURE_CLASS.set("invalid_args")
        return {"success": False, "error": (
            "Missing required target. Retry with target='memory', target='user' or "
            "target='task' at the top level. "
            "For a batch, remove target from every operation; use separate calls for different stores. "
            "Nothing was written.")}
    if not isinstance(target, str) or target not in {"memory", "user", "task"}:
        from tools.registry import _bound_error_text
        FAILURE_CLASS.set("invalid_args")
        return {"success": False,
                "error": _bound_error_text(f"Invalid memory target '{target}'. Use 'memory', 'user' or 'task'.")}
    if isinstance(operations, list):
        for index, op in enumerate(operations, 1):
            # A nested target equal to the call's (or null) is redundant, not wrong; only a
            # different one asks for two stores in one call, which a batch cannot do.
            if isinstance(op, dict) and op.get("target") not in (None, target):
                FAILURE_CLASS.set("invalid_args")
                return {"success": False, "error": (
                    f"Operation {index} names a different target than the one at the top level. "
                    "One call writes one store: retry with separate calls for different stores. "
                    "Nothing was written.")}
    if target == "task":
        # stage003 step 3: SQL-backed topic scope. Available only inside a
        # controller-managed run (HERMES_SCOPE_*); independent of store flags.
        from tools.topic_memory_bridge import available as task_available
        if task_available():
            return None
        FAILURE_CLASS.set("invalid_args")
        return {"success": False, "target": "task", "error": (
            "Target 'task' is only available in controller-managed runs: this process has no "
            "HERMES_SCOPE_KIND/HERMES_SCOPE_ID. Write your local reasoning into the task's own "
            "artifacts instead of raising any global store silently.")}
    if store.target_enabled(target):
        from tools.topic_memory_bridge import (restrict_globals_for_scoped_workers,
                                               task_scope_from_env)
        scope = task_scope_from_env()
        if scope and restrict_globals_for_scoped_workers():
            FAILURE_CLASS.set("disabled")
            return {"success": False, "target": target, "error": (
                "This controller-scoped session treats the built-in stores (MEMORY.md/USER.md) "
                "as read-only policy (memory.task_restrict_globals). Record task-local facts via "
                "target='task'; propose a generalization instead of writing it globally yourself.")}
        if scope:
            # stage003 step 6 shadow telemetry: the legacy global channel was used
            # inside a scoped process while the guard is OFF. Count, never block.
            try:
                _shadow_record_scoped("leak_global", {"target": target})
            except Exception:
                pass
        return None
    label = "USER.md" if target == "user" else "MEMORY.md"
    FAILURE_CLASS.set("disabled")
    return {"success": False, "error": f"Built-in {label} writes are disabled in memory config.", "target": target}


MEMORY_SCHEMA = {
    "name": "memory",
    "description": (
        "Save durable facts to persistent memory that survive across sessions. Memory is "
        "injected into every future turn, so keep entries compact and high-signal.\n\n"
        "HOW: make ALL your changes to one target in ONE call via an 'operations' array (each "
        "item: {action, content?, old_text?}). The batch applies atomically and the char limit is "
        "checked only on the FINAL result — so a single call can remove/patch stale entries "
        "to free room AND add new ones, even when an add alone would overflow. The response "
        "reports current/limit chars and confirms completion; one batch call finishes the "
        "update, so don't repeat it.\n\n"
        "WHEN: only for facts that apply to EVERY session regardless of task: who the user "
        "is, stable environment facts, standing conventions with no task home. Anything "
        "learned while doing a task (procedures, pitfalls, and the user's preferences and "
        "corrections for that kind of work) belongs in the task's skill via skill_manage, "
        "where it loads only when relevant; memory is injected into every turn and must "
        "stay small.\n\n"
        "IF FULL: an add is rejected with the current entries shown. Reissue as ONE batch that "
        "removes or shortens enough stale entries and adds the new one together.\n\n"
        "TARGETS: 'user' = who the user is (name, role, preferences, style). 'memory' = your "
        "notes (environment, conventions, tool quirks, lessons).\n\n"
        "SKIP: trivial/obvious info, easily re-discovered facts, raw data dumps, task progress, "
        "completed-work logs, temporary TODO state (use session_search for those). Reusable "
        "procedures belong in a skill, not memory."
    ),
    # Each field is advertised once, inside operations[]. The handler still ACCEPTS the
    # top-level single-op shape (action/content/old_text) and the new_text alias: models
    # drift and older transcripts replay them.
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "enum": ["memory", "user"],
                "description": "Which memory store: 'memory' for personal notes, 'user' for user profile."
            },
            "operations": {
                "type": "array",
                "description": (
                    "Changes applied atomically in one call against the final char budget; "
                    "a single change is a one-item list."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["add", "patch", "remove"]},
                        "content": {
                            "type": "string",
                            "description": ("Required for 'add' and 'patch'. For 'add', the new entry. For 'patch', "
                                            "the text that replaces old_text; the rest of the entry is kept."),
                        },
                        "old_text": {
                            "type": "string",
                            "description": ("REQUIRED for 'patch' and 'remove': text from one entry. 'patch' replaces "
                                            "exactly this span (it must occur once in the entry); 'remove' deletes the "
                                            "whole entry. Omit for 'add'."),
                        },
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["target", "operations"],
    },
}


# Schema text when only one built-in store is enabled: (target description, TARGETS replacement).
_SINGLE_TARGET_TEXT = {
    ("memory",): ("The enabled built-in store: 'memory' for personal notes.",
                  "TARGET: only 'memory' is enabled for personal notes (environment, conventions, "
                  "tool quirks, lessons)."),
    ("user",): ("The enabled built-in store: 'user' for user profile.",
                "TARGET: only 'user' is enabled for user profile facts (name, role, preferences, style).")}


def _build_memory_schema_overrides() -> dict[str, Any]:
    """Narrow the advertised target surface using the availability snapshot."""
    flags = _memory_surface_flags.get() or get_builtin_memory_store_flags()
    _memory_surface_flags.set(None)
    targets = [t for t, on in zip(("memory", "user"), flags) if on]
    parameters = copy.deepcopy(MEMORY_SCHEMA["parameters"])
    target_schema, description = parameters["properties"]["target"], MEMORY_SCHEMA["description"]
    narrowed = _SINGLE_TARGET_TEXT.get(tuple(targets))
    if narrowed:
        target_schema["description"], replacement = narrowed
        description = description.replace(
            "TARGETS: 'user' = who the user is (name, role, preferences, style). 'memory' = your "
            "notes (environment, conventions, tool quirks, lessons).", replacement)
    # stage003 step 3: a third, SQL-backed target that exists ONLY inside a
    # controller-managed run (HERMES_SCOPE_*); schema bytes for every other
    # session stay byte-identical to the pre-stage003 surface.
    from tools.topic_memory_bridge import available as task_available
    if task_available():
        targets.append("task")
        target_schema["enum"] = targets
        description += ("\n\nTARGET 'task' = this run's topic scope in the controller DB "
                        "(kind ∈ fact|constraint|decision|preference; defaults to an unconfirmed "
                        "agent-proposal — it becomes a rule only after human acceptance, and it "
                        "never leaks outside the task/project chain).")
        target_schema["description"] = (
            "Which memory store: 'memory' personal notes, 'user' user profile, "
            "'task' this controller run's topic scope.")
    return {"description": description, "parameters": parameters}


from tools.registry import registry, tool_error

registry.register(
    name="memory",
    toolset="memory",
    schema=MEMORY_SCHEMA,
    handler=lambda args, **kw: memory_tool(
        action=args.get("action", ""), target=args.get("target"), store=kw.get("store"),
        **{k: args.get(k) for k in ("content", "old_text", "new_text", "operations")}),
    check_fn=check_memory_requirements,
    emoji="🧠",
    dynamic_schema_overrides=_build_memory_schema_overrides)
