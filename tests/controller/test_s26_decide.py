"""S2.6 (ТЗ thinking-enable) — the «решить, что делать» event.

Kernel half (controller.decide): wake only on non-empty material, watermark
durable, digest cooldown, failed delivery keeps the moment alive.
Agent half (ctl_decision): the closed outcome menu — noop needs a reason,
reprioritize moves the queue, draft is spoken with ctl_enqueue instead.
"""
import json
import sqlite3
import time

import pytest

from controller import decide, model
from hermes_state_common import SCHEMA_SQL


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "state.db")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA_SQL)
    yield c
    c.close()


def _seed_transition(conn, tid, to_state, ts, basis="verify: pump"):
    conn.execute(
        "INSERT INTO ctl_state_transition (ts, task_id, from_state, to_state,"
        " basis) VALUES (?,?,?,?,?)", (ts, tid, "running", to_state, basis))
    conn.commit()


def _permission_on(conn):
    conn.execute("INSERT INTO ctl_autonomy_permission (scope, enabled,"
                 " execution_epoch, granted_via, updated_at)"
                 " VALUES ('*', 1, 0, 'test', ?)", (time.time(),))
    conn.commit()


def _sent(sink):
    """deliver_fn contract: truthy == delivered (list.append returns None)."""
    def d(msg):
        sink.append(msg)
        return True
    return d


# ---- kernel: when does a wake happen ----------------------------------------

def test_historic_transitions_never_wake_after_boot(conn):
    # first scan anchors the watermark at now; rows BEFORE that are history
    _seed_transition(conn, "t" * 16, "needs_human", time.time() - 86400)
    lines, _st = decide.scan(conn)
    assert lines == []


def test_new_needs_human_transition_wakes(conn):
    _lines0, _ = decide.scan(conn)           # anchor watermark
    tid = model.new_id("task")
    _seed_transition(conn, tid, "needs_human", time.time() + 1)
    lines, _st = decide.scan(conn)
    assert any("needs_human" in ln for ln in lines)


def test_noop_escalation_line_fires_once_at_threshold(conn):
    decide.scan(conn)
    for i in (1, 2, 3):
        model.emit(conn, kind="step_noop", origin="agent",
                   payload={"reason": "окно тихое", "step_id": "s1",
                            "same_reason_count": i})
    lines, _st = decide.scan(conn)
    assert sum("окно тихое" in ln for ln in lines) == 1


def test_empty_queue_with_permission_and_material_wakes(conn, tmp_path):
    _ = decide.scan(conn)
    tid = model.create_task(conn, owner="vovak", workdir=str(tmp_path),
                            goal="черновик решения")
    conn.execute("UPDATE ctl_task SET state='draft' WHERE task_id=?", (tid,))
    _permission_on(conn)
    lines, st = decide.scan(conn)
    assert st["queue_empty"] and st["perm_on"]
    assert any("очередь пуста" in ln and "drafts=1" in ln for ln in lines)


def test_empty_queue_without_material_is_silent(conn):
    decide.scan(conn)
    _permission_on(conn)
    lines, _st = decide.scan(conn)
    assert lines == []          # пустая очередь ≠ терн (Вл 21.11)


def test_empty_queue_without_permission_is_silent(conn, tmp_path):
    decide.scan(conn)
    tid = model.create_task(conn, owner="vovak", workdir=str(tmp_path),
                            goal="черновик без разрешения")
    conn.execute("UPDATE ctl_task SET state='draft' WHERE task_id=?", (tid,))
    # no ctl_autonomy_permission row → permission off → no decision window
    lines, _st = decide.scan(conn)
    assert lines == []


# ---- kernel: watermark + delivery semantics ---------------------------------

def test_delivered_wake_advances_watermark_once(conn):
    _ = decide.scan(conn)
    _seed_transition(conn, "a" * 16, "completed", time.time() + 1)
    sent = []
    assert decide.decide_tick(conn, deliver_fn=_sent(sent)) is True
    assert len(sent) == 1 and "DECIDE" in sent[0]
    # same moment again: watermark moved past it, nothing new to say
    assert decide.decide_tick(conn, deliver_fn=_sent(sent)) is False
    assert len(sent) == 1


