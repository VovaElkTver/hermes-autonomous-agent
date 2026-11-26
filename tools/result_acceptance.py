"""M2 (TZ-memdoor-5090-v2): result-acceptance for subagents — semantics before
the announcement (donor gateway/result_acceptance.go:25-56).

Disease: our child acceptance is schema-shaped — a bare claim, a plan, a
question back, or an error passes ``schema_valid`` and the parent's "done"
becomes a lie. The donor asks one yes/no per child result: *is it unfinished?*
The first unfinished result is handed **back** to the same child session once
(the run is never handed back twice); the second result is always announced,
but with the verdict attached so the requester never reports it as done
unknowingly.

Until the L1 decision server exists the judge is deterministic markers
(plan-phrasing / question / empty prose / bare claim **and** no verifiable
handle). A model judge replaces this without touching the wiring.

OFF by default (``result_acceptance.enabled`` / ``HERMES_RESULT_ACCEPTANCE``):
no judgement, no extra keys, no rework — entries byte-identical to before.
"""

from __future__ import annotations

import os
import re
from typing import Any

_HANDBACK_NOTE = (
    "\n\n[acceptance handback] Your result arrived unfinished ({reason}). "
    "Complete the work NOW and return the real outcome: what was changed or "
    "produced (paths, URLs, ids, measurements) and the verdict against the "
    "goal. A plan, a bare claim, or a question back is not a result. This is "
    "the one and only handback."
)

# Deterministic markers (conservative: prose WITH a verifiable handle is finished).
_PLAN_RE = re.compile(
    r"^\s*(I will|I'll|Now I (?:will|am going to)|Let me (?:start|first)|"
    r"План[:：]|Сейчас я (?:буду|сделаю)|Теперь я (?:буду|сделаю)|"
    r"Начну с|Собираюсь)\b", re.IGNORECASE | re.MULTILINE)
_BARE_CLAIM_RE = re.compile(
    r"^\s*(done|готово|finished|выполнено|успешно|завершено|all (?:set|done))"
    r"\b[.!…\s]*$", re.IGNORECASE)
_MIN_SUBSTANTIVE_CHARS = 40
_HANDLE_RE = re.compile(
    r"(/[A-Za-z0-9._/-]{3,}|https?://|MEDIA:|[0-9a-f]{8,40}|"
    r"\bexit\s*(code)?\s*\d|\d+\s*(tests?|passed|failed|MB|GB|ms)\b)",
    re.IGNORECASE)


def result_acceptance_enabled() -> bool:
    """``result_acceptance.enabled`` (default **False**) / ``HERMES_RESULT_ACCEPTANCE``."""
    env = os.environ.get("HERMES_RESULT_ACCEPTANCE")
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "on")
    try:
        from hermes_cli.config import load_config
        cfg = load_config() or {}
        val = (cfg.get("result_acceptance") or {}).get("enabled")
        return bool(val) if val is not None else False
    except Exception:
        return False


def judge(entry: dict, goal: str = "") -> str | None:
    """Return the unfinished-reason, or None when the result reads finished.
    Only *completed* entries are judged: failed/interrupted are the host's own
    structured failure classes, announced truthfully already."""
    if entry.get("status") != "completed":
        return None
    summary = str(entry.get("summary") or "").strip()
    trace = entry.get("tool_trace") or []
    has_handle = bool(_HANDLE_RE.search(summary)) or bool(trace)
    if not summary:
        return "empty summary"
    if summary.endswith("?"):
        return "question back instead of a result"
    if _BARE_CLAIM_RE.match(summary) and not has_handle:
        return "bare claim with nothing verifiable"
    if _PLAN_RE.search(summary) and not has_handle:
        return "plan announced instead of a result"
    if len(summary) < _MIN_SUBSTANTIVE_CHARS and not has_handle:
        return "summary too thin to be a result"
    return None


def rework_child(entry: dict, task: dict, child: Any, parent_agent: Any,
                 reason: str) -> bool:
    """One handback into the SAME child session (its conversation is the state
    we resume on). Returns True when the child produced a second result.
    Mirrors delegate_tool's own call shape; import is late to avoid the
    delegate_tool <-> delegate_tool_results cycle."""
    from tools.delegate_tool import _run_single_child
    note = _HANDBACK_NOTE.format(reason=reason)
    second = _run_single_child(entry.get("task_index", -1),
                               str(task.get("goal") or "") + note,
                               child, parent_agent)
    if not isinstance(second, dict):
        return False
    for key in ("summary", "status", "exit_reason", "truncated", "tool_trace",
                "duration_seconds"):
        if key in second:
            entry[key] = second[key]
    entry["acceptance_reworked"] = True
    return True


def apply_acceptance(results: list[dict], task_list: list[dict],
                     child_by_index: dict, parent_agent) -> None:
    """In-place, BEFORE budgeting/redaction so the verdict rides the final
    summary. Every finished result gets nothing extra; an unfinished one gets
    acceptance='unfinished' after the single handback (or without it when no
    child object is reachable — then the verdict at least reaches the parent)."""
    if not result_acceptance_enabled():
        return
    for entry in results:
        idx = entry.get("task_index", -1)
        reason = judge(entry)
        if reason is None:
            continue
        entry.setdefault("task_index", idx)
        task = task_list[idx] if 0 <= idx < len(task_list) else {}
        child = child_by_index.get(idx)
        handed_back = False
        if child is not None and not entry.get("acceptance_reworked"):
            # the run is never handed back twice: the marker is on the entry,
            # and a rework that itself lands unfinished is ANNOUNCED, not retried
            try:
                handed_back = rework_child(entry, task, child, parent_agent, reason)
            except Exception:
                handed_back = False
        reason = judge(entry) if handed_back else reason
        if reason is not None:
            entry["acceptance"] = "unfinished"
            entry["acceptance_reason"] = reason
