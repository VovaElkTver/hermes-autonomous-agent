"""M3 (TZ-memdoor-5090-v2): turn-progress shadow — stuck detection that first
only watches (donor gateway/route_shadow.go:37-50 + turn_verdict.go:134-165).

Phase 1 (``turn_shadow.enabled``): every API-call iteration scores the last
``WINDOW`` tool calls deterministically — identical repeats, repeated failures,
read pressure without edits — and appends one JSONL line to
``~/.hermes/logs/turn_shadow.jsonl`` when the score is non-zero. Nothing else
changes; their calibration is healthy≈0.27 vs failing≈0.96, and we must watch
our own distribution before believing any threshold.

Phase 2 (``turn_shadow.wrap_up_enabled``, off until phase 1 has data): when a
run scores ≥ FIRE, the next iteration gets an injected wrap-up directive
(«what is done / what failed / what is next — no tools») through the same
steer-injection channel the run-budget notice uses. Strict tool-stripping of
that round is a follow-up once the directive is calibrated.

With ``turn_shadow`` off the module writes nothing and the iteration prep is
byte-identical to before.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any

WINDOW = 8                 # donor: score over the last 8 tool calls
IDENTICAL_ALONE_FIRE = 5   # 5 identical calls in a row: stuck, no debate
IDENTICAL_RATIO = 6        # 6 matching calls inside the window: stuck loop
FAIL_REPEAT = 3            # same call failing 3+ times: flailing
READS_NO_EDITS = 30        # donor: explore-question nudge cadence
FIRE_SCORE = 0.8           # donor: P≥0.8 → is this run stuck…?
_EDIT_TOOLS = {"patch", "write_file", "execute_code", "skill_manage",
               "task_queue", "memory", "sqlite_memory"}
_READ_TOOLS = {"read_file", "search_files", "web_extract", "web_search",
               "skill_view", "session_search"}


def turn_shadow_enabled() -> bool:
    env = os.environ.get("HERMES_TURN_SHADOW")
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "on")
    try:
        from hermes_cli.config import load_config
        cfg = load_config() or {}
        return bool((cfg.get("turn_shadow") or {}).get("enabled"))
    except Exception:
        return False


def wrap_up_enabled() -> bool:
    env = os.environ.get("HERMES_TURN_WRAPUP")
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "on")
    try:
        from hermes_cli.config import load_config
        cfg = load_config() or {}
        return bool((cfg.get("turn_shadow") or {}).get("wrap_up_enabled"))
    except Exception:
        return False


def _fingerprint(name: str, args: Any) -> str:
    try:
        blob = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        blob = str(args)
    return hashlib.sha256(f"{name}\x00{blob}".encode()).hexdigest()[:16]


def recent_calls(messages: list, cap: int = 200) -> list[dict]:
    """Chronological [{name, fp, ok, kind}] from assistant tool_calls rows,
    paired with their tool-result rows for the ok flag."""
    out: list[dict] = []
    ok_by_id: dict[str, bool] = {}
    for m in messages:
        if m.get("role") == "tool" and m.get("tool_call_id"):
            content = str(m.get("content") or "")
            ok_by_id[m["tool_call_id"]] = not (
                content.lstrip().startswith(("Error", "error", "[error]"))
                or '"error"' in content[:200])
    tail = messages[-cap:]
    for m in tail:
        for tc in (m.get("tool_calls") or []):
            fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
            name = str(fn.get("name") or "")
            if not name:
                continue
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except (TypeError, ValueError):
                args = {"raw": fn.get("arguments")}
            kind = ("edit" if name in _EDIT_TOOLS
                    else "read" if name in _READ_TOOLS else "other")
            out.append({"name": name, "fp": _fingerprint(name, args),
                        "ok": ok_by_id.get(tc.get("id") or "", True),
                        "kind": kind})
    return out[-WINDOW:]


def score_calls(calls: list[dict]) -> dict:
    """Deterministic progress score in [0,1] over the window."""
    if not calls:
        return {"score": 0.0, "identical": 0, "fail_run": 0,
                "reads_no_edits": False}
    # identical run: longest suffix of the same fingerprint
    identical = 1
    for i in range(len(calls) - 2, -1, -1):
        if calls[i]["fp"] == calls[-1]["fp"]:
            identical += 1
        else:
            break
    same_in_window = sum(1 for c in calls if c["fp"] == calls[-1]["fp"])
    fail_run = 0
    for c in reversed(calls):
        if c["fp"] == calls[-1]["fp"] and not c["ok"]:
            fail_run += 1
        else:
            break
    edits = any(c["kind"] == "edit" for c in calls)
    reads = sum(1 for c in calls if c["kind"] == "read")
    reads_no_edits = (not edits) and reads >= min(READS_NO_EDITS, len(calls)) \
        and len(calls) == WINDOW
    score = 0.0
    if identical >= IDENTICAL_ALONE_FIRE:
        score = 0.9
    elif same_in_window >= IDENTICAL_RATIO:
        score = 0.85
    elif identical >= 3:
        score = 0.2 * identical
    if fail_run >= FAIL_REPEAT:
        score = max(score, 0.85)
    if reads_no_edits and score < 0.8:
        score = max(score, 0.5)  # explore-pressure: visible, not a fire alone
    return {"score": round(min(score, 1.0), 3), "identical": identical,
            "fail_run": fail_run, "reads_no_edits": reads_no_edits}


def _log_path():
    from hermes_constants import get_hermes_home
    d = get_hermes_home() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d / "turn_shadow.jsonl"


def observe(agent: Any, messages: list, api_call_count: Any) -> dict | None:
    """One iteration's shadow record. Returns the verdict dict (used by tests
    and phase 2) or None when the iteration scored zero. NEVER raises."""
    try:
        calls = recent_calls(messages or [])
        v = score_calls(calls)
        would = v["score"] >= FIRE_SCORE
        if v["score"] > 0:
            line = {"ts": time.time(), "session": getattr(agent, "session_id", None),
                    "iter": api_call_count, **v, "would_wrap_up": would}
            with open(_log_path(), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
            return line
        return None
    except Exception:
        return None


WRAP_UP_DIRECTIVE = (
    "[wrap-up] Your run looks stuck (repeated identical work without "
    "progress). Do NOT call tools. Answer in text: what is done, what "
    "failed, and what the next concrete step is — then stop.")


def maybe_inject_wrap_up(agent: Any, messages: list) -> bool:
    """Phase 2: once per turn, when the shadow fired, append the wrap-up
    directive as a fresh user row (same channel as /steer). Flag-off and
    already-fired: False, messages untouched."""
    if not wrap_up_enabled() or getattr(agent, "_turn_shadow_wrapped", False):
        return False
    try:
        v = score_calls(recent_calls(messages or []))
    except Exception:
        return False
    if v["score"] < FIRE_SCORE:
        return False
    agent._turn_shadow_wrapped = True
    from agent.turn_iteration_prep import _inject_steer_after_newest_tool_result
    _inject_steer_after_newest_tool_result(agent, messages, WRAP_UP_DIRECTIVE)
    return True
