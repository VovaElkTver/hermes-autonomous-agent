"""Async follow-ups on the 1 Hz beat (mini-spec journal/20261103-async-followups-spec.md).

A consequence is a ROW in state.db, never a timer object: the ticker wakes, finds
due ``ctl_followup`` rows and acts on them. Restarts lose nothing; an idle beat
still runs zero SQL (the scan rides its own interval counter in
``Ticker._schedule``, not every beat).

Recipient model (v2, принят Вл 04.11): DEFAULT RECIPIENT IS THE AGENT ITSELF.
Fire = enqueue a real controller task into the scope of ``scope_task``
(inherits owner/workdir/project from it) so the agent executes the consequence
like any other queued work — the human never shovels reminder texts back at the
bot. ``recipient='user'`` is an explicit opt-in that keeps the v1 chat-delivery
semantics (repeat beats, exhaustion notice via live port).

Reminder (agent): fire once → dispatched task recorded in ``dispatch_ref``;
the follow-up closes when that task reaches a terminal state (completed /
cancelled). Review (agent): each ``period_s`` a new task is enqueued unless the
previous one is still open (reviews never pile up).

Delivery/enqueue is at-least-once across crash seams: the state flip commits
after the attempt, so a crash seam duplicates at worst. Duplicates are rarer
than losses.
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
RECIPIENTS = ("agent", "user")
_STATES = ("pending", "awaiting_ack", "done", "cancelled")

#: Task states that mean «the dispatched work is still somebody's problem».
_OPEN_TASK_STATES = ("draft", "ready", "running", "blocked", "paused",
                     "needs_human", "verifying", "failed")
_TERMINAL_TASK_STATES = ("completed", "cancelled")


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
          recipient TEXT NOT NULL DEFAULT 'agent'
               CHECK (recipient IN ('agent','user')),
          dispatch_ref TEXT,
          last_fired_at REAL,
          retries INTEGER NOT NULL DEFAULT 0,
          repeat_beats INTEGER NOT NULL DEFAULT 300,
          max_repeats INTEGER NOT NULL DEFAULT 3,
          created_by TEXT NOT NULL,
          created_at REAL NOT NULL,
          provenance_json TEXT,
          expires_at REAL,
          close_reason TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_followup_open ON ctl_followup(state, due_at);
        CREATE INDEX IF NOT EXISTS idx_followup_review ON ctl_followup(state, last_fired_at);
        """
    )
    _ensure_columns(conn)


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Add the A2 columns to a pre-existing ctl_followup table (idempotent).

    The canonical SCHEMA_SQL carries them for fresh DBs; this only fires for a
    state.db created before the columns existed. No-op when already present."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(ctl_followup)")}
    with conn:
        if "expires_at" not in cols:
            conn.execute("ALTER TABLE ctl_followup ADD COLUMN expires_at REAL")
        if "close_reason" not in cols:
            conn.execute("ALTER TABLE ctl_followup ADD COLUMN close_reason TEXT")


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
        scope_project: str | None = None, recipient: str = "agent",
        repeat_beats: int = DEFAULT_REPEAT_BEATS,
        max_repeats: int = DEFAULT_MAX_REPEATS, created_by: str = "agent-proposal",
        provenance: dict | None = None, now: float | None = None,
        decision_ttl_s: int | None = None) -> str:
    """Register one follow-up. Reminder needs ``due_at``; review needs ``period_s``.
    Rings the ticker wake seam (go Вл 09.11): a reminder set for the next beat
    is counted off on the next beat, not at the next followups deadline.

    A2 (plan_review TTL borrow): an optional ``decision_ttl_s`` puts a deadline
    on the pending decision (``expires_at = now + ttl``). When that deadline
    passes with no decision the row closes as ``close_reason='expired'`` rather
    than waiting forever. ``decision_ttl_s=None`` (the default) leaves no
    deadline — the legacy unbounded-wait behaviour (the OFF path)."""
    if kind == "reminder" and due_at is None:
        raise ValueError("reminder requires due_at")
    if kind == "review" and not period_s:
        raise ValueError("review requires period_s")
    if kind not in KINDS:
        raise ValueError(f"invalid followup kind {kind!r}")
    if recipient not in RECIPIENTS:
        raise ValueError(f"invalid recipient {recipient!r}")
    if decision_ttl_s is not None and decision_ttl_s <= 0:
        raise ValueError("decision_ttl_s must be a positive second count or None")
    now = now if now is not None else time.time()
    expires_at = (now + float(decision_ttl_s)) if decision_ttl_s is not None else None
    ensure_schema(conn)
    fid = _new_id(conn, "fup")
    with conn:
        conn.execute(
            """INSERT INTO ctl_followup
               (followup_id, kind, scope_task, scope_project, text, due_at,
                period_s, state, recipient, repeat_beats, max_repeats,
                created_by, created_at, provenance_json, expires_at, close_reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (fid, kind, scope_task, scope_project, text, due_at, period_s,
             "pending", recipient, repeat_beats, max_repeats, created_by, now,
             json.dumps(provenance, ensure_ascii=False) if provenance else None,
             expires_at, None))
    from . import wake
    wake.notify()
    return fid


def cancel(conn: sqlite3.Connection, followup_id: str, reason: str = "") -> bool:
    ensure_schema(conn)
    with conn:
        cur = conn.execute(
            "UPDATE ctl_followup SET state='cancelled', close_reason='cancelled' "
            "WHERE followup_id=? AND state IN ('pending','awaiting_ack')",
            (followup_id,))
    if cur.rowcount:
        logger.info("followup %s closed: cancelled (%s)", followup_id, reason or "explicit")
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


def _task_status(conn: sqlite3.Connection, task_id: str) -> str | None:
    row = conn.execute("SELECT state FROM ctl_task WHERE task_id=?", (task_id,)).fetchone()
    return row["state"] if row else None


def _dispatch_task(conn: sqlite3.Connection, row: dict) -> str:
    """Enqueue the consequence as a real controller task in its scope's lane."""
    from . import model, transitions
    scope = conn.execute(
        "SELECT owner, project_id, workdir FROM ctl_task WHERE task_id=?",
        (row["scope_task"],)).fetchone()
    if scope is None:
        raise LookupError(f"scope task vanished: {row['scope_task']}")
    goal = (f"[followup {row['followup_id']}] {row['text']}"
            + (f" (периодический обзор раз в {row['period_s']}с)" if row["kind"] == "review" else ""))
    tid = model.create_task(conn, owner=scope["owner"], workdir=scope["workdir"],
                            goal=goal, project_id=scope["project_id"] or "")
    # The consequence IS its own mandate source (created_by carries the authorizer);
    # contract v1 is self-agreed, otherwise every reminder would dead-lock on review.
    model.agree_contract(conn, tid)
    transitions.transition(conn, tid, "ready", basis=f"followup:{row['followup_id']}")
    return tid


def _fire_agent_row(conn: sqlite3.Connection, row: dict, deliver, now: float) -> str | None:
    """Agent-recipient fire: dispatch a task, mind the previous one. Returns rid if fired."""
    rid = row["followup_id"]
    prev = row["dispatch_ref"]
    status = _task_status(conn, prev) if prev else "no-dispatch-yet"
    in_flight = status in _OPEN_TASK_STATES
    if prev:
        if row["kind"] == "reminder":
            if in_flight:
                return None               # previous work still somebody's problem
            # terminal (completed/cancelled) or vanished row = the agent handled it,
            # a human stopped it, or its own lifecycle cleaned it up. Either way
            # the reminder has nothing left to wait for.
            with conn:
                conn.execute(
                    "UPDATE ctl_followup SET state='done', close_reason='decided' "
                    "WHERE followup_id=?", (rid,))
            logger.info("followup %s closed: decided (dispatched task terminal)", rid)
            return None
        if in_flight:
            return None                   # reviews never pile up
        # review: previous cycle finished → slot free, enqueue next cycle below
    try:
        tid = _dispatch_task(conn, row)
    except LookupError as exc:
        # Scope gone and nobody left to own this: say it out loud once, then rest.
        note = (f"[controller] следствие {rid} не исполнить — исходная задача "
                f"{row['scope_task']} недоступна: {row['text'][:100]}")
        logger.warning("followup %s dispatch impossible: %s", rid, exc)
        _deliver_one({"text": note}, deliver)
        with conn:
            conn.execute("UPDATE ctl_followup SET state='done' WHERE followup_id=?", (rid,))
        return None
    except Exception as exc:                  # transient queue trouble → retry window
        logger.debug("followup %s dispatch deferred: %s", rid, exc)
        with conn:
            conn.execute("UPDATE ctl_followup SET last_fired_at=?, retries=retries+1 WHERE followup_id=?",
                         (now, rid))
        return None
    next_state = "awaiting_ack" if row["kind"] == "reminder" else row["state"]
    with conn:
        conn.execute(
            "UPDATE ctl_followup SET state=?, dispatch_ref=?, last_fired_at=?, retries=retries+1"
            " WHERE followup_id=?", (next_state, tid, now, rid))
    return rid


def _expire_row(conn: sqlite3.Connection, row: dict, deliver, now: float) -> None:
    """A2: close a pending decision whose TTL has elapsed with no decision.

    Terminal ``state='done'`` + ``close_reason='expired'`` (distinct from
    ``'cancelled'`` and ``'decided'``). Host policy is to escalate with a single
    notice rather than park the decision forever."""
    rid = row["followup_id"]
    note = (f"[controller] review decision {rid} expired without a decision "
            f"(TTL elapsed): {row['text'][:100]}")
    logger.warning("followup %s expired without a decision (TTL elapsed)", rid)
    _deliver_one({"text": note}, deliver)
    with conn:
        conn.execute(
            "UPDATE ctl_followup SET state='done', close_reason='expired', "
            "last_fired_at=? WHERE followup_id=?", (now, rid))


def scan(conn: sqlite3.Connection, deliver, *, now: float | None = None,
         beat_s: float = 1.0) -> list[str]:
    """Find due follow-ups, act on them, advance state. Returns fired ids.

    Call on its own interval counter (NOT every beat — doctrine: idle beats run
    zero SQL). agent rows → task dispatch; user rows → chat delivery with the
    v1 repeat/exhaustion cadence.
    """
    now = now if now is not None else time.time()
    fired: list[str] = []
    ensure_schema(conn)   # self-heal: the A2 columns exist even on a migrated DB
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM ctl_followup WHERE state IN ('pending','awaiting_ack') "
        "ORDER BY COALESCE(due_at, last_fired_at, created_at)")]
    for row in rows:
        rid = row["followup_id"]
        last = row["last_fired_at"]
        base = row["created_at"] if last is None else last   # review period counts from creation

        # A2: a pending decision whose deadline has passed closes as 'expired'
        # (distinct from 'cancelled'/'decided') and is escalated once, instead
        # of parking forever. Only rows that carry a deadline (expires_at set)
        # are affected; a NULL deadline keeps the legacy unbounded wait (OFF).
        if row["expires_at"] is not None and now >= row["expires_at"]:
            _expire_row(conn, row, deliver, now)
            continue

        if row["recipient"] == "agent":
            due_now = (
                (row["kind"] == "reminder" and row["state"] == "pending"
                 and row["due_at"] is not None and now >= row["due_at"])
                or (row["kind"] == "reminder" and row["state"] == "awaiting_ack")
                or (row["kind"] == "review" and now >= base + (row["period_s"] or 0))
            )
            if due_now:
                # Cheap per-scan work: _fire_agent_row holds while the dispatched
                # task is open, closes the row when it turns terminal.
                if _fire_agent_row(conn, row, deliver, now) == rid:
                    fired.append(rid)
            continue

        # ---- explicit user-recipient path: v1 chat delivery -------------------
        window_ok = last is None or (now - last) >= row["repeat_beats"] * beat_s
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
