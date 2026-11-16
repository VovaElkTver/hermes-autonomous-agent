"""Approval contract (TZ-2026-11-16-dsh-B, item B2): allowed-once grants,
pending TTL, and the three stable denial codes — behind
``approvals.once_and_ttl`` (default OFF: every behavior here is a no-op).

The three codes are stable strings carried in the tool result's
``meta.error.code`` (structure, never prose) and traceable in the
``pending_approvals`` ledger:

* ``APPROVAL_REJECTED``    — an explicit human ``/deny``.
* ``APPROVAL_CANCELLED``   — a pending outlived its TTL (nobody answered in
  the window; the row auto-resolves, a later ``/approve`` cannot revive it).
* ``APPROVAL_UNAVAILABLE`` — there is nobody to ask (detached process: no
  notify channel, no live surface): the gate denies up front with this code,
  no agent-facing pending; the ledger still records the trace.

Naming doctrine: no "cron" in any new name; periodics stay on ``ctl_followup``
rows plus the controller ticker (item B4).
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Stable denial codes (B2d): the structure of a denial, not its prose.
APPROVAL_REJECTED = "APPROVAL_REJECTED"
APPROVAL_CANCELLED = "APPROVAL_CANCELLED"
APPROVAL_UNAVAILABLE = "APPROVAL_UNAVAILABLE"

_DECISIONS = ("allow", "deny")
_SCOPE_ONCE = "once"


def _approval_config() -> dict:
    """The LIVE ``approvals`` config block (config-cache sub-dict; never mutated)."""
    try:
        from tools.approval_context import _get_approval_config
        return _get_approval_config() or {}
    except Exception:
        logger.debug("approval contract: config read failed; flags stay off", exc_info=True)
        return {}


def once_and_ttl_enabled() -> bool:
    """``approvals.once_and_ttl`` — default OFF: legacy dict/ledger behavior."""
    return bool(_approval_config().get("once_and_ttl", False))


def pending_ttl_s() -> int:
    """``approvals.pending_ttl_s`` — a pending row's lifetime (default 3600s;
    ``0`` = never expires: backward-compatible forever-pending)."""
    try:
        return max(0, int(_approval_config().get("pending_ttl_s", 3600)))
    except Exception:
        logger.debug("approval contract: pending_ttl_s read failed; using default",
                     exc_info=True)
        return 3600


def gate_code(outcome: str) -> str:
    """The B2 code for a gate *outcome* string: explicit denials REJECTED,
    everything that ended without an answer in the window CANCELLED (timeout,
    a withdrawn prompt), and an unreachable surface UNAVAILABLE."""
    if outcome in ("denied",):
        return APPROVAL_REJECTED
    if outcome in ("timeout", "cancelled"):
        return APPROVAL_CANCELLED
    if outcome in ("notify_failed", "unavailable"):
        return APPROVAL_UNAVAILABLE
    return APPROVAL_UNAVAILABLE


def structured_answer(decision: str, reason: str, scope: str = _SCOPE_ONCE) -> dict:
    """Validate the approve-surface's structured answer (B2a):
    ``{decision: "allow"|"deny", reason: str, scope: "once"}``.

    ``reason`` is REQUIRED — an empty reason raises ``ValueError`` (the
    surface must collect one; the answer is what lands in
    ``pending_approvals`` and the observation). Only the one-operation grant
    is in the contract: any scope but ``"once"`` raises ``ValueError``
    (pattern-persisting grants are the legacy channel's business)."""
    if decision not in _DECISIONS:
        raise ValueError(f"structured answer: decision must be one of {_DECISIONS}, got {decision!r}")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("structured answer: reason is required (a non-empty string)")
    if scope != _SCOPE_ONCE:
        raise ValueError(f"structured answer: only scope {_SCOPE_ONCE!r} is in the contract, got {scope!r}")
    return {"decision": decision, "reason": reason.strip(), "scope": _SCOPE_ONCE}


def error_meta(code: str, reason: str = "") -> dict:
    """The ``meta.error`` block that rides the tool result: the stable code in
    the structure (B2d), the reason alongside it when the surface gave one."""
    meta = {"error": {"code": code}}
    if reason:
        meta["error"]["reason"] = reason
    return meta


def ledger_trace(extra: dict, code: str, reason: str = "") -> dict:
    """Fold the B2 trace into a ledger row's ``extra_json`` payload (never
    mutates *extra*): the code is greppable, the reason travels with it."""
    trace = dict(extra or {})
    trace["approval_code"] = code
    if reason:
        trace["approval_reason"] = reason
    return trace
