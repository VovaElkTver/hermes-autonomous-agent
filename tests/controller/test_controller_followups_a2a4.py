"""A2 x A4 interaction (integration-branch regression, neither PR covers it):
a deduplicated pending review must expire as ONE row with ONE expired notice.

The two branches touch the same table with orthogonal changes (A4: dup_count
merge on identical pending events; A2: expires_at/close_reason TTL). The
union-schema merge must preserve BOTH semantics at once: dedup keeps the
deadline of the (single) surviving row, and the expiry sweep closes it as
'expired' exactly once — not once per merged add, not re-parked, not
double-noticed.
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


@pytest.fixture()
def a4_on():
    followups.set_a4_enabled(True)
    try:
        yield
    finally:
        followups.set_a4_enabled(False)


class _Deliver:
    def __init__(self):
        self.sent: list[str] = []

    def __call__(self, receipt):
        self.sent.append(receipt["text"])
        return "sent"


def _row(conn, fid):
    return dict(conn.execute(
        "SELECT * FROM ctl_followup WHERE followup_id=?", (fid,)).fetchone())


def test_deduped_review_expires_once_with_one_notice(conn, scope_task, a4_on):
    fid1 = followups.add(conn, kind="review", scope_task=scope_task,
                         text="решить: продолжить или бросить", period_s=60,
                         decision_ttl_s=120, created_by="user-directive", now=0.0)
    fid2 = followups.add(conn, kind="review", scope_task=scope_task,
                         text="решить: продолжить или бросить", period_s=60,
                         decision_ttl_s=120, created_by="user-directive", now=1.0)
    assert fid1 == fid2                                  # A4: collapsed
    row = _row(conn, fid1)
    assert row["dup_count"] == 2                         # A4: counted
    assert row["expires_at"] == 120.0                    # A2: deadline on the survivor
    assert row["close_reason"] is None

    d = _Deliver()
    assert followups.scan(conn, d, now=121.0) == []      # A2: past deadline -> expired
    row = _row(conn, fid1)
    assert row["state"] == "done"
    assert row["close_reason"] == "expired"
    # exactly one expiry notice for the merged row (not one per duplicate add)
    assert sum("expired without a decision" in s for s in d.sent) == 1
    # terminal rest: no resurrection on later scans
    assert followups.scan(conn, d, now=999.0) == []
    assert sum("expired without a decision" in s for s in d.sent) == 1


def test_dup_bump_keeps_earliest_deadline_no_park(conn, scope_task, a4_on):
    """The merged survivor must not silently lose its TTL when a duplicate
    arrives, and must not end up parked (state != terminal-parked) afterwards."""
    fid = followups.add(conn, kind="review", scope_task=scope_task,
                        text="review: X", period_s=60, decision_ttl_s=120,
                        created_by="user-directive", now=0.0)
    followups.add(conn, kind="review", scope_task=scope_task,
                  text="review: X", period_s=60, decision_ttl_s=120,
                  created_by="user-directive", now=50.0)
    row = _row(conn, fid)
    assert row["dup_count"] == 2
    assert row["expires_at"] == 120.0                    # earliest deadline wins
    followups.scan(conn, _Deliver(), now=121.0)
    row = _row(conn, fid)
    assert row["state"] == "done" and row["close_reason"] == "expired"
