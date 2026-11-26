"""M3 (TZ-memdoor-5090-v2): turn-progress shadow — deterministic stuck score,
journal-only phase 1, once-per-turn wrap-up injection in phase 2, total silence
when the flag is off."""
import json

import pytest

from agent import turn_shadow as ts


def _calls(n, name="terminal", args=None, ok=True, kind=None):
    msgs, ids = [], []
    for i in range(n):
        tcid = f"c{i}"
        ids.append(tcid)
        msgs.append({"role": "assistant", "content": "",
                     "tool_calls": [{"id": tcid, "function": {
                         "name": name,
                         "arguments": json.dumps(args or {"command": "ls"})}}]})
        msgs.append({"role": "tool", "tool_call_id": tcid,
                     "content": "ok" if ok else "Error: boom"})
    return msgs


class _Agent:
    session_id = "s-test"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    return tmp_path


def _read_log(home):
    p = home / "logs" / "turn_shadow.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


# ---- score --------------------------------------------------------------------

def test_stuck_patterns_score():
    stuck = ts.recent_calls(_calls(5))
    assert ts.score_calls(stuck)["score"] >= 0.8          # 5 identical alone
    mixed = _calls(2, args={"command": "a"}) + _calls(6, args={"command": "b"})
    assert ts.score_calls(ts.recent_calls(mixed))["score"] >= 0.8  # 6/8 identical
    flail = _calls(3, name="patch", args={"path": "x"}, ok=False)
    assert ts.score_calls(ts.recent_calls(flail))["score"] >= 0.8
    healthy = _calls(2, name="read_file", args={"p": 1}) + \
        _calls(1, name="patch", args={"p": 2}) + \
        _calls(2, name="read_file", args={"p": 3}) + \
        _calls(1, name="terminal", args={"command": "pytest"}) + \
        _calls(1, name="patch", args={"p": 4}) + _calls(1, name="terminal", args={"command": "git"})
    assert ts.score_calls(ts.recent_calls(healthy))["score"] < 0.8
    assert ts.score_calls([])["score"] == 0.0


def test_reads_without_edits_nudges_not_fires():
    msgs = _calls(1, name="read_file", args={"p": 1}) + \
        _calls(1, name="read_file", args={"p": 2}) + \
        _calls(1, name="read_file", args={"p": 3}) + \
        _calls(1, name="search_files", args={"p": 4}) + \
        _calls(1, name="read_file", args={"p": 5}) + \
        _calls(1, name="read_file", args={"p": 6}) + \
        _calls(1, name="skill_view", args={"p": 7}) + \
        _calls(1, name="read_file", args={"p": 8})
    v = ts.score_calls(ts.recent_calls(msgs))
    assert v["reads_no_edits"] is True and 0.4 < v["score"] < 0.8


# ---- phase 1: journal only ------------------------------------------------------

def test_phase1_journals_stuck_and_stays_silent_on_healthy(home):
    v = ts.observe(_Agent(), _calls(6), 7)
    assert v and v["would_wrap_up"] is True
    lines = _read_log(home)
    assert len(lines) == 1 and lines[0]["iter"] == 7
    healthy = _calls(1, name="read_file", args={"a": 1}) + \
        _calls(1, name="patch", args={"b": 2}) + \
        _calls(1, name="read_file", args={"c": 3}) + \
        _calls(1, name="terminal", args={"d": 4})
    assert ts.observe(_Agent(), healthy, 2) is None
    assert len(_read_log(home)) == 1  # no new line for a healthy window


def test_phase1_never_touches_messages(home):
    msgs = _calls(6)
    before = json.dumps(msgs, sort_keys=True)
    ts.observe(_Agent(), msgs, 3)
    assert json.dumps(msgs, sort_keys=True) == before


# ---- phase 2: wrap-up injection ---------------------------------------------------

def test_wrapup_flag_off_no_injection(home, monkeypatch):
    monkeypatch.setattr(ts, "wrap_up_enabled", lambda: False)
    msgs = _calls(6)
    n = len(msgs)
    assert ts.maybe_inject_wrap_up(_Agent(), msgs) is False
    assert len(msgs) == n


def test_wrapup_fires_once_per_turn(home, monkeypatch):
    monkeypatch.setattr(ts, "wrap_up_enabled", lambda: True)
    agent = _Agent()
    msgs = _calls(6)

    def inject(a, m, text):
        m.append({"role": "user", "content": text, "hidden": True})

    import agent.turn_iteration_prep as tip
    monkeypatch.setattr(tip, "_inject_steer_after_newest_tool_result", inject)
    assert ts.maybe_inject_wrap_up(agent, msgs) is True
    assert any("wrap-up" in str(m.get("content")) for m in msgs)
    # second iteration of the same turn: never twice
    assert ts.maybe_inject_wrap_up(agent, _calls(6)) is False
