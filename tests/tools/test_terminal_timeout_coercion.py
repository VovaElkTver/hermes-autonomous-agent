"""Stringified ``timeout`` must never TypeError the planner (2026-11-04).

A session sent ``timeout="14400"`` (providers stringify integer schema
fields); ``timeout <= 0`` in ``_plan_execution`` raised
``TypeError: '<=' not supported between instances of 'str' and 'int'``,
which surfaced as the generic crash envelope ("Failed to execute
command") instead of a clean rejection — and the caller retried blind.
The coercion helper accepts numeric strings and turns every other
unusable value into the standard rejection message.
"""

from __future__ import annotations

import json

import pytest

import tools.terminal_tool as terminal_module


# ---------- helper unit contract (hermetic, no env) ----------

@pytest.mark.parametrize("value, expected", [
    (None, (None, None)),
    (30, (30, None)),
    (14400.5, (14400.5, None)),
    ("14400", (14400, None)),
    ("  60 ", (60, None)),
    ("0.5", (0.5, None)),
])
def test_usable_values_pass_through(value, expected):
    assert terminal_module._coerce_timeout(value) == expected


@pytest.mark.parametrize("value", [
    "abc", "", "nan", "inf", "-1h", True, 0, -5, 0.0, object(),
])
def test_unusable_values_return_message(value):
    coerced, message = terminal_module._coerce_timeout(value)
    assert coerced is None
    assert message and "timeout" in message


def test_nonpositive_message_matches_legacy_wording():
    # Downstream tests and muscle memory depend on this exact phrasing.
    _, message = terminal_module._coerce_timeout(0)
    assert message == "timeout must be a positive number of seconds (got 0)."


# ---------- integration through the real planner ----------

def test_plan_execution_accepts_string_timeout():
    plan = terminal_module._plan_execution(
        "echo hi", task_id=None, timeout="14400", background=False, _host_local=True,
    )
    assert plan.effective_timeout == 14400


def test_plan_execution_rejects_garbage_timeout_with_clean_json():
    with pytest.raises(terminal_module._Rejected) as excinfo:
        terminal_module._plan_execution(
            "echo hi", task_id=None, timeout="10m", background=False, _host_local=True,
        )
    payload = json.loads(excinfo.value.result_json)
    assert "timeout must be a number of seconds" in payload["error"]
    assert "traceback" not in payload
