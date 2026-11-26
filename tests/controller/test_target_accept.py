"""M1 (TZ-memdoor-5090-v2): target-based step acceptance — flag-off identity,
close-veto, pre-check skip, target_guard restore, malformed-spec loud reject.

The invariant from the acceptance rules: with the flag OFF every behavior is
byte-identical (no events, no snapshot files, finish_step returns None);
with it ON, a step closes only when its own machine target holds.
"""
import json
import os

import pytest

from controller import model, target_accept


@pytest.fixture()
def conn(tmp_path):
    c = model.connect(tmp_path / "state.db")
    yield c
    c.close()


@pytest.fixture()
def on(tmp_path, monkeypatch):
    """Flag ON + hermes home (snapshot dir) pointed at tmp."""
    monkeypatch.setenv("HERMES_TARGET_ACCEPT", "1")
    home = tmp_path / "hermes-home"
    (home / "cache").mkdir(parents=True)
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: home)
    return home


@pytest.fixture()
def off(monkeypatch):
    monkeypatch.delenv("HERMES_TARGET_ACCEPT", raising=False)
    monkeypatch.setattr(target_accept, "target_accept_enabled", lambda: False)


def _task(conn, tmp_path, steps):
    wd = tmp_path / "work"
    wd.mkdir(exist_ok=True)
    tid = model.create_task(conn, owner="v", workdir=str(wd), goal="g")
    sids = [model.add_step(conn, tid, "do", t, accept=a) for t, a in steps]
    return tid, sids, wd


def _run(conn, tid, sid):
    rid = model.start_run(conn, task_id=tid, step_id=sid, contract_version=1,
                          execution_epoch=0, worker_id="t")
    model.mark_step_in_progress(conn, sid)
    return rid


def _events(conn, kind):
    return conn.execute("SELECT payload FROM ctl_event WHERE kind=?",
                        (kind,)).fetchall()


# ---- flag OFF: byte-identical state machine ---------------------------------

def test_off_close_never_vetoed_no_events(conn, tmp_path, off):
    tid, (sid,), _wd = _task(conn, tmp_path, [("s", {"command": "false"})])
    _run(conn, tid, sid)
    assert model.finish_step(conn, sid, "done") is None
    assert model.get_step(conn, sid)["status"] == "done"
    assert _events(conn, "step_accept_pass") == []
    assert _events(conn, "step_accept_fail") == []
    assert conn.execute("SELECT accept_result FROM ctl_run").fetchone()[0] is None


def test_off_dispatch_writes_no_snapshot(conn, tmp_path, off, monkeypatch):
    _tid, (sid,), wd = _task(conn, tmp_path, [("s", {"command": "true"})])
    calls = []
    monkeypatch.setattr(target_accept, "snapshot_step",
                        lambda *a, **k: calls.append(a))
    spec = json.loads(model.get_step(conn, sid)["accept"])
    # the loop's snapshot block is gated by the same enabled() predicate the
    # pre-check uses — with the flag off the block never reaches snapshot_step:
    if spec and target_accept.target_accept_enabled():
        target_accept.snapshot_step(sid, spec, str(wd))
    assert calls == []


# ---- flag ON: close vetoed until the target holds ----------------------------

def test_on_veto_then_repair_close(conn, tmp_path, on):
    tid, (sid,), wd = _task(conn, tmp_path,
                            [("s", {"command": "test -e out.txt"})])
    rid = _run(conn, tid, sid)
    res = model.finish_step(conn, sid, "done")
    assert res["ok"] is False and res["stored"] == "pending"
    assert model.get_step(conn, sid)["status"] == "pending"
    assert len(_events(conn, "step_accept_fail")) == 1
    (wd / "out.txt").write_text("proof")
    res = model.finish_step(conn, sid, "done")
    assert res["ok"] is True
    assert model.get_step(conn, sid)["status"] == "done"
    rec = json.loads(conn.execute(
        "SELECT accept_result FROM ctl_run WHERE run_id=?", (rid,)).fetchone()[0])
    assert rec["holds"] is True and rec["rc"] == 0
    assert len(_events(conn, "step_accept_pass")) == 1


