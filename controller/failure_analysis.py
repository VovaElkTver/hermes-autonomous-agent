"""controller.failure_analysis — LLM-analyzed recovery on repeated task failure.

Contract donor: camel-ai/camel RecoveryStrategy/FailureHandlingConfig
(Apache-2.0, STEAL-PLAN item 3), adapted to OUR native transition edges instead
of their in-memory worker pool:

    retry_same_step   — transient cause, worker re-dispatches as-is
    revise_step_brief — plan-level fix: brief rewritten, step returns to pending
    split_task        — scope too large: decompose before the next attempt
    park_needs_human  — owner context required; nothing autonomous to try

Design constraints (Вл rules):
- default OFF via top-level config flag ``failure_analysis`` (like tool_trace);
- fail-open and log-only: any analyzer error never blocks the durable
  failed-transition that already happened;
- the LLM only chooses among edges the controller already has; unknown or
  disabled recommendations clamp to ``retry_same_step`` (camel's clamp, but
  logged instead of silent).
"""

from __future__ import annotations

import json
import urllib.request

from .model import emit as _emit  # ctl_event writer (same sink as tool_trace)

STRATEGIES = ("retry_same_step", "revise_step_brief", "split_task", "park_needs_human")

_TRIGGER_ATTEMPT = 2          # analyze from the second failure of a task on


def enabled() -> bool:
    try:
        from hermes_cli.config import load_config
        return bool(load_config().get("failure_analysis", False))
    except Exception:
        return False


def _expert_endpoint() -> tuple[str, str]:
    """auxiliary.expert endpoint (same judge channel as gateway critic)."""
    from hermes_cli.config import load_config
    aux = load_config().get("auxiliary") or {}
    exp = aux.get("expert") or {}
    return exp.get("base_url") or "", exp.get("model") or ""


def analyze_failure(task_goal: str, step_summary: str,
                    attempt: int, error_tail: str) -> dict | None:
    """Ask the expert model to classify a repeated failure. Never raises."""
    if not enabled() or attempt < _TRIGGER_ATTEMPT:
        return None
    base, model_name = _expert_endpoint()
    if not (base and model_name):
        return None
    prompt = (
        "Controller task failed repeatedly. Classify the recovery.\n"
        f"Task goal: {task_goal!r}\n"
        f"Step: {step_summary!r}\nAttempt №{attempt}; last error tail:\n"
        f"{error_tail[-1500:]!r}\n\n"
        "Pick exactly one strategy:\n"
        "retry_same_step — transient/environmental cause;\n"
        "revise_step_brief — the step brief itself is wrong/ambiguous (provide corrected text);\n"
        "split_task — too much scope for one step;\n"
        "park_needs_human — needs owner intent/context.\n\n"
        'Answer JSON only: {"strategy": "...", "reasoning": "...", '
        '"modified_brief": "...|null"}'
    )
    try:
        req = urllib.request.Request(
            base.rstrip("/") + "/chat/completions",
            data=json.dumps({"model": model_name, "max_tokens": 2000,
                             "messages": [{"role": "user", "content": prompt}]}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=90) as resp:
            answer = json.loads(resp.read())
        txt = (answer["choices"][0]["message"].get("content") or "").strip()
        obj, _ = json.JSONDecoder().raw_decode(txt)
        return {"strategy": clamp_strategy(obj.get("strategy")),
                "reasoning": str(obj.get("reasoning") or "")[:500],
                "modified_brief": obj.get("modified_brief")}
    except Exception as exc:                        # fail-open, always
        try:
            log_line(kind="failure_analysis_error", detail=str(exc)[:300])
        except Exception:
            pass
        return None


def clamp_strategy(value) -> str:
    """Unknown/disabled recommendations fall back to retry (camel's clamp, logged upstream)."""
    return value if value in STRATEGIES else "retry_same_step"


def apply(conn, *, task_id: str, run_id: str, attempt: int,
          task_goal: str, step_summary: str, error_tail: str) -> dict | None:
    """Post-transition hook: analyze + persist an observation. Returns analysis."""
    result = analyze_failure(task_goal, step_summary, attempt, error_tail)
    if result is None:
        return None
    log_line(kind="failure_analysis", task_id=task_id, run_id=run_id,
             strategy=result.get("strategy"))
    return result


def log_line(kind: str, **payload) -> None:
    """Best-effort ctl_event write through controller.model.emit."""
    try:
        conn = connect_state()
        _emit(conn, kind=kind, origin="failure_analysis", payload=payload)
    except Exception:
        pass


def connect_state():
    from . import model
    return model.connect()
