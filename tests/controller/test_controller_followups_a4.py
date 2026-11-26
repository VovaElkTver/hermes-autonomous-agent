"""A4 (openhuman memory-v2 §Background jobs): merge-дедупликация идентичных
pending-эвентов и «отказ аккаунта — не попытка».

Дisease (donor): ``docs/specs/memory-v2.md`` — «merged when identical … a failing
job is retried up to five times; an account-wide refusal (no credits, a rejected
credential, an unreachable engine) does not count as an attempt, so the job waits
instead of being dropped».

Acceptance (TZ A4):
- two identical adds → one record with ``dup_count=2``;
- a 503 / resource-unavailable series → 0 attempts, the row *waits* (never
  dead-letters, the retry budget is not burned);
- N real errors → dead-letter (the budget is consumed and the row closes);
- flag OFF (``set_a4_enabled(False)``) → byte-for-byte the pre-A4 semantics
  (no dedup, every failure burns the budget).
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


@pytest.fixture()
def a4_on():
    followups.set_a4_enabled(True)
    try:
        yield
    finally:
        followups.set_a4_enabled(False)


@pytest.fixture()
def a4_off():
    followups.set_a4_enabled(False)
    yield


class _WaitDeliver:
    """Resource is down (503 / credential / no-credit): every send is a wait."""

    def __init__(self):
        self.waits = 0
        self.sent: list[str] = []

    def __call__(self, receipt):
        self.waits += 1
        raise followups.ResourceUnavailable("endpoint 503 / account down")


class _AlwaysRefused:
    """Real failure: every send is refused (consumes the retry budget)."""

    def __init__(self):
        self.attempts = 0
        self.sent: list[str] = []

    def __call__(self, receipt):
        self.attempts += 1
        raise RuntimeError("real error (simulated)")


def _row(conn, fid):
    return dict(conn.execute(
        "SELECT * FROM ctl_followup WHERE followup_id=?", (fid,)).fetchone())


def _open_rows(conn, scope_task):
    return conn.execute(
        "SELECT followup_id, dup_count FROM ctl_followup "
        "WHERE scope_task=? AND state IN ('pending','awaiting_ack') "
        "ORDER BY followup_id", (scope_task,)).fetchall()


# ---- (1) merge-дедупликация ---------------------------------------------------

def test_a4_on_two_identical_adds_collapse_to_one_row(conn, a4_on):
    f1 = followups.add(conn, kind="reminder", scope_task="t1", text="проверить X",
                       due_at=5.0, created_by="user-directive", now=0.0)
    f2 = followups.add(conn, kind="reminder", scope_task="t1", text="проверить X",
                       due_at=5.0, created_by="user-directive", now=0.0)
    assert f1 == f2                              # the second add returns the same id
    rows = _open_rows(conn, "t1")
    assert len(rows) == 1                        # one record, not two
    assert rows[0]["dup_count"] == 2            # …with the counter


def test_a4_on_distinct_events_do_not_collapse(conn, a4_on):
    a = followups.add(conn, kind="reminder", scope_task="t1", text="проверить X",
                      due_at=5.0, now=0.0)
    b = followups.add(conn, kind="reminder", scope_task="t1", text="проверить Y",
                      due_at=5.0, now=0.0)
    assert a != b and len(_open_rows(conn, "t1")) == 2


def test_a4_off_identical_adds_stay_two_rows(conn, a4_off):
    """Pre-A4 invariant: add() never deduped — two identical adds = two rows."""
    a = followups.add(conn, kind="reminder", scope_task="t1", text="проверить X",
                      due_at=5.0, now=0.0)
    b = followups.add(conn, kind="reminder", scope_task="t1", text="проверить X",
                      due_at=5.0, now=0.0)
    assert a != b and len(_open_rows(conn, "t1")) == 2


# ---- (2) «отказ аккаунта — не попытка» ---------------------------------------

def test_a4_on_resource_unavailable_waits_without_burning_budget(conn, a4_on):
    fid = followups.add(conn, kind="reminder", scope_task="t1", text="test",
                        due_at=0.0, recipient="user", repeat_beats=10,
                        max_repeats=3, created_by="user-directive", now=0.0)
    d = _WaitDeliver()
    # Run well past the point where pre-A4 semantics would have dead-lettered
    # (max_repeats=3): the resource is down, so the row must keep waiting.
    for t in (0.0, 11.0, 22.0, 33.0, 44.0):
        assert followups.scan(conn, d, now=t) == []      # nothing fired
    row = _row(conn, fid)
    assert row["state"] == "pending"           # NOT dropped / dead-lettered
    assert row["retries"] == 0                 # zero attempts consumed
    assert d.waits >= 5                        # …yet the transport was tried


def test_a4_off_resource_unavailable_still_burns_budget(conn, a4_off):
    """Pre-A4 invariant: any failure (incl. resource-unavailable) consumed the
    retry budget — a 503 series would have dead-lettered like any other error."""
    fid = followups.add(conn, kind="reminder", scope_task="t1", text="test",
                        due_at=0.0, recipient="user", repeat_beats=10,
                        max_repeats=3, created_by="user-directive", now=0.0)
    d = _WaitDeliver()
    followups.scan(conn, d, now=0.0)
    assert _row(conn, fid)["retries"] == 1        # consumed, as pre-A4


# ---- (3) N реальных ошибок → dead-letter --------------------------------------

def test_a4_on_real_errors_deadletter(conn, a4_on):
    fid = followups.add(conn, kind="reminder", scope_task="t1", text="test",
                        due_at=0.0, recipient="user", repeat_beats=10,
                        max_repeats=3, created_by="user-directive", now=0.0)
    d = _AlwaysRefused()
    for t in (0.0, 11.0, 22.0):                 # 3 real refusals → budget full
        followups.scan(conn, d, now=t)
    assert _row(conn, fid)["retries"] == 3
    assert _row(conn, fid)["state"] == "pending"  # still open at the last attempt
    followups.scan(conn, d, now=33.0)            # exhausted → dead-letter
    row = _row(conn, fid)
    assert row["state"] == "done"                 # real errors burned the budget
    # 3 real refusals + 1 exhaustion-notice attempt (the dead-letter branch also
    # routes its notice through deliver, which this stub refuses as well).
    assert d.attempts == 4
    # The row is now closed: no further scan touches it (the notice fired once).
    assert followups.scan(conn, d, now=44.0) == []

