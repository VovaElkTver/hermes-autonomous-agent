"""Async follow-ups contract (plan item 3, mini-spec 20261103).

Consequences are durable rows the ticker scans on its own interval counter:
reminder = due_at + repeat-beats countdown (donor evals REQUEUES→unavailable
parity), review = periodic by period_s counted from creation/last fire.
Delivery rides the live chat port; a refused send backs off, never loses the row.
All tests drive clocks explicitly (now=...) — no wall-clock dependence.
"""

import sqlite3

import pytest

from controller import followups
from hermes_state_common import SCHEMA_SQL


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "state.db")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA_SQL)
    yield c
    c.close()


class _Deliver:
    """Recording deliverer: truthy per attempt, optionally failing N first calls."""

    def __init__(self, fail_first=0):
        self.sent: list[str] = []
        self.fail_first = fail_first

    def __call__(self, receipt):
        if self.fail_first > 0:
            self.fail_first -= 1
            raise RuntimeError("transport refused (simulated)")
        self.sent.append(receipt["text"])
        return "sent"


def _row(conn, fid):
    return dict(conn.execute("SELECT * FROM ctl_followup WHERE followup_id=?", (fid,)).fetchone())


def test_reminder_fires_once_then_awaiting_ack(conn):
    fid = followups.add(conn, kind="reminder", scope_task="t1", text="позвонить в сервис",
                        due_at=100.0, created_by="user-directive", now=0.0)
    d = _Deliver()
    assert followups.scan(conn, d, now=99.0) == []          # not yet due
    assert followups.scan(conn, d, now=101.0) == [fid]      # fired
    d2 = _Deliver()
    assert followups.scan(conn, d2, now=102.0) == []        # awaiting_ack holds
    row = _row(conn, fid)
    assert row["state"] == "awaiting_ack" and row["retries"] == 1
    assert d.sent == ["позвонить в сервис"] and d2.sent == []


def test_reminder_repeats_then_unavailable_notice(conn):
    fid = followups.add(conn, kind="reminder", scope_task="t1", text="test",
                        due_at=0.0, repeat_beats=10, max_repeats=2,
                        created_by="user-directive", now=0.0)
    d = _Deliver()
    t = 0.0
    for expected in ([fid], [fid], []):   # initial + 1 repeat; 3rd attempt closes out
        fired = followups.scan(conn, d, now=t)
        assert fired == expected, f"at t={t}: {fired} != {expected}"
        t += 11.0
    followups.scan(conn, d, now=t)        # exhaustion → unavailable notice + done
    assert _row(conn, fid)["state"] == "done"
    notices = [s for s in d.sent if "не дозвонилось" in s]
    real_sends = [s for s in d.sent if s == "test"]
    assert len(notices) == 1 and len(real_sends) <= 2
    followups.scan(conn, d, now=t + 100.0)                # rests after done
    assert len([s for s in d.sent if "не дозвонилось" in s]) == 1


def test_delivery_failure_keeps_row_durable_with_backoff(conn):
    fid = followups.add(conn, kind="reminder", scope_task="t1", text="hello",
                        due_at=5.0, repeat_beats=10, created_by="user-directive", now=0.0)
    broken = _Deliver(fail_first=1)
    fired = followups.scan(conn, broken, now=10.0)
    assert fired == []                       # refused → not counted as fired
    row = _row(conn, fid)
    assert row["state"] == "pending" and row["retries"] == 1   # durable open work
    ok = _Deliver()
    assert followups.scan(conn, ok, now=15.0) == []            # inside backoff window
    assert followups.scan(conn, ok, now=21.0) == [fid]         # window passed → fires
    assert ok.sent == ["hello"]


def test_review_is_periodic_until_cancelled(conn):
    fid = followups.add(conn, kind="review", scope_task="t2", text="обзор темы",
                        period_s=60, created_by="user-directive", now=0.0)
    d = _Deliver()
    assert followups.scan(conn, d, now=50.0) == []
    assert followups.scan(conn, d, now=61.0) == [fid]
    assert followups.scan(conn, d, now=90.0) == []
    assert followups.scan(conn, d, now=122.0) == [fid]
    assert len(d.sent) == 2
    followups.cancel(conn, fid)
    assert followups.scan(conn, d, now=300.0) == []


def test_scope_rule_and_validation(conn):
    with pytest.raises(ValueError):
        followups.add(conn, kind="reminder", scope_task="t", text="x")            # no due_at
    with pytest.raises(ValueError):
        followups.add(conn, kind="review", scope_task="t", text="x")              # no period
    with pytest.raises(ValueError):
        followups.add(conn, kind="cron", scope_task="t", text="x", due_at=1.0)    # bad kind


def test_schema_in_canonical_sql(tmp_path):
    """The canonical SCHEMA_SQL must create ctl_followup on a fresh DB."""
    c = sqlite3.connect(tmp_path / "fresh.db")
    c.executescript(SCHEMA_SQL)
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "ctl_followup" in tables
    c.close()
