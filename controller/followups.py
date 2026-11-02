"""Async follow-ups on the 1 Hz beat (mini-spec journal/20261103-async-followups-spec.md).

A consequence is a ROW in state.db, never a timer object: the ticker wakes, finds
due ``ctl_followup`` rows, and delivers them through the live chat port. Restarts
lose nothing; an idle beat still runs zero SQL (the scan rides its own interval
counter in ``Ticker._schedule``, not every beat).

Reminder semantics (v1, donor-referenced): fire at/after ``due_at`` → repeat every
``repeat_beats`` beats until ``max_repeats`` attempts → final "не дозвонилось"
notice + done (donor evals: REQUEUES → unavailable). Ack-capture is OUT of v1 —
the human may cancel any follow-up explicitly; a reminder that annoyed without a
cancel is a spec question, not lost work. Review = periodic by ``period_s``,
stays pending forever until cancelled.

Delivery is at-least-once across crash seams: the state flip to awaiting_ack is
a separate commit from the send itself. Duplicates are rarer than losses.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time

logger = logging.getLogger("controller.followups")

#: Default retry cadence for reminders (beats over the 1 Hz base beat).
DEFAULT_REPEAT_BEATS = 300
DEFAULT_MAX_REPEATS = 3

KINDS = ("reminder", "review")
_STATES = ("pending", "awaiting_ack", "done", "cancelled")


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create ctl_followup when absent (additive, canonical SCHEMA_SQL keeps the
    copy too — this guards fresh test connections built from older schemas)."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ctl_followup (
          followup_id TEXT PRIMARY KEY,
          kind TEXT NOT NULL CHECK (kind IN ('reminder','review')),
          scope_task TEXT NOT NULL,
          scope_project TEXT,
          text TEXT NOT NULL,
          due_at REAL,
          period_s INTEGER,
          state TEXT NOT NULL DEFAULT 'pending'
               CHECK (state IN ('pending','awaiting_ack','done','cancelled')),
          last_fired_at REAL,
          retries INTEGER NOT NULL DEFAULT 0,
          repeat_beats INTEGER NOT NULL DEFAULT 300,
          max_repeats INTEGER NOT NULL DEFAULT 3,
          created_by TEXT NOT NULL,
          created_at REAL NOT NULL,
          provenance_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_followup_open ON ctl_followup(state, due_at);
        CREATE INDEX IF NOT EXISTS idx_followup_review ON ctl_followup(state, last_fired_at);
        """
    )


def _new_id(conn: sqlite3.Connection, prefix: str) -> str:
    from . import model
    for _ in range(5):
        fid = model.new_id(prefix)
        dup = conn.execute("SELECT 1 FROM ctl_followup WHERE followup_id = ?", (fid,)).fetchone()
        if not dup:
            return fid
    raise RuntimeError("followup id exhaustion")


