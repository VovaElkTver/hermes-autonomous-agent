"""M2 (TZ-memdoor-5090-v2): subagent result-acceptance — deterministic judge
markers, exactly-one handback into the same child session, verdict on the
announcement, and total invisibility when the flag is off."""
import pytest

from tools import result_acceptance as ra


@pytest.fixture()
def on(monkeypatch):
    monkeypatch.setattr(ra, "result_acceptance_enabled", lambda: True)


@pytest.fixture()
def off(monkeypatch):
    monkeypatch.setattr(ra, "result_acceptance_enabled", lambda: False)


def _entry(summary, status="completed", **kw):
    return {"task_index": 0, "summary": summary, "status": status, **kw}


class _Child:
    def __init__(self):
        self.calls = []


@pytest.fixture()
def runner(monkeypatch):
    """Captures handback calls; the returned second result is per-test set."""
    import tools.delegate_tool as dt
    box = {"calls": [], "second": None, "raise": False}

    def fake(task_index, goal, child=None, parent_agent=None, **kw):
        box["calls"].append(goal)
        if box["raise"]:
            raise RuntimeError("child session dead")
        return box["second"]

    monkeypatch.setattr(dt, "_run_single_child", fake)
    return box


# ---- judge ----------------------------------------------------------------

def test_judge_markers(on):
    assert ra.judge(_entry("")) == "empty summary"
    assert ra.judge(_entry("Done.")) == "bare claim with nothing verifiable"
    assert ra.judge(_entry("Should I use pytest or unittest?")) == \
        "question back instead of a result"
    assert ra.judge(_entry("I will start by reading the repo and then plan "
                           "the refactor steps.")) == \
        "plan announced instead of a result"
    # prose WITH a verifiable handle is finished
    assert ra.judge(_entry("Done: /home/qwen/app/x.py, 42 tests passed")) is None
    assert ra.judge(_entry("I will note that the fix landed at "
                           "https://example.com/pr/7 and exit code 0 came back")) is None
    # non-completed statuses are the host's structured failures — not judged
    assert ra.judge(_entry("", status="failed")) is None
    # a real tool trace is evidence even against thin prose
    assert ra.judge(_entry("all set", tool_trace=[{"name": "patch"}])) is None


def test_off_invisible(on, off, runner):
    runner["second"] = {"summary": "real /x 1 passed"}
    results = [_entry("Done.")]
    ra.apply_acceptance(results, [{"goal": "g"}], {0: _Child()}, None)
    assert results[0] == {"task_index": 0, "summary": "Done.",
                          "status": "completed"}
    assert runner["calls"] == []


# ---- handback exactly once --------------------------------------------------

def test_first_unfinished_gets_one_handback_and_passes(on, runner):
    runner["second"] = {"summary": "created /home/qwen/x.py, 7 tests passed",
                        "status": "completed"}
    child = _Child()
    results = [_entry("I will start by reading the config and planning the "
                      "changes needed.")]
    ra.apply_acceptance(results, [{"goal": "ship X"}], {0: child}, None)
    assert len(runner["calls"]) == 1
    assert "[acceptance handback]" in runner["calls"][0]
    assert "ship X" in runner["calls"][0]
    assert results[0]["summary"].startswith("created /home")
    assert "acceptance" not in results[0]
    assert results[0]["acceptance_reworked"] is True


def test_second_result_announced_with_verdict_never_rehanded(on, runner):
    runner["second"] = {"summary": "Still not done — will continue."}
    results = [_entry("Done.")]
    ra.apply_acceptance(results, [{"goal": "g"}], {0: _Child()}, None)
    assert len(runner["calls"]) == 1          # never handed back twice
    assert results[0]["acceptance"] == "unfinished"
    assert results[0]["acceptance_reason"] in (
        "bare claim with nothing verifiable",
        "summary too thin to be a result",
        "plan announced instead of a result")


def test_no_child_still_attaches_verdict(on, runner):
    results = [_entry("Готово!")]
    ra.apply_acceptance(results, [{"goal": "g"}], {}, None)
    assert results[0]["acceptance"] == "unfinished"
    assert runner["calls"] == []


def test_dead_child_rework_keeps_first_result(on, runner):
    runner["raise"] = True
    results = [_entry("Done.")]
    ra.apply_acceptance(results, [{"goal": "g"}], {0: _Child()}, None)
    assert results[0]["summary"] == "Done."
    assert results[0]["acceptance"] == "unfinished"
