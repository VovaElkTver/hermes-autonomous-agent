"""S2.3 (ТЗ thinking-enable) — refusal cheaper than a false done — plus the
noop outcome of decision-2 (Вл 21.11): noop is a parametric success-state
(counter in the ledger), no new graph arcs anywhere.
"""
import json
import sqlite3
import time

import pytest

from controller import loop, model, transitions
from hermes_state_common import SCHEMA_SQL


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "state.db")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA_SQL)
    yield c
    c.close()


def _running_task(conn, tmp_path, *, contract_v=1, started_at=None):
    tid = model.create_task(conn, owner="vovak", workdir=str(tmp_path),
                            goal="шаг для анализа")
    sid = conn.execute("SELECT step_id FROM ctl_step WHERE task_id=?", (tid,)).fetchone()
    sid = sid["step_id"] if sid else model.add_step(conn, tid, kind="do", title="шаг")
    conn.execute("UPDATE ctl_task SET state='running' WHERE task_id=?", (tid,))
    run_id = model.new_id("run")
    conn.execute(
        "INSERT INTO ctl_run (run_id, task_id, step_id, contract_version,"
        " execution_epoch, run_status, started_at) VALUES (?,?,?,?,?,?,?)",
        (run_id, tid, sid, contract_v, 0, "running",
         started_at if started_at is not None else time.time() - 600))
    conn.commit()
    return tid, sid, run_id


def _pred(conn, ts):
    conn.execute(
        "INSERT INTO investigation_predictions (prediction_id, probe_id,"
        " hypothesis_id, expected, created_at) VALUES (?,?,?,?,?)",
        (model.new_id("pred"), "p1", "h1", "в этот раз пройдёт", ts))
    conn.commit()


class TestRouting:
    def test_retry_without_basis_parks_needs_human(self, conn, tmp_path):
        tid, sid, run_id = _running_task(conn, tmp_path)
        to = loop._route_recovery(conn, task_id=tid, step_id=sid, run_id=run_id,
                                  fa={"strategy": "retry_same_step",
                                      "reasoning": "таймаут сети", "modified_brief": None})
        assert to == "needs_human"
        assert model.get_task(conn, tid)["state"] == "needs_human"
        # the refusal names the missing knowledge, not just the pain
        ev = conn.execute("SELECT basis FROM ctl_state_transition WHERE task_id=?"
                          " ORDER BY rowid DESC LIMIT 1", (tid,)).fetchone()
        assert ev and "missing" in ev["basis"]

    def test_fresh_prediction_legitimizes_retry(self, conn, tmp_path):
        tid, sid, run_id = _running_task(conn, tmp_path, started_at=time.time() - 600)
        _pred(conn, time.time())  # recorded AFTER the run started
        to = loop._route_recovery(conn, task_id=tid, step_id=sid, run_id=run_id,
                                  fa={"strategy": "retry_same_step",
                                      "reasoning": "", "modified_brief": None})
        assert to == "ready"
        assert model.get_task(conn, tid)["state"] == "ready"

    def test_old_prediction_is_not_fresh(self, conn, tmp_path):
        tid, sid, run_id = _running_task(conn, tmp_path, started_at=time.time())
        _pred(conn, time.time() - 7200)  # predates the run
        to = loop._route_recovery(conn, task_id=tid, step_id=sid, run_id=run_id,
                                  fa={"strategy": "retry_same_step",
                                      "reasoning": "r", "modified_brief": None})
        assert to == "needs_human"

    def test_contract_change_legitimizes_retry(self, conn, tmp_path):
        tid, sid, run_id = _running_task(conn, tmp_path, contract_v=1)
        conn.execute("UPDATE ctl_task SET contract_version=2 WHERE task_id=?", (tid,))
        conn.commit()
        to = loop._route_recovery(conn, task_id=tid, step_id=sid, run_id=run_id,
                                  fa={"strategy": "retry_same_step",
                                      "reasoning": "", "modified_brief": None})
        assert to == "ready"

    def test_revise_brief_persists_text_and_retries(self, conn, tmp_path):
        tid, sid, run_id = _running_task(conn, tmp_path)
        to = loop._route_recovery(conn, task_id=tid, step_id=sid, run_id=run_id,
                                  fa={"strategy": "revise_step_brief",
                                      "reasoning": "бриф невнятен",
                                      "modified_brief": "запускать train.py с --cfg a.yaml"})
        assert to == "ready"
        row = conn.execute("SELECT brief FROM ctl_step WHERE step_id=?",
                           (sid,)).fetchone()
        assert row["brief"] == "запускать train.py с --cfg a.yaml"

    def test_park_and_split_strategies_needs_human(self, conn, tmp_path):
        for strategy in ("park_needs_human", "split_task", "unknown"):
            tid, sid, run_id = _running_task(conn, tmp_path)
            to = loop._route_recovery(conn, task_id=tid, step_id=sid, run_id=run_id,
                                      fa={"strategy": strategy, "reasoning": "x"})
            assert to == "needs_human", strategy


