"""L2 (TZ-memdoor-5090-v2): per-task bounds on agent-registered periodics —
openhuman Contracts.swift semantics (times<=60, 12h expiry, no overlap,
self-remove), donor source NOT imported (contract only).

The carrier (``ctl_followup``) already fires at most ``max_repeats`` times and
sleeps (self-remove semantics, followups.py review-exhaustion path). This
module adds what the donor's TasksModel enforced and the carrier did not:

* ``times`` capped at 60 registrations;
* the whole horizon capped at an expiry window (default 12h) — a review's
  repeat count is lowered so its last fire stays inside the window, and a
  reminder due beyond the window is refused;
* no overlap: an identical open row (scope_task + kind + text) is refused with
  the existing followup_id.

OFF by default (``agent_periodic_bounds.enabled`` / ``HERMES_AGENT_PERIODIC_BOUNDS``):
the tool rejects the new args outright, so no flag-off caller can believe
bounds were applied when they were not.
"""

from __future__ import annotations

import os

MAX_TIMES = 60
DEFAULT_EXPIRY_S = 12 * 3600


def agent_periodic_bounds_enabled() -> bool:
    env = os.environ.get("HERMES_AGENT_PERIODIC_BOUNDS")
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "on")
    try:
        from hermes_cli.config import load_config
        cfg = load_config() or {}
        return bool((cfg.get("agent_periodic_bounds") or {}).get("enabled"))
    except Exception:
        return False


def validate_add(conn, *, kind: str, scope_task: str, text: str,
                 due_at, period_s, rrule, now: float,
                 times=None, expiry_h=None) -> dict:
    """Returns {max_repeats: int|None} or {error: str}. Pure validator — the
    caller owns the transaction."""
    from controller import followups

    if times is not None:
        try:
            times = int(times)
        except (TypeError, ValueError):
            return {"error": "times must be an integer"}
        if not 1 <= times <= MAX_TIMES:
            return {"error": f"times must be 1..{MAX_TIMES} (TZ-memdoor L2 cap)"}
    expiry_s = DEFAULT_EXPIRY_S if expiry_h is None else float(expiry_h) * 3600
    if expiry_s <= 0:
        return {"error": "expiry_h must be positive"}

    dup = conn.execute(
        "SELECT followup_id FROM ctl_followup WHERE state IN ('pending','awaiting_ack')"
        " AND scope_task=? AND kind=? AND text=? LIMIT 1",
        (scope_task, kind, text)).fetchone()
    if dup:
        return {"error": f"overlapping followup already open: {dup['followup_id']}"}

    if kind == "reminder":
        if due_at is None or float(due_at) > now + expiry_s:
            return {"error": f"reminder due beyond the {expiry_s / 3600:.1f}h expiry window"}
        return {"max_repeats": times}
    # review: keep the whole repeat chain inside the window
    if period_s:
        fits = max(1, int(expiry_s // int(period_s)))
        max_repeats = min(times or followups.DEFAULT_MAX_REPEATS, fits, MAX_TIMES)
        return {"max_repeats": max_repeats}
    if not rrule:
        return {"error": "review requires period_s or rrule"}
    if due_at is not None and float(due_at) > now + expiry_s:
        return {"error": "first rrule occurrence beyond the expiry window"}
    return {"max_repeats": min(times, MAX_TIMES) if times is not None else None}
