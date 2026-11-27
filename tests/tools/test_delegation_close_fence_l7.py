"""Tier L7 (TZ-tinyhumansai): delegated children cannot close tracked work.

The completion fence lives at the tool boundary: task_queue(skip/cancel) and
ctl_step_done refuse inside a delegated-child context; emit/report stay allowed."""

import importlib.util
import json
from pathlib import Path

from agent.delegation_context import delegated_child_context

R = Path(__file__).resolve().parents[2]


def _ctl_agent_tools():
    spec = importlib.util.spec_from_file_location(
        "ctl_agent_tools_under_test", R / "plugins" / "controller" / "agent_tools.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_task_queue_skip_fenced_for_child():
    from tools.task_tools import task_queue
    with delegated_child_context():
        out = task_queue({"action": "skip", "chat_id": "x", "task_id": "t"})
    assert out["ok"] is False
    assert "parent decides" in out["error"]


def test_task_queue_cancel_fenced_for_child():
    from tools.task_tools import task_queue
    with delegated_child_context():
        out = task_queue({"action": "cancel", "chat_id": "x", "task_id": "t"})
    assert out["ok"] is False
    assert "closes tracked work" in out["error"]


def test_task_queue_emit_not_fenced():
    # emit must reach the kernel path even in a child context (fence is close-only);
    # an unknown event id is an acceptable kernel answer — the point is no fence error.
    from tools.task_tools import task_queue
    with delegated_child_context():
        out = task_queue({"action": "emit", "chat_id": "x", "event_id": "no-such-event"})
    text = json.dumps(out)
    assert "closes tracked work" not in text


def test_ctl_step_done_fenced_for_child():
    mod = _ctl_agent_tools()
    with delegated_child_context():
        out = mod.ctl_step_done({"step_id": "s1", "status": "done"})
    assert "parent-only" in out
