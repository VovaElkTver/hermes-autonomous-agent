"""S1 (TZ-memdoor-5090-v2): turn receipts — «done — это квитанция, не слова модели».

Acceptance (TZ): a seeded turn —
  * edit без проверки            -> ``unverified``
  * edit + pytest PASS          -> ``✓ checked``
  * edit + read-back            -> checked, with the read-back mark
and the feature flag OFF -> no receipt line (byte-identical to pre-S1).

The pure core (``compute_turn_receipts`` over events) is seeded directly; the
transcript path (``turn_receipts_line``) is driven by realistic message dicts so
call-id pairing and turn scoping are covered too.
"""
from __future__ import annotations

import json

import pytest

from agent.turn_receipts import (
    RECEIPT_DONE_PREFIX,
    RECEIPT_UNVERIFIED_PREFIX,
    _turn_tool_events,
    compute_turn_receipts,
    turn_receipts_enabled,
    turn_receipts_line,
)


def _msg_user(text="do the thing"):
    return {"role": "user", "content": text}


def _assistant_with_calls(calls):
    """``calls`` = [(call_id, name, args_dict)] -> an assistant tool-call message."""
    tool_calls = []
    for cid, name, args in calls:
        tool_calls.append({
            "id": cid,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args) if isinstance(args, dict) else args},
        })
    return {"role": "assistant", "content": "", "tool_calls": tool_calls}


def _tool_result(cid, name, content, *, tool_call_id=None):
    return {"role": "tool", "name": name, "tool_name": name,
            "tool_call_id": tool_call_id or cid, "content": content}


# ---------------------------------------------------------------------------
# Pure core
# ---------------------------------------------------------------------------
def _ev(name, content, *, index, succeeded=True, args=None, **extra):
    d = {"name": name, "args": args or {}, "content": content,
         "index": index, "succeeded": succeeded}
    d.update(extra)
    return d


def test_edit_without_check_is_unverified():
    events = [_ev("write_file", "wrote a.txt", index=1, args={"path": "a.txt"}),
              _ev("read_file", "unrelated", index=2, args={"path": "other.txt"})]
    line = compute_turn_receipts(events)
    assert line is not None
    assert line.startswith(RECEIPT_UNVERIFIED_PREFIX)
    assert "nothing run after last change" in line


def test_edit_then_pytest_pass_is_checked():
    events = [_ev("write_file", "wrote a.txt", index=1, args={"path": "a.txt"}),
              _ev("terminal", "============================= 3 passed in 0.12s =============================",
                   index=2, args={"command": "pytest tests/test_a.py"})]
    line = compute_turn_receipts(events)
    assert line is not None
    assert line.startswith(RECEIPT_DONE_PREFIX)
    assert "pytest PASS" in line
    assert "1 file changed" in line


def test_edit_then_gotest_pass_is_checked():
    events = [_ev("write_file", "ok", index=1, args={"path": "pkg/x.go"}),
              _ev("terminal", "ok pkg (cached)", index=2, args={"command": "go test ./..."})]
    line = compute_turn_receipts(events)
    assert line is not None
    assert line.startswith(RECEIPT_DONE_PREFIX)
    assert "go test PASS" in line


def test_failing_test_is_not_a_check():
    events = [_ev("write_file", "ok", index=1, args={"path": "a.py"}),
              _ev("terminal", "1 failed, 2 passed", index=2, args={"command": "pytest"})]
    line = compute_turn_receipts(events)
    assert line is not None
    # the failing test is not a passing check -> unverified
    assert line.startswith(RECEIPT_UNVERIFIED_PREFIX)


def test_read_back_of_mutated_path_is_a_check():
    events = [_ev("write_file", "ok", index=1, args={"path": "notes.md"}),
              _ev("read_file", "the new content", index=2, args={"path": "notes.md"})]
    line = compute_turn_receipts(events)
    assert line is not None
    assert line.startswith(RECEIPT_DONE_PREFIX)
    assert "read-back" in line


def test_read_back_of_unmutated_path_is_not_a_check():
    events = [_ev("write_file", "ok", index=1, args={"path": "a.txt"}),
              _ev("read_file", "found", index=2, args={"path": "b.txt"})]
    line = compute_turn_receipts(events)
    assert line is not None
    assert line.startswith(RECEIPT_UNVERIFIED_PREFIX)


def test_no_mutation_no_receipt():
    events = [_ev("read_file", "x", index=1, args={"path": "a.txt"}),
              _ev("terminal", "1 + 1 = 2", index=2, args={"command": "echo hi"})]
    assert compute_turn_receipts(events) is None


def test_empty_events_no_receipt():
    assert compute_turn_receipts([]) is None
    assert compute_turn_receipts(None) is None


def test_multiple_files_changed_count():
    events = [_ev("write_file", "a", index=1, args={"path": "a.txt"}),
              _ev("write_file", "b", index=2, args={"path": "b.txt"}),
              _ev("terminal", "PASS", index=3, args={"command": "pytest"})]
    line = compute_turn_receipts(events)
    assert line is not None
    assert "2 files changed" in line


# ---------------------------------------------------------------------------
# Transcript path (call-id pairing + turn scoping)
# ---------------------------------------------------------------------------
def test_turn_receipts_line_scoped_to_current_turn():
    # A PRIOR turn mutated + checked; the CURRENT turn did nothing. The receipt
    # must reflect the current turn only (no mutation -> None).
    messages = [
        _msg_user("prior request"),
        _assistant_with_calls([("c0", "write_file", {"path": "old.txt"})]),
        _tool_result("c0", "write_file", "ok"),
        _tool_result("c0", "write_file", "ok"),  # duplicate tolerated
        _msg_user("current request"),
        _assistant_with_calls([("c1", "read_file", {"path": "readme.md"})]),
        _tool_result("c1", "read_file", "the readme"),
    ]
    assert turn_receipts_line(messages) is None


def test_turn_receipts_line_edit_then_readback():
    messages = [
        _msg_user("make a note"),
        _assistant_with_calls([("c1", "write_file", {"path": "notes.md"})]),
        _tool_result("c1", "write_file", "wrote notes.md"),
        _assistant_with_calls([("c2", "read_file", {"path": "notes.md"})]),
        _tool_result("c2", "read_file", "hello in notes.md"),
    ]
    line = turn_receipts_line(messages)
    assert line is not None
    assert line.startswith(RECEIPT_DONE_PREFIX)
    assert "read-back" in line


def test_turn_tool_events_pairs_args_by_call_id():
    messages = [
        _msg_user("run it"),
        _assistant_with_calls([("t1", "terminal", {"command": "pytest"})]),
        _tool_result("t1", "terminal", "5 passed"),
    ]
    events = _turn_tool_events(messages)
    assert len(events) == 1
    assert events[0]["name"] == "terminal"
    assert events[0]["args"] == {"command": "pytest"}
    assert events[0]["content"] == "5 passed"


# ---------------------------------------------------------------------------
# Feature gate
# ---------------------------------------------------------------------------
def test_flag_off_by_default(monkeypatch):
    monkeypatch.delenv("HERMES_TURN_RECEIPTS", raising=False)
    # No config key -> default False. Patch the source module the function imports from.
    monkeypatch.setattr("hermes_cli.config.load_config", dict)
    assert turn_receipts_enabled() is False


def test_flag_on_via_env(monkeypatch):
    monkeypatch.setenv("HERMES_TURN_RECEIPTS", "true")
    assert turn_receipts_enabled() is True


def test_flag_off_via_env(monkeypatch):
    monkeypatch.setenv("HERMES_TURN_RECEIPTS", "off")
    assert turn_receipts_enabled() is False
