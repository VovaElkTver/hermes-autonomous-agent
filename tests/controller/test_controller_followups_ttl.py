"""A2 (openhuman plan_review TTL borrow): pending decisions carry a TTL and a
distinguishable terminal cause. ``expired`` (deadline passed, no decision) is
distinct from ``cancelled`` (explicit) and ``decided`` (the work completed).
A NULL deadline (no TTL) keeps the legacy unbounded-wait behaviour — the OFF
path is byte-for-byte the prior behaviour.
"""

import sqlite3

import pytest

from controller import followups, model
from hermes_state_common import SCHEMA_SQL


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "state.db")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA_SQL)
    yield c
    c.close()


@pytest.fixture()
def scope_task(conn, tmp_path):
    return model.create_task(conn, owner="vovak", workdir=str(tmp_path),
                             goal="корневая задача темы", project_id="proj-1")


class _Deliver:
    def __init__(self):
        self.sent: list[str] = []

    def __call__(self, receipt):
        self.sent.append(receipt["text"])
        return "sent"


def _row(conn, fid):
    return dict(conn.execute(
        "SELECT * FROM ctl_followup WHERE followup_id=?", (fid,)).fetchone())


# --- expired: nobody decided before the TTL ---------------------------------
def test_review_expires_without_decision(conn, scope_task):
    fid = followups.add(conn, kind="review", scope_task=scope_task,
                        text="решить: продолжить или бросить", period_s=60,
                        decision_ttl_s=120, created_by="user-directive", now=0.0)
    row = _row(conn, fid)
    assert row["expires_at"] == 120.0 and row["close_reason"] is None

    d = _Deliver()
    # before the deadline the periodic review still fires (t=61)
    assert followups.scan(conn, d, now=61.0) == [fid]
    assert _row(conn, fid)["state"] in ("pending", "awaiting_ack")
    # past the deadline with no decision -> closed as 'expired', not parked
    assert followups.scan(conn, d, now=121.0) == []
    row = _row(conn, fid)
    assert row["state"] == "done" and row["close_reason"] == "expired"
    # host escalation: exactly one expiry notice, not the periodic re-fire text
    assert any("expired without a decision" in s for s in d.sent)
    # a terminal row rests: further scans do nothing
    assert followups.scan(conn, d, now=999.0) == []


def test_expiry_only_affects_deadlined_rows_off_keeps_unbounded_wait(conn, scope_task):
    # OFF: no TTL -> the review keeps re-firing forever (legacy behaviour).
    fid = followups.add(conn, kind="review", scope_task=scope_task,
                        text="периодический обзор", period_s=60,
                        created_by="user-directive", now=0.0)
    assert _row(conn, fid)["expires_at"] is None
    d = _Deliver()
    assert followups.scan(conn, d, now=61.0) == [fid]
    model.set_task_state(conn, _row(conn, fid)["dispatch_ref"], "completed")
    assert followups.scan(conn, d, now=130.0) == [fid]      # still re-fires...
    model.set_task_state(conn, _row(conn, fid)["dispatch_ref"], "completed")
    assert followups.scan(conn, d, now=190.0) == [fid]       # ...indefinitely
    assert _row(conn, fid)["close_reason"] is None


# --- the three terminal causes are distinguishable --------------------------
def test_cancel_is_distinct_from_expired(conn, scope_task):
    fid = followups.add(conn, kind="review", scope_task=scope_task,
                        text="решение", period_s=60, decision_ttl_s=100,
                        created_by="user-directive", now=0.0)
    assert followups.cancel(conn, fid, reason="владелец снял вопрос")
    row = _row(conn, fid)
    assert row["state"] == "cancelled" and row["close_reason"] == "cancelled"


def test_decided_is_distinct(conn, scope_task):
    fid = followups.add(conn, kind="reminder", scope_task=scope_task,
                        text="проверить PR", due_at=10.0,
                        created_by="user-directive", now=0.0)
    d = _Deliver()
    assert followups.scan(conn, d, now=11.0) == [fid]
    ref = _row(conn, fid)["dispatch_ref"]
    model.set_task_state(conn, ref, "completed")             # the work got done
    assert followups.scan(conn, d, now=20.0) == []
    row = _row(conn, fid)
    assert row["state"] == "done" and row["close_reason"] == "decided"


# --- migration: the columns exist on a pre-A2 table -------------------------
def test_ensure_columns_migrates_legacy_table(tmp_path):
    c = sqlite3.connect(tmp_path / "legacy.db")
    c.row_factory = sqlite3.Row
    # a ctl_followup built without the A2 columns (the pre-A2 canonical shape)
    c.executescript(
        """CREATE TABLE ctl_followup (
             followup_id TEXT PRIMARY KEY,
             kind TEXT NOT NULL, scope_task TEXT NOT NULL,
             text TEXT NOT NULL, due_at REAL, period_s INTEGER,
             state TEXT NOT NULL DEFAULT 'pending',
             recipient TEXT NOT NULL DEFAULT 'agent',
             dispatch_ref TEXT, last_fired_at REAL,
             retries INTEGER NOT NULL DEFAULT 0,
             repeat_beats INTEGER NOT NULL DEFAULT 300,
             max_repeats INTEGER NOT NULL DEFAULT 3,
             created_by TEXT NOT NULL, created_at REAL NOT NULL,
             provenance_json TEXT);""")
    cols_before = {r[1] for r in c.execute("PRAGMA table_info(ctl_followup)")}
    assert "expires_at" not in cols_before
    followups.ensure_schema(c)          # must add the columns in place
    cols_after = {r[1] for r in c.execute("PRAGMA table_info(ctl_followup)")}
    assert {"expires_at", "close_reason"} <= cols_after
    c.close()
