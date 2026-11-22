"""S2.4 (ТЗ thinking-enable) — consequences into the brief.

Writer half: every task-bound ctl_event gains an `about` edge (model.emit).
Reader half: workers._consequences walks those edges + last reviews and the
section lands in the dispatch brief. Caps are the point: O(recent-window),
never O(store) — doctrine Вл 25.10 (живность дешёвая).
"""
import json
import sqlite3
import time

import pytest

from controller import model, workers
from hermes_state_common import SCHEMA_SQL


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "state.db")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA_SQL)
    yield c
    c.close()


def _task(conn, tmp_path):
    return model.create_task(conn, owner="vovak", workdir=str(tmp_path),
                             goal="собрать прогон")


def test_emit_links_event_about_task(conn, tmp_path):
    tid = _task(conn, tmp_path)
    eid = model.emit(conn, kind="agent_blocked", origin="agent", task_id=tid,
                     payload={"reason": "нет доступа кgpu"})
    row = conn.execute("SELECT kind FROM ctl_causal_link WHERE from_event=?"
                       " AND to_event=?", (f"task:{tid}", f"ctl:{eid}")).fetchone()
    assert row and row["kind"] == "about"


def test_taskless_event_writes_no_edge(conn):
    eid = model.emit(conn, kind="ticker_heartbeat", origin="ticker")
    assert conn.execute("SELECT 1 FROM ctl_causal_link WHERE from_event=?",
                        (f"ctl:{eid}",)).fetchone() is None


def test_broken_task_ref_does_not_sink_event(conn):
    # dangling to-endpoint: strict=False must drop the edge, keep the event
    eid = model.emit(conn, kind="alert", origin="ops", task_id="task-ghost")
    assert conn.execute("SELECT 1 FROM ctl_event WHERE event_id=?", (eid,)).fetchone()
    assert conn.execute("SELECT 1 FROM ctl_causal_link WHERE from_event=?",
                        (f"ctl:{eid}",)).fetchone() is None


def test_brief_carries_consequences_and_reviews(conn, tmp_path):
    tid = _task(conn, tmp_path)
    sid = model.add_step(conn, tid, kind="do", title="шаг сборки")
    model.emit(conn, kind="external_patch_reverted", origin="ops", task_id=tid,
               payload={"reason": "внешний агент снял interim-патч"})
    for _ in range(3):
        model.emit(conn, kind="alert", origin="ops", task_id=tid,
                   payload={"detail": "прогон встал на GPU"})
    conn.execute(
        "INSERT INTO ctl_review (review_id, step_id, run_id, artifact_hash,"
        " verdict, reviewer, detail, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (model.new_id("rev"), sid, "run-1", "h", "revise", "judge",
         "критерий AC-2 не покрыт", time.time()))
    conn.commit()
    brief = workers._brief(conn, model.get_task(conn, tid),
                           dict(conn.execute("SELECT * FROM ctl_step WHERE"
                                             " step_id=?", (sid,)).fetchone()))
    assert "Recent consequences around this task" in brief
    assert "внешний агент снял interim-патч" in brief
    assert "alert x3" in brief           # repeats collapse into one counted line
    assert "критерий AC-2 не покрыт" in brief


def test_old_events_out_of_window(conn, tmp_path):
    tid = _task(conn, tmp_path)
    old = model.emit(conn, kind="stale", origin="ops", task_id=tid)
    conn.execute("UPDATE ctl_event SET ts=? WHERE event_id=?",
                 (time.time() - 40 * 86400, old))
    conn.execute("UPDATE ctl_causal_link SET ts=? WHERE to_event=?",
                 (time.time() - 40 * 86400, f"ctl:{old}"))
    conn.commit()
    task = model.get_task(conn, tid)
    out = workers._consequences(conn, task)
    assert "stale" not in out


def test_caps_bound_the_section(conn, tmp_path):
    tid = _task(conn, tmp_path)
    for i in range(30):
        model.emit(conn, kind=f"kind_{i}", origin="ops", task_id=tid,
                   payload={"reason": "р" * 200})
    task = model.get_task(conn, tid)
    out = workers._consequences(conn, task)
    assert len(out) <= 1200
    assert out.count("\n") <= 8  # cap rows (reviews absent here)
