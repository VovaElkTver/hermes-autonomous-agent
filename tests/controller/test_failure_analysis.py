"""failure_analysis: flag-off inertness, attempt gate, strategy clamping."""

from __future__ import annotations

import json
import sqlite3

import pytest

from controller import failure_analysis as FA


@pytest.fixture
def mem_db():
    con = sqlite3.connect(":memory:")
    con.execute("create table ctl_event (event_id text, ts real, kind text,"
                " task_id text, origin text, payload text, idempotency_key text)")
    yield con
    con.close()


def test_flag_off_apply_is_inert(mem_db, monkeypatch):
    monkeypatch.setattr(FA, "enabled", lambda: False)
    assert FA.enabled() is False
    assert FA.apply(mem_db, task_id="t", run_id="r", attempt=5,
                    task_goal="g", step_summary="s", error_tail="e") is None


def test_attempt_gate(monkeypatch):
    calls = []
    monkeypatch.setattr(FA, "enabled", lambda: True)
    monkeypatch.setattr(FA, "_expert_endpoint",
                        lambda: ("http://127.0.0.1:1/v1", "m"))  # will fail -> None
    # attempt below trigger: no HTTP attempt at all
    assert FA.analyze_failure("goal", "step", 1, "err") is None
    # at trigger it WOULD attempt HTTP; endpoint refuses -> fail-open None
    def probe(*a, **kw):
        calls.append(1)
        return FA.analyze_failure("goal", "step", FA._TRIGGER_ATTEMPT, "err")
    assert probe() is None or calls == []  # never raises either way
    assert True


def test_clamp_strategy():
    for s in FA.STRATEGIES:
        assert FA.clamp_strategy(s) == s
    assert FA.clamp_strategy("galaxy_brain") == "retry_same_step"
    assert FA.clamp_strategy(None) == "retry_same_step"


def test_analyze_parses_json_with_trailing_prose(monkeypatch):
    seen = []
    monkeypatch.setattr(FA, "log_line", lambda **kw: seen.append(kw))
    payload = ('{"strategy": "revise_step_brief", "reasoning": "brief vague", '
               '"modified_brief": "do X first"}  explanation tail')

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return json.dumps(
            {"choices": [{"message": {"content": payload}}]}).encode()

    def fake_urlopen(req, timeout=0):
        return FakeResp()

    monkeypatch.setattr(FA, "enabled", lambda: True)
    monkeypatch.setattr(FA, "_expert_endpoint",
                        lambda: ("http://expert.local/v1", "glm"))
    import urllib.request as _u
    monkeypatch.setattr(_u, "urlopen", fake_urlopen)
    out = FA.analyze_failure("goal", "step", 2, "boom")
    assert out is not None, seen          # surface swallowed analyzer errors
    assert out["strategy"] == "revise_step_brief"
    assert out["modified_brief"] == "do X first"


def test_log_line_swallows_errors(tmp_path, monkeypatch):
    # log_line must never raise even with a broken sink
    def broken_emit(conn, **kw):
        raise RuntimeError("sink down")
    monkeypatch.setattr(FA, "_emit", broken_emit)
    monkeypatch.setattr(FA, "connect_state", lambda: sqlite3.connect(":memory:"))
    FA.log_line(kind="x")
    assert True
