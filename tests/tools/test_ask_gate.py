"""S4 (TZ-memdoor-5090-v2): deterministic ask-gate acceptance.

Acceptance (TZ): "какой цвет кнопки?" passes; "leжит li konfig v ~/.x/y?" at 0
reads is refused with an instruction; a repeat passes. OFF → the gate is a no-op
and clarify is byte-for-byte unchanged.
"""

import json

import pytest

from tools import ask_gate


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    ask_gate._clear_cache()
    ask_gate._state.clear()
    ask_gate._current_turn = "default"
    monkeypatch.delenv("HERMES_ASK_GATE", raising=False)
    yield
    ask_gate._state.clear()
    ask_gate._current_turn = "default"
    ask_gate._clear_cache()


def _enable(monkeypatch):
    monkeypatch.setenv("HERMES_ASK_GATE", "1")
    ask_gate._clear_cache()


def test_off_gate_is_noop(monkeypatch):
    assert ask_gate.ask_gate_enabled() is False
    assert ask_gate.gate("t", "leжит li konfig v ~/.x/y?", seen_read_exec=False) is None


def test_color_question_passes(monkeypatch):
    _enable(monkeypatch)
    assert ask_gate.gate("t", "какой цвет кнопки?", seen_read_exec=False) is None


def test_factual_probe_zero_reads_refused(monkeypatch):
    _enable(monkeypatch)
    r = ask_gate.gate("t", "leжит li konfig v ~/.x/y?", seen_read_exec=False)
    assert r is not None
    low = r.lower()
    assert ("read" in low or "run" in low or "check" in low)


def test_repeat_always_passes(monkeypatch):
    _enable(monkeypatch)
    assert ask_gate.gate("t", "leжит li konfig v ~/.x/y?", seen_read_exec=False) is not None
    assert ask_gate.gate("t", "leжит li konfig v ~/.x/y?", seen_read_exec=False) is None


def test_readexec_seen_passes(monkeypatch):
    _enable(monkeypatch)
    assert ask_gate.gate("t", "leжит li konfig v ~/.x/y?", seen_read_exec=True) is None


def test_note_tool_call_marks_readexec(monkeypatch):
    _enable(monkeypatch)
    ask_gate.set_current_turn("t1")
    assert ask_gate.gate("t1", "leжит li konfig v ~/.x/y?") is not None   # 0 reads
    ask_gate.note_tool_call("read_file", "t1")
    assert ask_gate.gate("t1", "leжит li konfig v ~/.x/y?") is None       # readexec seen


def test_set_current_turn_resets_state(monkeypatch):
    _enable(monkeypatch)
    ask_gate.set_current_turn("a")
    ask_gate.note_tool_call("terminal", "a")
    assert ask_gate.gate("a", "leжит li konfig v ~/.x/y?") is None        # readexec seen
    ask_gate.set_current_turn("b")                                         # fresh turn
    assert ask_gate.gate("b", "leжит li konfig v ~/.x/y?") is not None    # 0 reads again


def test_clarify_tool_refuses_when_gate_on(monkeypatch):
    import tools.clarify_tool as ct
    _enable(monkeypatch)
    ask_gate.set_current_turn("t")
    called = []

    def cb(shown):
        called.append(1)
        return {"answers": {"q0": "x"}, "outcome": "submitted"}

    res = ct.clarify_tool([{"question": "leжит li konfig v ~/.x/y?"}], callback=cb)
    data = json.loads(res)
    assert data["outcome"] == "ask_gate_refused"
    assert called == []                                   # the human-facing callback never fired


def test_clarify_tool_lets_color_through(monkeypatch):
    import tools.clarify_tool as ct
    _enable(monkeypatch)
    ask_gate.set_current_turn("t")
    fired = []

    def cb(shown):
        fired.append(1)
        return {"answers": {"q0": "red"}, "outcome": "submitted"}

    res = ct.clarify_tool([{"question": "какой цвет кнопки?", "choices": ["red", "blue"]}], callback=cb)
    data = json.loads(res)
    assert data["outcome"] == "submitted"
    assert fired == [1]                                    # preference question goes to the human
