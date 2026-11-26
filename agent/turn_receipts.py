"""Turn receipts: «done — это квитанция, не слова модели» (S1, TZ-memdoor-5090-v2).

Donor mechanics: ``gateway/turn_done.go`` (turnReceipts) + DECIDE.md turn_done section,
borrowed as pure accounting — no LLM, no new service. A turn's completion is a *receipt*
about what was actually checked after the last change, not a claim by the model.

Feature is **OFF by default** (``turn_receipts.enabled`` / ``HERMES_TURN_RECEIPTS``).
When OFF the behavior is byte-for-byte identical to pre-S1 (no receipt line is produced
and nothing is attached to the turn result).

Receipts rules (from the donor, kept verbatim where the TZ requires it):
* read-back — a file just written, then read (``read_file`` / ``search_files`` /
  ``read_terminal``) and found — is that file's check.
* a command (bash/terminal/execute_code) that exits 0 is a passing check *only if its
  output agrees* (does not contradict what was claimed); a failing test is not a check.
* a test command that PASSes (pytest / go test / …) is a named check.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

# Receipt line tails (donor ``receiptDone`` / ``receiptUnverified``).
RECEIPT_DONE_PREFIX = "✓ checked: "
RECEIPT_UNVERIFIED_PREFIX = "Recap: unverified: "
_UNVERIFIED_TAIL = "nothing run after last change"

# Tool classes.
_FILE_MUTATING = frozenset({"write_file", "patch"})
_COMMAND = frozenset({"terminal", "execute_code", "bash", "shell", "run_command"})
_READBACK = frozenset({"read_file", "search_files", "read_terminal"})

# Test / check commands detected inside a command's text.
_TEST_CMD = re.compile(
    r"\b(pytest|go\s+test|npm\s+test|jest|mocha|cargo\s+test|make\s+test|ctest|phpunit)\b",
    re.IGNORECASE,
)
_PASS_MARK = re.compile(
    r"\b(PASS|PASSED|OK\b|ok\b|0 failed|all tests passed|tests? (?:ran )?(?:passed|ok))\b",
    re.IGNORECASE,
)
_FAIL_MARK = re.compile(
    r"\b(FAIL|FAILED|ERROR\b|error:|Traceback|Exception|non-zero exit|exit code [1-9])\b",
    re.IGNORECASE,
)

# Argument keys that carry the target path, per tool family.
_PATH_KEYS = ("path", "file_path", "target", "file")


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------
def turn_receipts_enabled() -> bool:
    """``turn_receipts.enabled`` (default **False**) / ``HERMES_TURN_RECEIPTS``.

    The env override is read on every call and is never cached; the config value
    defaults to False so OFF is the safe pre-S1 behavior."""
    env = os.environ.get("HERMES_TURN_RECEIPTS")
    if env is not None:
        return env.strip().lower() not in {"0", "false", "no", "off"}
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
    except Exception:
        return False
    val = (cfg.get("turn_receipts") or {}).get("enabled")
    return bool(val) if val is not None else False


# ---------------------------------------------------------------------------
# Turn event extraction
# ---------------------------------------------------------------------------
def _call_id(tc: Any) -> str:
    """Effective call id of a stored tool_call dict (``call_id`` or ``id``); bridge ids
    may be ``call_id|response_item_id`` — the first half is authoritative."""
    for key in ("call_id", "id", "tool_call_id"):
        raw = tc.get(key) if isinstance(tc, dict) else getattr(tc, key, None)
        if isinstance(raw, str) and raw.strip():
            return raw.split("|", 1)[0].strip()
    return ""


def _parse_args(args: Any) -> dict:
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _last_user_index(messages: list) -> int:
    idx = -1
    for i, m in enumerate(messages):
        if isinstance(m, dict) and m.get("role") == "user":
            idx = i
    return idx


def _result_succeeded(name: Optional[str], content: Any) -> bool:
    """Cheap success signal from a tool result content (no LLM). A result that looks
    like an error/failure is not a success; everything else is treated as not-contradicting."""
    text = content if isinstance(content, str) else (
        json.dumps(content, ensure_ascii=False) if content is not None else ""
    )
    text = str(text)
    low = text.lstrip()
    if low.startswith("Error:") or low.startswith("error:") or '"error"' in text[:80]:
        return False
    if _FAIL_MARK.search(text):
        # A failing run is not a passing check.
        if name in _COMMAND:
            return False
    return True


def _target_paths(name: Optional[str], args: dict, content: Any) -> set[str]:
    """Candidate target paths of a mutating/read event, from its args (best-effort)."""
    if not isinstance(args, dict):
        return set()
    found: set[str] = set()
    for key in _PATH_KEYS:
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            found.add(val.strip())
    # write_file/patch: ``path`` / ``file_path``. read_file/search_files: ``path``.
    return found


def _turn_tool_events(messages: list) -> list[dict]:
    """The current turn's tool records as ``(name, args, content, succeeded, index)``.

    Scoped to after the last user message (the one that started this turn). Pairs
    assistant ``tool_calls`` with their ``role:"tool"`` results by call id so each
    event carries its arguments (needed for read-back / mutation-target matching)."""
    if not isinstance(messages, list):
        return []
    start = _last_user_index(messages)
    pending: dict[str, tuple] = {}
    events: list[dict] = []
    for i in range(start + 1, len(messages)):
        m = messages[i]
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "assistant":
            for tc in (m.get("tool_calls") or []):
                fn = (tc or {}).get("function") or {} if isinstance(tc, dict) else {}
                name = fn.get("name")
                args = _parse_args(fn.get("arguments"))
                pending[_call_id(tc)] = (name, args)
        elif role == "tool":
            cid = str(m.get("tool_call_id") or "")
            name, args = pending.get(cid, (m.get("tool_name") or m.get("name"), {}))
            content = m.get("content")
            events.append({
                "name": name,
                "args": args,
                "content": content,
                "index": i,
                "succeeded": _result_succeeded(name, content),
            })
    return events


# ---------------------------------------------------------------------------
# Receipt computation (pure over events — trivially seedable in tests)
# ---------------------------------------------------------------------------
def _is_mutation(name: Optional[str]) -> bool:
    return name in _FILE_MUTATING


def _check_label(name: Optional[str], args: dict, content: Any, succeeded: bool,
                 mutation_paths: set[str]) -> Optional[str]:
    """What check (if any) this event constitutes, or None."""
    if name in _COMMAND:
        if not succeeded:
            return None
        text = content if isinstance(content, str) else str(content)
        # Command text: terminal/execute_code carry it under a small set of keys.
        cmd = ""
        if isinstance(args, dict):
            cmd = str(args.get("command") or args.get("code") or args.get("cmd") or "")
        if _TEST_CMD.search(cmd):
            # A test run is a check only when it PASSes (output agrees).
            if _PASS_MARK.search(text) and not _FAIL_MARK.search(text):
                m = _TEST_CMD.search(cmd)
                label = (m.group(0) if m else "test").strip()
                return f"{label} PASS"
            return None  # a failing test is not a check
        # Generic command: exit-0 / non-contradicting = a check.
        return "command exit 0"
    if name in _READBACK:
        if not succeeded:
            return None
        paths = _target_paths(name, args, content)
        # read-back counts as a check only when it re-reads a path this turn mutated.
        if paths and paths & mutation_paths:
            return "read-back"
        return None
    return None


def compute_turn_receipts(events: Optional[list[dict]]) -> Optional[str]:
    """Pure receipt over turn events. None when the turn made no mutating change
    (nothing to verify)."""
    events = [e for e in (events or []) if isinstance(e, dict)]
    if not events:
        return None
    mutation_idxs = [e["index"] for e in events if _is_mutation(e.get("name"))]
    if not mutation_idxs:
        return None
    last_mut = max(mutation_idxs)
    # Distinct files changed this turn (args-derived; fall back to event count).
    mutation_paths: set[str] = set()
    for e in events:
        if _is_mutation(e.get("name")):
            mutation_paths |= _target_paths(e.get("name"), e.get("args") or {}, e.get("content"))
    if not mutation_paths:
        files_changed = len(mutation_idxs)
    else:
        files_changed = len(mutation_paths)

    checks: list[str] = []
    for e in events:
        if e["index"] <= last_mut:
            continue
        label = _check_label(e.get("name"), e.get("args") or {}, e.get("content"),
                             e.get("succeeded", True), mutation_paths)
        if label and label not in checks:
            checks.append(label)

    if checks:
        detail = f"{files_changed} file{'s' if files_changed != 1 else ''} changed"
        return RECEIPT_DONE_PREFIX + " · ".join([detail, *checks])
    return RECEIPT_UNVERIFIED_PREFIX + _UNVERIFIED_TAIL


def turn_receipts_line(messages: list) -> Optional[str]:
    """Public entry: compute the current turn's receipt line from the transcript.
    Fail-open — any unexpected error yields no receipt (OFF-equivalent)."""
    try:
        return compute_turn_receipts(_turn_tool_events(messages))
    except Exception:
        return None
