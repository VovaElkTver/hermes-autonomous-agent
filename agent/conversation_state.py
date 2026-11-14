"""Conversation-state estimator (delivery-3 P1, speedup-v2): cheap depth tracking.

A per-session state file under <runs_dir>/state/ holds {phase, goal, active_plan_ref,
depth, updated_at}. Admission consults it via :func:`route_with_state` BEFORE the
code-only text router; the router verdict then adjusts depth with hysteresis
(raise needs fresh explicit signals; lower is conservative). No LLM anywhere.

Design decisions ratified by the owner 14.11:
- storage: a tiny per-session FILE (survives gateway restarts, observable), not in-process;
- P1b LLM advisory selector: OUT OF SCOPE - state modulates the existing code-only router;
- acceptance R2: scripted trivial-vs-work dialog asserting different routes/timings.
"""

from __future__ import annotations

import json
import os
import time

from agent.planning_router import classify_route

_STATE_DIRNAME = "state"
# Depth semantics: 0 no planning; 1 single-pass sketch; 2+ full conveyor.
_DEPTH_FULL = 2
_VALID_DEPTHS = (0, 1, 2, 3, 4, 5)
# Router reasons that clearly signal work -> can raise depth (fresh-signal rule).
_RAISE_REASONS = {"multi_path", "imperative_with_path", "multistep_marker",
                  "explicit_plan_request", "long_request"}
# A quiet tail of chat must not keep the session "hot" forever.
_STALE_SECONDS = 3600.0


def _state_path(runs_dir: str, session_key: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in (session_key or "default"))
    return os.path.join(runs_dir, _STATE_DIRNAME, f"{safe}.json")


def load_state(runs_dir: str, session_key: str) -> dict:
    """Read the session state; best-effort by design - missing/corrupt = idle defaults."""
    try:
        with open(_state_path(runs_dir, session_key), encoding="utf-8") as fh:
            st = json.load(fh)
        if isinstance(st, dict):
            if int(st.get("depth", 0)) not in _VALID_DEPTHS or int(st.get("depth", 0)) and \
                    time.time() - float(st.get("updated_at", 0)) > _STALE_SECONDS:
                # stale non-zero depth decays to sketch-level, never blocks cheap turns
                st["depth"] = min(int(st.get("depth", 0)), 1)
            return st
    except Exception:
        pass
    return {"phase": "idle", "goal": "", "active_plan_ref": "", "depth": 0,
            "updated_at": 0.0, "signals": []}


def save_state(runs_dir: str, session_key: str, state: dict) -> None:
    """Best-effort atomic write; failure never breaks the turn."""
    try:
        path = _state_path(runs_dir, session_key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"phase": state.get("phase", "idle"),
                       "goal": str(state.get("goal", ""))[:300],
                       "active_plan_ref": state.get("active_plan_ref", ""),
                       "depth": max(0, min(5, int(state.get("depth", 0)))),
                       # honour a backdated updated_at (tests / migrated state): only set
                       # it to NOW when the caller did not bring its own timestamp.
                       "updated_at": float(state.get("updated_at") or time.time()),
                       "signals": list(state.get("signals", [])[-8:])},
                      fh, ensure_ascii=False)
        os.replace(tmp, path)  # atomic on POSIX
    except Exception:
        pass


def _persist(runs_dir: str, session_key: str, st: dict, *, depth: int) -> None:
    """Write back the evolved depth (decay/raise) without touching the rest."""
    save_state(runs_dir, session_key, {**st, "depth": depth})


def route_with_state(message: str, runs_dir: str, session_key: str) -> tuple[str, str]:
    """Admission entry: combine cached conversation state + this message's text signals.

    Returns ``(route, reason)`` where route is ``direct`` | ``plan`` | ``continue``.
    Hysteresis: depth rises only on fresh explicit work signals from THIS message;
    it falls conservatively (one level at a time) when chatter persists.
    """
    st = load_state(runs_dir, session_key)
    route, reason, _confidence = classify_route(message)

    if reason == "explicit_plan_request":
        return "plan", reason  # owner override: /plan-like wording always plans

    depth = int(st.get("depth", 0))
    stale = time.time() - float(st.get("updated_at", 0)) > _STALE_SECONDS

    raise_hit = reason in _RAISE_REASONS or any(
        reason.startswith(r + "(") for r in _RAISE_REASONS if r.endswith("_path"))
    if route == "plan" and (raise_hit or reason.startswith("multi_path")):
        new_depth = min(5, max(depth, _DEPTH_FULL)) + (1 if depth >= _DEPTH_FULL else 0)
        return ("plan", f"{reason}:depth{new_depth}")
    if route == "direct":
        if depth <= 1 or stale:
            _persist(runs_dir, session_key, st, depth=1 if stale and depth > 1 else depth)
            return "direct", f"{reason}{'@stale' if stale else ''}"
        # in-flight task: trivial-looking follow-up may be a continuation -> ask for continue
        if st.get("active_plan_ref"):
            return "continue", f"{reason}:inflight"
        new_depth = max(0, depth - 1)  # conservative decay
        _persist(runs_dir, session_key, st, depth=new_depth)  # decay MUST stick across reads
        # below full-depth the turn is cheap again: only depth>=2 runs the conveyor
        return ("direct" if new_depth < _DEPTH_FULL else "plan", f"{reason}:decay{new_depth}")
    # plan-route without raising reasons keeps current depth (grey zone)
    if depth >= _DEPTH_FULL or st.get("active_plan_ref"):
        return "continue" if st.get("active_plan_ref") else "plan", f"{reason}:keep"
    return "plan", f"{reason}:grey"
