"""Stepped iteration-budget checkpoint notices (A3, donor BUDGET_NOTICE_THRESHOLDS).

Flag OFF (empty ``agent.budget_notice_thresholds``) keeps the one-shot
behaviour byte-for-byte; a non-empty list fires a progress-check at the first
threshold crossed and a wrap-up at each later one, each at most once.
"""
import os

from agent.iteration_budget import (
    IterationBudget,
    normalize_budget_notice_thresholds,
)
from agent.turn_iteration_prep import (
    ITERATION_BUDGET_WARNING_TEMPLATE,
    ITERATION_BUDGET_WRAPUP_TEMPLATE,
    _maybe_inject_iteration_budget_warning,
)


class _FakeAgent:
    """Just the attributes the budget-notice path reads."""

    def __init__(self, max_total=10, thresholds=None, ratio=None):
        self._interrupt_requested = False
        self.iteration_budget = IterationBudget(max_total)
        self.budget_notice_thresholds = list(thresholds or [])
        self.budget_warning_ratio = ratio
        self.valid_tool_names = ()
        self._budget_notice_fired = set()
        self._iteration_budget_warning_injected = False

    def consume_to(self, n):
        for _ in range(n - self.iteration_budget.used):
            self.iteration_budget.consume()


def _messages():
    return [
        {"role": "user", "content": "work"},
        {"role": "assistant",
         "tool_calls": [{"id": "t0", "type": "function",
                          "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t0", "content": "result"},
    ]


# --- the normalizer ----------------------------------------------------------
def test_normalize_thresholds_sorts_dedupes_and_filters():
    assert normalize_budget_notice_thresholds(None) == []
    assert normalize_budget_notice_thresholds([]) == []
    assert normalize_budget_notice_thresholds([0.8, 0.5]) == [0.5, 0.8]
    assert normalize_budget_notice_thresholds([0.5, 0.5, 0.8]) == [0.5, 0.8]
    # out-of-range / non-finite / non-numeric / bools are dropped
    assert normalize_budget_notice_thresholds([0, 1, -1, 1.5, float("nan"),
                                               float("inf"), "x", True, 0.5]) == [0.5]
    # a bare scalar is a one-element list
    assert normalize_budget_notice_thresholds(0.5) == [0.5]
    # a bare string is not a threshold list
    assert normalize_budget_notice_thresholds("0.5") == []


# --- stepped sequence (ON) ---------------------------------------------------
def test_stepped_sequence_fires_progress_then_wrapup_once_each():
    agent = _FakeAgent(max_total=10, thresholds=[0.5, 0.8])
    messages = _messages()

    # below the first threshold: nothing
    agent.consume_to(4)
    assert _maybe_inject_iteration_budget_warning(agent, messages) is False
    assert messages[-1]["content"] == "result"

    # cross 0.5 (5/10): progress-check notice, not the wrap-up
    agent.consume_to(5)
    assert _maybe_inject_iteration_budget_warning(agent, messages) is True
    tail = messages[-1]["content"]
    assert ITERATION_BUDGET_WARNING_TEMPLATE.format(used=5, maximum=10) in tail
    assert ITERATION_BUDGET_WRAPUP_TEMPLATE.format(used=5, maximum=10) not in tail

    # re-fire on the same call does not double-inject
    assert _maybe_inject_iteration_budget_warning(agent, messages) is False
    assert tail.count(ITERATION_BUDGET_WARNING_TEMPLATE.format(used=5, maximum=10)) == 1

    # cross 0.8 (8/10): wrap-up notice
    agent.consume_to(8)
    assert _maybe_inject_iteration_budget_warning(agent, messages) is True
    tail = messages[-1]["content"]
    assert ITERATION_BUDGET_WRAPUP_TEMPLATE.format(used=8, maximum=10) in tail

    # still no re-fire
    assert _maybe_inject_iteration_budget_warning(agent, messages) is False


def test_stepped_notices_rearm_when_latch_cleared():
    agent = _FakeAgent(max_total=10, thresholds=[0.5, 0.8])
    messages = _messages()
    agent.consume_to(5)
    _maybe_inject_iteration_budget_warning(agent, messages)
    # simulate the per-turn re-arm (turn_context._reset_per_turn_agent_state)
    agent._budget_notice_fired = set()
    assert _maybe_inject_iteration_budget_warning(agent, messages) is True


# --- OFF keeps the one-shot behaviour ---------------------------------------
def test_off_empty_thresholds_uses_oneshot():
    agent = _FakeAgent(max_total=10, thresholds=[], ratio=0.75)
    messages = _messages()
    agent.consume_to(7)   # 0.7 of 10, below the 0.75 ratio
    assert _maybe_inject_iteration_budget_warning(agent, messages) is False
    agent.consume_to(8)   # crosses 0.75
    assert _maybe_inject_iteration_budget_warning(agent, messages) is True
    assert ITERATION_BUDGET_WARNING_TEMPLATE.format(used=8, maximum=10) in \
        messages[-1]["content"]
    assert agent._iteration_budget_warning_injected is True
    # one-shot: never re-fires within the turn
    assert _maybe_inject_iteration_budget_warning(agent, messages) is False


def test_off_no_ratio_no_notice():
    agent = _FakeAgent(max_total=10, thresholds=[], ratio=None)
    messages = _messages()
    agent.consume_to(9)
    assert _maybe_inject_iteration_budget_warning(agent, messages) is False
    assert messages[-1]["content"] == "result"