def add(conn: sqlite3.Connection, *, kind: str, scope_task: str, text: str,
        due_at: float | None = None, period_s: int | None = None,
        scope_project: str | None = None, repeat_beats: int = DEFAULT_REPEAT_BEATS,
        max_repeats: int = DEFAULT_MAX_REPEATS, created_by: str = "agent-proposal",
        provenance: dict | None = None, now: float | None = None) -> str:
    """Register one follow-up. Reminder needs ``due_at``; review needs ``period_s``."""
    if kind == "reminder" and due_at is None:
        raise ValueError("reminder requires due_at")
    if kind == "review" and not period_s:
        raise ValueError("review requires period_s")
    if kind not in KINDS:
        raise ValueError(f"invalid followup kind {kind!r}")
    now = now if now is not None else time.time()
    ensure_schema(conn)
    fid = _new_id(conn, "fup")
    with conn:
        conn.execute(
            """INSERT INTO ctl_followup
               (followup_id, kind, scope_task, scope_project, text, due_at,
                period_s, state, repeat_beats, max_repeats, created_by,
                created_at, provenance_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (fid, kind, scope_task, scope_project, text, due_at, period_s,
             "pending", repeat_beats, max_repeats, created_by, now,
             json.dumps(provenance) if provenance else None))
    return fid


def cancel(conn: sqlite3.Connection, followup_id: str, reason: str = "") -> bool:
    ensure_schema(conn)
    with conn:
        cur = conn.execute(
            "UPDATE ctl_followup SET state='cancelled' WHERE followup_id=? AND state IN ('pending','awaiting_ack')",
            (followup_id,))
    return bool(cur.rowcount)


def list_open(conn: sqlite3.Connection, scope_task: str | None = None) -> list[dict]:
    sql = "SELECT * FROM ctl_followup WHERE state IN ('pending','awaiting_ack')"
    params: tuple = ()
    if scope_task:
        sql += " AND scope_task=?"
        params = (scope_task,)
    return [dict(r) for r in conn.execute(sql + " ORDER BY due_at", params)]


def _deliver_one(receipt: dict, deliver) -> bool:
    """One send attempt with a ``{"text": ...}`` receipt (live_ports contract:
    default_deliver unwraps the receipt itself; gateway deliver_fn gets text).
    True = transport accepted; False/exception = retryable."""
    try:
        return bool(deliver(receipt))
    except Exception as exc:
        logger.debug("followup delivery refused: %s", exc)
        return False


def scan(conn: sqlite3.Connection, deliver, *, now: float | None = None,
         beat_s: float = 1.0) -> list[str]:
    """Find due follow-ups, attempt delivery, advance state. Returns fired ids.

    Call on its own interval counter (NOT every beat — doctrine: idle beats run
    zero SQL). Idempotency key per attempt is carried by the caller's transport;
    state flips commit after the attempt so a crash seam duplicates at worst.
    """
    now = now if now is not None else time.time()
    fired: list[str] = []
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM ctl_followup WHERE state IN ('pending','awaiting_ack') "
        "ORDER BY COALESCE(due_at, last_fired_at, created_at)")]
    for row in rows:
        rid = row["followup_id"]
        last = row["last_fired_at"]
        window_ok = last is None or (now - last) >= row["repeat_beats"] * beat_s
        base = row["created_at"] if last is None else last   # review period counts from creation
        deadline_reached = (
            row["kind"] == "reminder" and row["state"] == "pending"
            and row["due_at"] is not None and now >= row["due_at"]
            and window_ok                                    # backoff after refused send
        ) or (
            row["kind"] == "reminder" and row["state"] == "awaiting_ack"
            and (now - (last or 0)) >= row["repeat_beats"] * beat_s
        ) or (
            row["kind"] == "review" and row["state"] == "pending"
            and now >= base + (row["period_s"] or 0)
        )
        if not deadline_reached:
            continue
        exhausted = row["kind"] == "reminder" and row["retries"] >= row["max_repeats"]
        if exhausted:
            # Donor parity (evals REQUEUES→unavailable): say it out loud once, then rest.
            note = (f"[controller] напоминание {rid} не дозвонилось за "
                    f"{row['max_repeats']} попыток: {row['text'][:120]}")
            _deliver_one({"text": note}, deliver)
            with conn:
                conn.execute("UPDATE ctl_followup SET state='done' WHERE followup_id=?", (rid,))
            continue
        ok = _deliver_one({"text": row["text"]}, deliver)
        # Success: pending→awaiting_ack (reminder) or stays pending (review).
        # Refused send: row keeps its state, but the window restarts — a broken
        # transport must not be hammered every scan, nor lose the row.
        next_state = "awaiting_ack" if (row["kind"] == "reminder" and ok) else row["state"]
        with conn:
            conn.execute(
                "UPDATE ctl_followup SET state=?, last_fired_at=?, retries=retries+1 WHERE followup_id=?",
                (next_state, now, rid))
        if ok:
            fired.append(rid)
    return fired
