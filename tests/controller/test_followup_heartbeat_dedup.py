"""S3 (TZ-memdoor-5090-v2): HEARTBEAT_OK dedup of periodic review answers.

A check with nothing to report answers HEARTBEAT_OK and wakes nobody (donor
gateway/server_jobs.go:585-600 + ADR-0022). A review whose body is the bare
token — or identical to the last delivered body of the same followup — is
skipped_quiet: no dispatch task, no chat wake, the window advances so it is
not re-tried every beat. A longer body ("HEARTBEAT_OK — но…") is NOT the bare
token and still wakes.

OFF by default (byte-for-byte pre-S3): when the flag is off the bare token
dispatches/delivers exactly as before.
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
    """Recording deliverer (truthy per attempt)."""

    def __init__(self):
        self.sent: list[str] = []

    def __call__(self, receipt):
        self.sent.append(receipt["text"])
        return "sent"


def _row(conn, fid):
    return dict(conn.execute(
        "SELECT * FROM ctl_followup WHERE followup_id=?", (fid,)).fetchone())


def _dispatched_tasks(conn):
    return conn.execute(
        "SELECT * FROM ctl_task WHERE goal LIKE '[followup %' ORDER BY created_at"
    ).fetchall()


@pytest.fixture(autouse=True)
def _clean_s3_state():
    """Module-level S3 state must not leak between tests."""
    followups._last_body.clear()
    followups.set_heartbeat_dedup_enabled(False)
    yield
    followups._last_body.clear()
    followups.set_heartbeat_dedup_enabled(False)


# ---- agent path (default recipient): quiet review answers enqueue nothing ----

def test_agent_bare_heartbeat_ok_does_not_dispatch(conn, scope_task):
    followups.set_heartbeat_dedup_enabled(True)
    fid = followups.add(conn, kind="review", scope_task=scope_task,
                        text="HEARTBEAT_OK", period_s=60, now=0.0)
    d = _Deliver()
    assert followups.scan(conn, d, now=61.0) == []          # quiet → no fire
    assert _dispatched_tasks(conn) == []                     # no dispatch task
    assert d.sent == []                                      # nothing to the human
    # window advanced, row stays armed for a future non-quiet answer
    row = _row(conn, fid)
    assert row["state"] == "pending" and row["last_fired_at"] == 61.0


def test_agent_identical_body_second_cycle_skipped_quiet(conn, scope_task):
    followups.set_heartbeat_dedup_enabled(True)
    fid = followups.add(conn, kind="review", scope_task=scope_task,
                        text="обзор CI", period_s=60, now=0.0)
    d = _Deliver()
    assert followups.scan(conn, d, now=61.0) == [fid]       # first cycle dispatches
    ref_a = _row(conn, fid)["dispatch_ref"]
    model.set_task_state(conn, ref_a, "completed")
    assert followups.scan(conn, d, now=121.0) == []         # same body → skipped_quiet
    assert len(_dispatched_tasks(conn)) == 1                 # no second task


def test_agent_heartbeat_ok_with_content_still_wakes(conn, scope_task):
    followups.set_heartbeat_dedup_enabled(True)
    fid = followups.add(conn, kind="review", scope_task=scope_task,
                        text="HEARTBEAT_OK — но CI красный", period_s=60, now=0.0)
    d = _Deliver()
    assert followups.scan(conn, d, now=61.0) == [fid]       # content → wakes
    assert len(_dispatched_tasks(conn)) == 1


def test_agent_off_bare_token_dispatches_as_before(conn, scope_task):
    """Flag OFF (default) → bare token dispatches exactly as pre-S3."""
    assert followups.is_heartbeat_dedup_enabled() is False   # default OFF
    fid = followups.add(conn, kind="review", scope_task=scope_task,
                        text="HEARTBEAT_OK", period_s=60, now=0.0)
    d = _Deliver()
    assert followups.scan(conn, d, now=61.0) == [fid]
    assert len(_dispatched_tasks(conn)) == 1


# ---- user-recipient path: quiet review answers are not delivered to the chat ----

def test_user_bare_heartbeat_ok_not_delivered(conn, scope_task):
    followups.set_heartbeat_dedup_enabled(True)
    fid = followups.add(conn, kind="review", scope_task=scope_task,
                        text="HEARTBEAT_OK", period_s=60,
                        recipient="user", now=0.0)
    d = _Deliver()
    assert followups.scan(conn, d, now=61.0) == []
    assert d.sent == []                                      # no chat wake
    assert _row(conn, fid)["last_fired_at"] == 61.0          # window advanced


def test_user_heartbeat_ok_with_content_is_delivered(conn, scope_task):
    followups.set_heartbeat_dedup_enabled(True)
    fid = followups.add(conn, kind="review", scope_task=scope_task,
                        text="HEARTBEAT_OK — но упал тест", period_s=60,
                        recipient="user", now=0.0)
    d = _Deliver()
    assert followups.scan(conn, d, now=61.0) == [fid]
    assert d.sent == ["HEARTBEAT_OK — но упал тест"]


def test_user_off_bare_token_delivers_as_before(conn, scope_task):
    """Flag OFF → bare token delivers to chat exactly as pre-S3."""
    fid = followups.add(conn, kind="review", scope_task=scope_task,
                        text="HEARTBEAT_OK", period_s=60,
                        recipient="user", now=0.0)
    d = _Deliver()
    assert followups.scan(conn, d, now=61.0) == [fid]
    assert d.sent == ["HEARTBEAT_OK"]


def test_token_is_exported_for_callers():
    assert followups.HEARTBEAT_OK_TOKEN == "HEARTBEAT_OK"
    assert followups.set_heartbeat_dedup_enabled(True) is None
    assert followups.is_heartbeat_dedup_enabled() is True