class TestBriefPrefersRevised:
    def test_brief_headline_uses_revised_text(self, conn, tmp_path):
        from controller import workers
        tid, sid, _run_id = _running_task(conn, tmp_path)
        model.set_step_brief(conn, sid, "переделанный бриф")
        step = dict(conn.execute("SELECT * FROM ctl_step WHERE step_id=?",
                                 (sid,)).fetchone())
        task = model.get_task(conn, tid)
        text = workers._brief(conn, task, step)
        assert "переделанный бриф" in text
        assert "(do): переделанный" in text  # replaces the title in the headline


class TestNoopOutcome:
    def _finish(self, conn, monkeypatch, status, reason=""):
        from plugins.controller import agent_tools
        monkeypatch.setattr(agent_tools, "_conn", lambda: conn)
        tid = model.create_task(conn, owner="vovak", workdir="/w", goal="g")
        sid = model.add_step(conn, tid, kind="do", title="шаг")
        out = json.loads(agent_tools.ctl_step_done(
            {"step_id": sid, "status": status, "reason": reason}, task_id=tid))
        return tid, sid, out

    def test_noop_closes_step_and_counts_by_reason(self, conn, monkeypatch, tmp_path):
        _tid, sid, out = self._finish(conn, monkeypatch, "noop", "прогон штатный")
        assert out["ok"] and out["status"] == "noop"
        assert out["same_reason_count"] == 1
        # same reason again -> counter grows (parametric state, one event per sitting)
        conn.execute("UPDATE ctl_step SET status='pending' WHERE step_id=?", (sid,))
        conn.commit()
        from plugins.controller import agent_tools
        out2 = json.loads(agent_tools.ctl_step_done(
            {"step_id": sid, "status": "noop", "reason": "прогон штатный"}))
        assert out2["same_reason_count"] == 2
        ev = conn.execute("SELECT payload FROM ctl_event WHERE kind='step_noop'"
                          " ORDER BY ts DESC LIMIT 1").fetchone()
        assert json.loads(ev["payload"])["same_reason_count"] == 2

    def test_noop_without_reason_refused(self, conn, monkeypatch):
        _tid, _sid, out = self._finish(conn, monkeypatch, "noop", "")
        assert out["ok"] is False and "reason" in out["error"]

    def test_noop_step_not_repicked_not_gated(self, conn, monkeypatch, tmp_path):
        from controller import queue as Q
        tid, _sid, _out = self._finish(conn, monkeypatch, "noop", "не трогать")
        assert Q.next_step(conn, tid) is None  # closed: pump never re-selects it
        # and the completed-guard does not demand a review for it (it filters
        # status='done' only) — noop neither blocks completion nor claims review.
        rows = conn.execute("SELECT step_id FROM ctl_step WHERE task_id=?"
                            " AND checkpoint IS NOT 0 AND status='done'", (tid,)).fetchall()
        assert rows == []