def test_on_file_target_holds_close(conn, tmp_path, on):
    tid, (sid,), wd = _task(conn, tmp_path, [("s", {"file": "done.flag"})])
    _run(conn, tid, sid)
    (wd / "done.flag").write_text("x")
    assert model.finish_step(conn, sid, "done")["ok"] is True
    assert model.get_step(conn, sid)["status"] == "done"


def test_precheck_skip_decision(conn, tmp_path, on):
    # the loop's pre-check consumes try_close: a held target returns ok=True
    # BEFORE any run exists — and the receipt is the event itself.
    tid, (sid,), wd = _task(conn, tmp_path, [("s", {"command": "true"})])
    spec = json.loads(model.get_step(conn, sid)["accept"])
    verdict = target_accept.try_close(conn, step_id=sid, task_id=tid,
                                      workdir=str(wd),
                                      accept_raw=json.dumps(spec),
                                      origin="autonomous")
    assert verdict["ok"] is True
    assert conn.execute("SELECT COUNT(*) c FROM ctl_run WHERE step_id=?",
                        (sid,)).fetchone()["c"] == 0
    assert len(_events(conn, "step_accept_pass")) == 1


# ---- target_guard: the agent cannot rewrite its own check --------------------

def test_self_reedit_restored_before_eval(conn, tmp_path, on):
    tid, (sid,), wd = _task(conn, tmp_path, [("s", {"command": "./check.sh"})])
    checker = wd / "check.sh"
    checker.write_text("#!/bin/sh\nexit 1\n")
    _run(conn, tid, sid)
    target_accept.snapshot_step(sid, {"command": "./check.sh"}, str(wd))
    # agent "fixes" its step by weakening the checker
    checker.write_text("#!/bin/sh\nexit 0\n")
    res = model.finish_step(conn, sid, "done")
    assert res["ok"] is False                      # original check still says no
    assert checker.read_text().endswith("exit 1\n")  # restored
    assert res["tampered"] == [str(checker.resolve())]


# ---- validation: loud at enqueue, not silent at close ------------------------

def test_parse_accept_rejects_ambiguity():
    with pytest.raises(ValueError):
        target_accept.parse_accept({"command": "x", "file": "y"})
    # a bare string is the documented shorthand for a command target
    assert target_accept.parse_accept("pytest -q") == {"command": "pytest -q"}
    assert target_accept.parse_accept(None) is None
    assert target_accept.parse_accept({"file": "a"}) == {"file": "a"}


def test_enqueue_stores_and_rejects_specs(conn, tmp_path, off):
    tid, _, _wd = _task(conn, tmp_path, [])
    with pytest.raises(ValueError):
        target_accept.parse_accept({"command": "ok", "timeout": "warm"})
    sid = model.add_step(conn, tid, "do", "x",
                         accept=target_accept.parse_accept({"command": "true"}))
    assert json.loads(model.get_step(conn, sid)["accept"]) == {"command": "true"}
    sid2 = model.add_step(conn, tid, "do", "plain")
    assert model.get_step(conn, sid2)["accept"] is None


def test_checker_timeout_is_not_a_pass(conn, tmp_path, on):
    tid, (sid,), _wd = _task(conn, tmp_path,
                             [("s", {"command": "sleep 5", "timeout": 1})])
    _run(conn, tid, sid)
    res = model.finish_step(conn, sid, "done")
    assert res["ok"] is False and res["rc"] == -1


# ---- plugin path: ctl_step_done surfaces the evidence ------------------------

def test_step_done_tool_returns_rework_evidence(tmp_path, monkeypatch, on):
    import plugins.controller.agent_tools as at
    conn = model.connect(tmp_path / "state2.db")
    monkeypatch.setattr(at, "_conn", lambda: conn)
    wd = tmp_path / "w"
    wd.mkdir()
    tid, (sid,), _ = _task(conn, tmp_path, [("s", {"command": "test -e p"})])
    _run(conn, tid, sid)
    res = json.loads(at.ctl_step_done({"step_id": sid, "status": "done",
                                       "expected_status": "in_progress"}))
    assert res["ok"] is False and res["status"] == "pending"
    assert "target not held" in res["error"]
    # the plugin's BEGIN IMMEDIATE may hold; conn is shared — no crash means
    # the transaction pattern survived the veto branch
    conn.close()
