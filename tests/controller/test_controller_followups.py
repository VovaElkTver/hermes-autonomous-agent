"""Async follow-ups contract (plan item 3, mini-spec 20261103, v2).

Consequences are durable rows the ticker scans on its own interval counter.
v2 recipient model: DEFAULT RECIPIENT IS THE AGENT — fire enqueues a controller
task in the scope of ``scope_task`` (inherits owner/workdir/project), and the
follow-up closes when that task goes terminal. ``recipient='user'`` keeps the
v1 chat-delivery cadence (repeat beats + exhaustion notice). All clocks are
explicit (now=...) — no wall-clock dependence.
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
    """A real task whose lane (owner/workdir/project) consequences inherit."""
    return model.create_task(conn, owner="vovak", workdir=str(tmp_path),
                             goal="корневая задача темы", project_id="proj-1")


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


def _dispatched_tasks(conn):
    return conn.execute(
        "SELECT * FROM ctl_task WHERE goal LIKE '[followup %' ORDER BY created_at"
    ).fetchall()


# ---- agent path (default): fire = enqueue work for ME, not a chat message ----

def test_agent_reminder_enqueues_task_not_chat_message(conn, scope_task):
    fid = followups.add(conn, kind="reminder", scope_task=scope_task,
                        text="проверить свежий PR", due_at=100.0,
                        created_by="user-directive", now=0.0)
    d = _Deliver()
    assert followups.scan(conn, d, now=99.0) == []          # not yet due
    assert followups.scan(conn, d, now=101.0) == [fid]      # fired → dispatch
    assert d.sent == []                                     # nothing to the human

    row = _row(conn, fid)
    assert row["state"] == "awaiting_ack" and row["retries"] == 1
    task = conn.execute("SELECT * FROM ctl_task WHERE task_id=?",
                        (row["dispatch_ref"],)).fetchone()
    assert task is not None and task["state"] == "ready"          # pump picks it up
    assert task["owner"] == "vovak" and task["project_id"] == "proj-1"
    assert "[followup" in task["goal"] and "проверить свежий PR" in task["goal"]
    # contract self-agreed so the queue does not dead-lock on review
    agreed = conn.execute("SELECT agreed_at FROM ctl_contract_revision WHERE task_id=?",
                          (row["dispatch_ref"],)).fetchone()["agreed_at"]
    assert agreed is not None


def test_agent_reminder_no_duplicate_and_closes_on_terminal(conn, scope_task):
    fid = followups.add(conn, kind="reminder", scope_task=scope_task, text="echo",
                        due_at=10.0, created_by="agent-proposal", now=0.0)
    d = _Deliver()
    assert followups.scan(conn, d, now=11.0) == [fid]
    ref = _row(conn, fid)["dispatch_ref"]

    assert followups.scan(conn, d, now=500.0) == []               # open task → hold
    assert len(_dispatched_tasks(conn)) == 1                      # never piles up

    model.set_task_state(conn, ref, "completed")                  # the work got done
    assert followups.scan(conn, d, now=900.0) == []
    assert _row(conn, fid)["state"] == "done"                     # consequence closes itself
    assert followups.scan(conn, d, now=901.0) == []               # rests after done


def test_agent_review_periodic_without_piling(conn, scope_task):
    fid = followups.add(conn, kind="review", scope_task=scope_task, text="обзор темы",
                        period_s=60, created_by="user-directive", now=0.0)
    d = _Deliver()
    assert followups.scan(conn, d, now=61.0) == [fid]
    ref_a = _row(conn, fid)["dispatch_ref"]

    assert followups.scan(conn, d, now=122.0) == []               # prev cycle still open
    assert len(_dispatched_tasks(conn)) == 1

    model.set_task_state(conn, ref_a, "completed")
    assert followups.scan(conn, d, now=130.0) == [fid]            # slot free → next cycle
    ref_b = _row(conn, fid)["dispatch_ref"]
    assert ref_b != ref_a and _row(conn, fid)["state"] == "pending"   # reviews stay pending
    followups.cancel(conn, fid)
    assert followups.scan(conn, d, now=300.0) == []


def test_agent_scope_task_gone_announces_once_then_rests(conn):
    fid = followups.add(conn, kind="reminder", scope_task="task-gone", text="проверить X",
                        due_at=10.0, created_by="user-directive", now=0.0)
    d = _Deliver()
    assert followups.scan(conn, d, now=11.0) == []
    assert _row(conn, fid)["state"] == "done"
    assert len(d.sent) == 1 and "не исполнить" in d.sent[0] and "task-gone" in d.sent[0]
    assert followups.scan(conn, d, now=999.0) == [] and len(d.sent) == 1


# ---- explicit user-recipient path: v1 semantics preserved --------------------

def test_user_reminder_repeats_then_unavailable_notice(conn):
    fid = followups.add(conn, kind="reminder", scope_task="t1", text="test",
                        due_at=0.0, recipient="user", repeat_beats=10, max_repeats=2,
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


def test_user_delivery_failure_keeps_row_durable_with_backoff(conn):
    fid = followups.add(conn, kind="reminder", scope_task="t1", text="hello",
                        due_at=5.0, recipient="user", repeat_beats=10,
                        created_by="user-directive", now=0.0)
    broken = _Deliver(fail_first=1)
    fired = followups.scan(conn, broken, now=10.0)
    assert fired == []                       # refused → not counted as fired
    row = _row(conn, fid)
    assert row["state"] == "pending" and row["retries"] == 1   # durable open work
    ok = _Deliver()
    assert followups.scan(conn, ok, now=15.0) == []            # inside backoff window
    assert followups.scan(conn, ok, now=21.0) == [fid]         # window passed → fires
    assert ok.sent == ["hello"]


# ---- general contract ----------------------------------------------------------

def test_scope_rule_and_validation(conn):
    with pytest.raises(ValueError):
        followups.add(conn, kind="reminder", scope_task="t", text="x")            # no due_at
    with pytest.raises(ValueError):
        followups.add(conn, kind="review", scope_task="t", text="x")              # no period
    with pytest.raises(ValueError):
        followups.add(conn, kind="cron", scope_task="t", text="x", due_at=1.0)    # bad kind
    with pytest.raises(ValueError):
        followups.add(conn, kind="reminder", scope_task="t", text="x", due_at=1.0,
                      recipient="broadcast")                                      # bad recipient


def test_schema_in_canonical_sql(tmp_path):
    """The canonical SCHEMA_SQL must create ctl_followup on a fresh DB."""
    c = sqlite3.connect(tmp_path / "fresh.db")
    c.executescript(SCHEMA_SQL)
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "ctl_followup" in tables
    cols = {r[1] for r in c.execute("PRAGMA table_info(ctl_followup)")}
    assert {"recipient", "dispatch_ref"} <= cols
    c.close()
