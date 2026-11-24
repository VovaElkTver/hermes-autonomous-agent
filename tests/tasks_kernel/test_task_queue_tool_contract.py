"""Regression: task_queue must survive the registry result contract.

tool_result_contract v2 (tools/registry.py) rejects handler results that are plain
dicts — the kernel function returning a dict was fine for ~all kernel tests (they
call task_queue() directly) while the DISPATCHED tool died on every action since
9e0bd2ac19e. This test goes through the registered handler, the exact shape the
gateway executes."""
import json

import tools.task_tools  # registers task_queue
from tools.registry import registry


def _handler():
    entry = registry.get_entry("task_queue")
    assert entry is not None, "task_queue must stay registered"
    return entry["handler"] if isinstance(entry, dict) else entry.handler


def test_schema_reaches_registry_as_parameters():
    # registry.py converts schema["parameters"]; a foreign key ("input_schema")
    # yields empty properties and the runtime strips even 'action' from calls.
    entry = registry.get_entry("task_queue")
    schema = getattr(entry, "schema", None) or {}
    params = schema.get("parameters") or {}
    assert "action" in (params.get("properties") or {}), "properties must survive registration"
    assert params.get("required") == ["action"]


def test_dispatch_shape_is_json_string():
    h = _handler()
    out = h({"action": "no-such-action"}, session_id="s")
    assert isinstance(out, str), f"registry contract wants str, got {type(out).__name__}"
    payload = json.loads(out)
    assert payload["ok"] is False and "no-such-action" in payload["error"]


def test_status_shape_is_json_string(tmp_path, monkeypatch):
    # status needs a chat_id; unknown queue state reads as empty, shape is the point
    h = _handler()
    out = h({"action": "status", "chat_id": "regression-chat"}, session_id="s")
    assert isinstance(out, str)
    assert json.loads(out)["ok"] is True
