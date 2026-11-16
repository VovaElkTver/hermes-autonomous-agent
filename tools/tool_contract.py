"""Tool-call contract v2 (TZ-2026-11-16-dsh-B, items B1+B3).

Frozen call identity, the monotonic final decision guard, and call identity in
observations — ported from the DeepSeek Harness ToolGuard / ToolExecution
contracts (donor: /home/qwen/deepseek-harness, MIT, read-only):

* B1: a scope-aware final pre-dispatch guard whose return type has NO allow
  result: ``None`` preserves the waterfall decision, a returned denial can only
  REDUCE permission, so a later listener cannot undo a lowering (structural:
  the guard signature is ``-> None | Denial``).
* B3: the call identity lives on the frozen :class:`ToolCallSpec` built at the
  dispatch entry; wrappers cannot mint a second identity or rewrite args (a
  mutation inside a wrapper is detected by re-hashing the args; RuntimeError
  under ``tools.call_spec_debug``, warn-log in production). The final decision
  and the lowering layer land in the ``event_observations`` row (nullable
  columns, two-step migration) and in the tool result's ``meta.call_contract``.

Flag: ``tools.call_contract_v2`` (default OFF — the dispatch path stays
byte-identical to the legacy behavior). Debug flag: ``tools.call_spec_debug``
(default OFF: arg mutation is a warn-log, never a crash).

Naming doctrine: no "cron" in any new name; periodics stay on ``ctl_followup``
rows plus the 1 Hz controller ticker (item B4).
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config access (pattern: tools/approval_context._get_approval_config)
# ---------------------------------------------------------------------------

def _tools_config() -> dict:
    """The LIVE ``tools`` config block (config-cache sub-dict; never mutated)."""
    try:
        from hermes_cli.config import load_config_readonly
        return load_config_readonly().get("tools", {}) or {}
    except Exception:
        logger.debug("tool contract: config read failed; flags stay off", exc_info=True)
        return {}


def call_contract_enabled() -> bool:
    """``tools.call_contract_v2`` — default OFF: byte-identical legacy dispatch."""
    return bool(_tools_config().get("call_contract_v2", False))


def call_spec_debug() -> bool:
    """``tools.call_spec_debug`` — arg mutation inside a wrapper raises instead of warn-log."""
    return bool(_tools_config().get("call_spec_debug", False))


# ---------------------------------------------------------------------------
# B1(a) + B3: frozen call identity
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ToolCallSpec:
    """Immutable identity of ONE dispatch (B1(a), donor: the immutable ToolExecution).

    Built exactly once, at the dispatch entry; every hook/wrapper/approval path
    receives it as-is. ``args_hash`` is the sha256 of the canonical JSON of the
    args AT DISPATCH ENTRY, so a wrapper that mutates the args produces a
    different hash — detectable, and (debug flag) fatal.
    """

    call_id: str        # uuid4 per dispatch
    tool: str
    args_hash: str      # sha256 of canonical JSON (sort_keys)
    session_key: str


def canonical_args_hash(args: dict) -> str:
    """sha256 over the canonical JSON of *args* (sort_keys, compact separators,
    ``default=str`` keeps the hash total for non-JSON argument values)."""
    canonical = json.dumps(args, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_call_spec(*, tool: str, args: dict, session_key: str,
                    call_id: Optional[str] = None) -> ToolCallSpec:
    """One spec per dispatch: ``call_id`` is a fresh uuid4 unless supplied."""
    return ToolCallSpec(
        call_id=call_id or uuid.uuid4().hex,
        tool=tool,
        args_hash=canonical_args_hash(args),
        session_key=session_key or "",
    )


class ArgsMutatedError(RuntimeError):
    """Contract violation: the args dict no longer hashes to the frozen spec's
    ``args_hash`` — a wrapper rewrote the arguments. Raised under
    ``tools.call_spec_debug``; in production it is a warn-log (never a crash)."""


# ---------------------------------------------------------------------------
# B1(b): the monotonic decision type
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Allow:
    """No lowering: the pre-waterfall decision (allow/deny/ask as settled before
    the final guard pass) stands. The guard pass itself never PRODUCES an Allow."""


@dataclass(frozen=True, slots=True)
class Ask:
    """A human decision is owed before the call may proceed (the pre-waterfall
    may still ask; only the final pass's denials are final)."""

    reason: str
    layer: str = ""


@dataclass(frozen=True, eq=False, slots=True)
class Deny:
    """Terminal lowering. Nothing downstream of the guard pass may re-allow it:
    the guard return type has no allow result, and a Deny is absorbing here."""

    reason: str
    info: dict = field(default_factory=dict)
    layer: str = ""


ToolDecision = Allow | Ask | Deny


@dataclass(frozen=True, eq=False, slots=True)
class Denial:
    """What a guard may return (donor ToolGuardDenial): a structured refusal that
    can ONLY reduce permission. ``info`` carries the denial's structured identity
    into the result meta."""

    reason: str
    info: dict = field(default_factory=dict)
    layer: str = ""


#: One guard: current decision in, ``None`` (preserve) or a Denial (reduce) out.
Guard = Callable[[ToolCallSpec, ToolDecision, dict], Optional[Denial]]

_guard_layers: list[tuple[str, Guard]] = []
_guard_lock = threading.Lock()


def register_tool_guard(layer: str, guard: Guard) -> None:
    """Register one lowering guard under its layer name (run in registration order)."""
    with _guard_lock:
        _guard_layers.append((layer, guard))


def clear_tool_guards() -> None:
    """Drop all registered guards (test seam)."""
    with _guard_lock:
        _guard_layers.clear()


def apply_guards(spec: ToolCallSpec, decision: ToolDecision, args: dict) -> ToolDecision:
    """THE final monotonic pass, run directly before the handler (B1(b)).

    Each guard receives the current decision and may answer ``None`` (preserve)
    or a :class:`Denial` (reduce). Structurally it cannot undo a lowering:
    the return type has no allow result, and a ``Deny`` is absorbing — a guard
    answering against an already-denied decision is a no-op, so no later
    listener can re-allow. The first lowering layer is remembered on the
    resulting ``Deny.layer`` (B3: who lowered, not just that it lowered).

    A RAISING guard fails closed (a broken security guard must not open the
    door): the call is denied with the guard's layer named.
    """
    current = decision
    with _guard_lock:
        guards = list(_guard_layers)
    for layer, guard in guards:
        try:
            denial = guard(spec, current, args)
        except Exception:
            logger.error("tool guard %r raised; failing closed", layer, exc_info=True)
            return Deny(reason=f"guard {layer!r} failed closed",
                       info={"guard": layer}, layer=layer)
        if denial is None:
            continue
        if isinstance(current, Deny):
            continue  # already at the lowest rung; the guard may not rewrite it
        current = Deny(reason=denial.reason, info=dict(denial.info or {}),
                       layer=denial.layer or layer)
    return current


# ---------------------------------------------------------------------------
# B1(v): identical-call repeat detection (the guard's repeat move, dsh analogue)
# ---------------------------------------------------------------------------

#: A repeat is "the same args_hash for the same session inside the window".
_REPEAT_WINDOW_S = 300.0
_RECENT_CALLS_MAX = 1024
_recent_calls: dict[tuple[str, str], tuple[float, int]] = {}
_recent_calls_lock = threading.Lock()


def note_repeat(spec: ToolCallSpec) -> bool:
    """Record this call and answer whether it repeats an earlier identical call
    (same args_hash, same session key) inside the repeat window. The flag is
    ALSO the guard's repeat reminder: a guard seeing the repeat move of an
    already-decided call may treat it accordingly (query without recording via
    :func:`is_repeat`)."""
    key = (spec.session_key, spec.args_hash)
    now = time.monotonic()
    with _recent_calls_lock:
        last = _recent_calls.get(key)
        if last is not None and now - last[0] <= _REPEAT_WINDOW_S:
            _recent_calls[key] = (now, last[1] + 1)
            return True
        _recent_calls[key] = (now, 1)
        if len(_recent_calls) > _RECENT_CALLS_MAX:
            oldest = min(_recent_calls, key=lambda k: _recent_calls[k][0])
            _recent_calls.pop(oldest, None)
        return False


def is_repeat(spec: ToolCallSpec) -> bool:
    """Pure query (no recording): did an identical call already pass in the window?"""
    key = (spec.session_key, spec.args_hash)
    now = time.monotonic()
    with _recent_calls_lock:
        last = _recent_calls.get(key)
        return last is not None and now - last[0] <= _REPEAT_WINDOW_S


def clear_repeat_index() -> None:
    """Drop the repeat index (test seam)."""
    with _recent_calls_lock:
        _recent_calls.clear()


# ---------------------------------------------------------------------------
# Built-in lowering guards (the current lowering points, unified on the type)
# ---------------------------------------------------------------------------

#: Tools whose args carry a shell-command payload for the floor guard.
_COMMAND_TOOLS = frozenset({"terminal", "execute_code"})
#: Write-side tools gated by the protected-instruction write check.
_WRITE_TOOLS = frozenset({"write_file", "patch"})

_ensured = False


def ensure_contract_guards() -> None:
    """Register the built-in lowering guards (idempotent).

    Each adapter delegates to the LIVE gate it mirrors, so a contract denial
    is the live gate's own denial re-expressed as a structured Denial (B1:
    the lowering points — floors, the pre-exec guards, the write gate —
    unified on the ToolDecision type; smart/yolo-off settle on the ALLOW side
    of the pre-waterfall, and only LOWERING is expressible after the pass)."""
    global _ensured
    if _ensured:
        return
    _ensured = True
    register_tool_guard("approval_floors", _command_floor_guard)
    register_tool_guard("pre_exec_guards", _terminal_preexec_guard)
    register_tool_guard("write_gate", _write_gate_guard)


def _command_floor_guard(spec: ToolCallSpec, decision: ToolDecision, args: dict) -> Optional[Denial]:
    """The unconditional floors (hardline / runtime self-delete / sudo-stdin /
    the user's own approvals.deny globs) as ONE structured denial."""
    if spec.tool not in _COMMAND_TOOLS:
        return None
    command = args.get("command")
    if not isinstance(command, str) or not command:
        return None
    from tools.approval_floors import floor_denial
    return floor_denial(command)


def _terminal_preexec_guard(spec: ToolCallSpec, decision: ToolDecision, args: dict) -> Optional[Denial]:
    """The terminal tool's pre-execution guards, mirrored at contract level:
    workdir validation, the supervised-gateway self-repo git guard, and the
    locked-skill guard (all pure functions of command/workdir/session_key)."""
    if spec.tool != "terminal":
        return None
    command = args.get("command")
    if not isinstance(command, str) or not command:
        return None
    from tools.terminal_tool_guards import (
        _validate_workdir, locked_skill_block, self_repo_block)
    workdir = args.get("workdir")
    workdir = workdir if isinstance(workdir, str) and workdir else None
    err = _validate_workdir(workdir or "")
    if err:
        return Denial(reason=err, info={"pre_exec": "workdir"}, layer="pre_exec_guards")
    session_key = spec.session_key
    for check_name, block in (("self_repo", self_repo_block),
                              ("locked_skill", locked_skill_block)):
        message = block(command=command, cwd="", workdir=workdir, session_key=session_key)
        if message:
            return Denial(reason=message, info={"pre_exec": check_name}, layer="pre_exec_guards")
    return None


def _write_gate_guard(spec: ToolCallSpec, decision: ToolDecision, args: dict) -> Optional[Denial]:
    """The write gate, expressed as a contract-level lowering: a write that
    targets a protected agent-instruction file is denied up front EXACTLY WHEN
    no human channel can answer the one-operation approval — the same
    fail-closed outcome the handler's interactive gate produces in its no-human
    case. With a live surface the interactive gate stays the single decider
    (one prompt, no duplication), so the guard defers (None)."""
    if spec.tool not in _WRITE_TOOLS:
        return None
    path = args.get("path")
    if not isinstance(path, str) or not path:
        return None
    from tools import approval as _approval
    from tools.file_tools_write_guards import _protected_instruction_reason
    reason = _protected_instruction_reason(path)
    if not reason:
        return None
    _, is_cli, is_gateway, is_ask = _approval._presence(None)
    if is_cli or is_gateway or is_ask:
        return None
    return Denial(
        reason=(f"BLOCKED: write to protected agent-instruction file(s) ({reason}). "
                "The user has NOT consented to this write and no approval channel is "
                "present. Do NOT retry it or attempt the same edit via another path "
                "(terminal, execute_code, etc.)."),
        info={"write_gate": "protected_instruction", "protected_reason": reason},
        layer="write_gate",
    )


# ---------------------------------------------------------------------------
# B3: identity into the result meta and the event_observations row
# ---------------------------------------------------------------------------

def decision_kind(decision: ToolDecision) -> str:
    """Stable string form of the decision: allow | ask | deny."""
    return type(decision).__name__.lower()


def call_meta(spec: ToolCallSpec, decision: ToolDecision, *,
              repeat: bool = False, mutated: bool = False) -> dict:
    """The contract identity block that lands in the result's ``meta``."""
    meta = {
        "call_id": spec.call_id,
        "args_hash": spec.args_hash,
        "decision": decision_kind(decision),
        "lowered_by": getattr(decision, "layer", "") if isinstance(decision, Deny) else "",
        "repeat": bool(repeat),
        "args_mutated": bool(mutated),
    }
    if isinstance(decision, Deny) and decision.info:
        meta["info"] = dict(decision.info)
    return meta


def attach_call_meta(result, spec: ToolCallSpec, *, decision: ToolDecision,
                      repeat: bool = False, mutated: bool = False):
    """Fold the contract identity into a JSON string result under
    ``meta.call_contract``. Multimodal envelopes and non-JSON results pass
    through untouched; this NEVER raises (an observation must not break a
    tool call)."""
    if not isinstance(result, str):
        return result
    try:
        payload = json.loads(result)
    except ValueError:
        return result
    if not isinstance(payload, dict):
        return result
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        meta = {}
    meta["call_contract"] = call_meta(spec, decision, repeat=repeat, mutated=mutated)
    payload["meta"] = meta
    return json.dumps(payload, ensure_ascii=False)


def denial_result(tool: str, spec: ToolCallSpec, decision: Deny, *,
                  repeat: bool = False) -> str:
    """The dispatch-level denial body: a bounded JSON error carrying the
    contract meta (B3) so the transcript's tool record joins back on call_id."""
    from tools.registry import tool_error  # deferred: registry imports this module at top level
    lowered_by = decision.layer or "contract"
    message = f"Tool call {spec.call_id[:12]} denied ({lowered_by}): {decision.reason}"
    return tool_error(message, meta={"call_contract": call_meta(spec, decision, repeat=repeat)})


def record_call_observation(spec: ToolCallSpec, decision: ToolDecision, *,
                            repeat: bool = False) -> None:
    """B3: the call-identity row in ``event_observations`` (two-step migration,
    stage 2: the columns exist declaratively; this flag-gated writer fills
    them). Short-lived connection, every error swallowed — an observation
    failure must never break a tool call. No-op when the flag is off."""
    if not call_contract_enabled():
        return
    try:
        from controller.model import _connect
        from controller.transport import record_tool_call_observation
        conn = _connect()
        try:
            record_tool_call_observation(conn, spec=spec, decision=decision, repeat=repeat)
        finally:
            conn.close()
    except Exception:
        logger.debug("tool-call observation write failed (state.db unavailable or busy)",
                     exc_info=True)
