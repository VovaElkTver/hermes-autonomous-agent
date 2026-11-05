"""controller.receipts — input read-receipt discipline (port #3).

Ported from dsh-superagent-prototype ``assignments.ts``/``tickets.ts``
(deliveredAt/readAt/idleAt, ``requeueUntaken``, RECOVERY_NOTE) @6337fc8
(audit 2026-11-01; mechanism donor, runtime NOT adapted wholesale).

Model: an input (ctl_event) handed to a run gets a receipt row on delivery;
``read_at`` is set only when a step of the run actually consumed it. When a run
is replaced (retry after failure, recovery after host restart), everything its
steps never took is requeued onto the successor — at-least-once delivery with
per-input dedup, no silent drops, no double-consumption.

:func:`classify_interrupted` and :data:`RECOVERY_NOTE` are the restart protocol:
a worker that had been busy since its last report restarts with the note; one
that was parked settled keeps its state and just receives waiting inputs.
"""
from __future__ import annotations

import time

#: Donor RECOVERY_NOTE, translated; carries the "do not repeat completed external
#: operations" rule so a recovered run audits before acting.
RECOVERY_NOTE = ("The host restarted during this task. Inspect the current worktree, "
                 "git history and external state before continuing. Do not repeat an "
                 "already completed external operation. Finish the original "
                 "assignment and report its status.")


def delivered(conn, run_id: str, event_ids, *, read_at=None) -> None:
    """The executor took these inputs into the run (idempotent)."""
    now = time.time()
    for eid in event_ids:
        conn.execute(
            "INSERT OR IGNORE INTO ctl_input_receipt (run_id,event_id,delivered_at)"
            " VALUES (?,?,?)", (run_id, eid, now))


def read(conn, run_id: str, event_ids, *, at=None) -> None:
    """A step of this run consumed the inputs (first read wins)."""
    now = at or time.time()
    for eid in event_ids:
        conn.execute(
            "UPDATE ctl_input_receipt SET read_at=?"
            " WHERE run_id=? AND event_id=? AND read_at IS NULL",
            (now, run_id, eid))


def unread(conn, run_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT event_id FROM ctl_input_receipt"
        " WHERE run_id=? AND read_at IS NULL ORDER BY rowid", (run_id,)).fetchall()
    return [r["event_id"] for r in rows]


def undelivered(conn, run_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT event_id FROM ctl_input_receipt"
        " WHERE run_id=? AND delivered_at IS NULL ORDER BY rowid", (run_id,)).fetchall()
    return [r["event_id"] for r in rows]


def requeue_untaken(conn, old_run_id: str, new_run_id: str) -> list[str]:
    """Move inputs the old run never consumed to its successor.

    A copy may still wait inside the old session (restart persisted it); an
    INSERT OR IGNORE against rows already carried by the new run takes it out,
    mirroring the donor's "the new delivery is the only one" rule.
    """
    now = time.time()
    moved = []
    for eid in unread(conn, old_run_id):
        cur = conn.execute(
            "INSERT OR IGNORE INTO ctl_input_receipt"
            " (run_id,event_id,delivered_at) VALUES (?,?,?)",
            (new_run_id, eid, now))
        if cur.rowcount:
            conn.execute(
                "DELETE FROM ctl_input_receipt WHERE run_id=? AND event_id=?",
                (old_run_id, eid))
            moved.append(eid)
        else:
            # The successor already holds this input ("a copy still waiting in
            # the session"): drop the old receipt so both sides agree on owner.
            conn.execute(
                "DELETE FROM ctl_input_receipt WHERE run_id=? AND event_id=?",
                (old_run_id, eid))
    return moved


def classify_interrupted(*, last_report_at: float | None,
                         last_delivered_at: float | None) -> bool:
    """Donor ``Assignments.interrupted`` predicate: was the worker busy with new
    input since its latest report? True → restart with :data:`RECOVERY_NOTE`;
    False → settled state, keep session, flush waiting inputs."""
    if last_report_at is None or last_delivered_at is None:
        return True
    return last_delivered_at >= last_report_at