def test_failed_delivery_keeps_the_moment_alive(conn):
    _ = decide.scan(conn)
    _seed_transition(conn, "b" * 16, "blocked", time.time() + 1)
    assert decide.decide_tick(conn, deliver_fn=lambda _m: False) is False
    # transport recovered: the SAME moment is still there (watermark held)
    sent = []
    assert decide.decide_tick(conn, deliver_fn=_sent(sent)) is True
    assert any("blocked" in ln for ln in sent[0].splitlines())


def test_same_digest_within_cooldown_does_not_rewake(conn, tmp_path):
    # stable material line (todo row) produces an identical digest twice
    conn.execute("INSERT INTO ctl_todo (todo_id, owner, text, status,"
                 " created_at) VALUES ('x1', 'agent', 'замётка', 'open', ?)",
                 (time.time(),))
    conn.commit()
    _permission_on(conn)
    sent = []
    assert decide.decide_tick(conn, deliver_fn=_sent(sent)) is True
    assert decide.decide_tick(conn, deliver_fn=_sent(sent)) is False
    assert len(sent) == 1


def test_wake_event_lands_in_ledger(conn):
    _ = decide.scan(conn)
    _seed_transition(conn, "c" * 16, "needs_human", time.time() + 1)
    decide.decide_tick(conn, deliver_fn=lambda _m: True)
    row = conn.execute("SELECT payload FROM ctl_event WHERE"
                       " kind='decision-wake'").fetchone()
    assert row is not None


# ---- agent half: ctl_decision ------------------------------------------------

def _tools(monkeypatch, conn):
    from plugins.controller import agent_tools
    monkeypatch.setattr(agent_tools, "_conn", lambda: conn)
    return agent_tools


def test_decision_noop_requires_reason(conn, monkeypatch):
    at = _tools(monkeypatch, conn)
    out = json.loads(at._ctl_decision({"kind": "noop"}))
    assert out["ok"] is False and "reason" in out["error"]


def test_decision_noop_counts_same_reason(conn, monkeypatch):
    at = _tools(monkeypatch, conn)
    o1 = json.loads(at._ctl_decision({"kind": "noop", "reason": "тихое окно"}))
    o2 = json.loads(at._ctl_decision({"kind": "noop", "reason": "тихое окно"}))
    assert o1["same_reason_count"] == 1 and o2["same_reason_count"] == 2
    n = conn.execute("SELECT COUNT(*) c FROM ctl_event"
                     " WHERE kind='decision_noop'").fetchone()["c"]
    assert n == 2


def test_decision_reprioritize_moves_queue_head(conn, monkeypatch, tmp_path):
    at = _tools(monkeypatch, conn)
    t1 = model.create_task(conn, owner="vovak", workdir=str(tmp_path),
                           goal="первый")
    t2 = model.create_task(conn, owner="vovak", workdir=str(tmp_path),
                           goal="второй")
    conn.execute("UPDATE ctl_task SET state='ready' WHERE task_id IN (?,?)",
                 (t1, t2))
    conn.commit()
    out = json.loads(at._ctl_decision({"kind": "reprioritize", "task_id": t2,
                                       "reason": "внешний дедлайн"}))
    assert out["ok"] is True
    p = conn.execute("SELECT json_extract(fields,'$.priority') p FROM"
                     " ctl_contract_revision WHERE task_id=? ORDER BY"
                     " version DESC LIMIT 1", (t2,)).fetchone()["p"]
    assert p > 0                      # head = above every live task
    ev = conn.execute("SELECT 1 FROM ctl_event WHERE kind="
                      "'decision_reprioritize' AND task_id=?", (t2,))
    assert ev.fetchone() is not None


def test_reprioritize_rejects_non_queueable_state(conn, monkeypatch, tmp_path):
    at = _tools(monkeypatch, conn)
    tid = model.create_task(conn, owner="vovak", workdir=str(tmp_path),
                            goal="работает")
    conn.execute("UPDATE ctl_task SET state='running' WHERE task_id=?", (tid,))
    conn.commit()
    out = json.loads(at._ctl_decision({"kind": "reprioritize", "task_id": tid}))
    assert out["ok"] is False          # running task is not in a queue to move


def test_draft_kind_points_at_ctl_enqueue(conn, monkeypatch):
    at = _tools(monkeypatch, conn)
    out = json.loads(at._ctl_decision({"kind": "draft"}))
    assert out["ok"] is False and "ctl_enqueue" in out["error"]
