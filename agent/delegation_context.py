"""Context-local state for delegate_task child execution.

Kanban excision note (15.11): the dispatcher that spawned ``HERMES_KANBAN_*``
worker processes is gone. The env-scrubbing half survives only as hygiene for
environments that still carry the legacy variables; identity gates below
therefore answer as they would for an ordinary non-worker process.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator, Mapping, MutableMapping

_DELEGATED_CHILD_CONTEXT: ContextVar[bool] = ContextVar("hermes_delegated_child_context", default=False)
# Any in-process execution that is NOT the dispatcher-owned worker (scheduled jobs). Kept separate
# so delegate_task-specific behaviour (subprocess env scrubbing, its error strings) is unchanged.
_NON_DISPATCHER_OWNED_CONTEXT: ContextVar[bool] = ContextVar("hermes_non_dispatcher_owned_context", default=False)

DELEGATED_CHILD_ENV_MARKER = "HERMES_DELEGATED_CHILD_CONTEXT"

# Legacy worker-identity variables; kept only so stale environments are scrubbed.
KANBAN_ENV_KEYS: tuple[str, ...] = (
    "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK",
    "HERMES_KANBAN_GOAL_MODE", "HERMES_KANBAN_GOAL_MAX_TURNS",
)


@contextmanager
def delegated_child_context(session_id: str | None = None) -> Iterator[None]:
    """Mark child execution and isolate its task-local session identity. Even a context
    entered without an id must restore the parent's session ContextVar (child
    construction calls ``set_current_session_id``)."""
    token = _DELEGATED_CHILD_CONTEXT.set(True)
    try:
        from gateway.session_context import scoped_current_session_id  # lazy: it calls is_delegated_child_context()

        with scoped_current_session_id(session_id):
            yield
    finally:
        _DELEGATED_CHILD_CONTEXT.reset(token)


def is_delegated_child_context() -> bool:
    """Return True while code is running for a delegate_task child."""
    return bool(_DELEGATED_CHILD_CONTEXT.get())


def enter_non_dispatcher_owned_context() -> Token[bool]:
    """Token form of :func:`non_dispatcher_owned_context` for long try/finally scopes."""
    return _NON_DISPATCHER_OWNED_CONTEXT.set(True)


def exit_non_dispatcher_owned_context(token: Token[bool]) -> None:
    """Restore the flag saved by :func:`enter_non_dispatcher_owned_context`."""
    _NON_DISPATCHER_OWNED_CONTEXT.reset(token)


@contextmanager
def non_dispatcher_owned_context() -> Iterator[None]:
    """Mark in-process execution that owns no dispatcher task. The dispatcher itself was
    removed with the Kanban excision; the scope remains so scheduled jobs and children
    never look like one to leftover gates."""
    token = enter_non_dispatcher_owned_context()
    try:
        yield
    finally:
        exit_non_dispatcher_owned_context(token)


def is_delegated_child_process_context() -> bool:
    """Return True in this process or a subprocess spawned by a child."""
    return bool(_DELEGATED_CHILD_CONTEXT.get()) or bool(os.environ.get(DELEGATED_CHILD_ENV_MARKER))


def is_dispatcher_owned_worker_context() -> bool:
    """The single predicate every former ``HERMES_KANBAN_*`` identity gate used.

    Since the excision there is no dispatcher ownership; this answers whether the code
    runs free of any delegated-child marking (the surviving part of the old predicate).
    """
    return not (is_delegated_child_process_context() or _NON_DISPATCHER_OWNED_CONTEXT.get())


def owned_kanban_task() -> str:
    """The board task this execution OWNS. Always empty after the Kanban excision."""
    return ""


def scrub_kanban_env(env: Mapping[str, str] | MutableMapping[str, str]) -> dict[str, str]:
    """Remove legacy worker identity variables, retaining the descendant fence marker.

    Hygiene only: no board reader remains, but descendant processes must not inherit
    stale ``HERMES_KANBAN_*`` variables from shells that predate the excision.
    """
    cleaned = {k: v for k, v in env.items() if k not in KANBAN_ENV_KEYS}
    cleaned.pop("HERMES_KANBAN_DB", None)
    cleaned.pop("HERMES_KANBAN_BOARD", None)
    cleaned.pop("HERMES_KANBAN_ROOT", None)
    # Only the gateway mints ``kanban`` sessions: a descendant ``hermes chat`` sending it is refused.
    if cleaned.get("HERMES_SESSION_SOURCE") == "kanban":
        del cleaned["HERMES_SESSION_SOURCE"]
    inherited = str(env.get(DELEGATED_CHILD_ENV_MARKER) or "")
    cleaned[DELEGATED_CHILD_ENV_MARKER] = inherited if inherited else "1"
    return cleaned


def delegated_child_subprocess_env(
    env: Mapping[str, str] | MutableMapping[str, str] | None = None,
) -> dict[str, str] | None:
    """Carry delegated-child denial across a real process spawn.

    Location and credentials are untouched; callers retain their existing secret policy.
    """
    if not is_delegated_child_process_context():
        return None if env is None else dict(env)
    return scrub_kanban_env(os.environ if env is None else env)
