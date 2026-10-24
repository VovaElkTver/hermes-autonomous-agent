"""controller.transport — transport-observation layer (agency-model §16.9).

The layer between a platform adapter (currently Matrix) and the semantic
journal (the ``messages`` table): it records what the TRANSPORT saw — before
anything was normalized or journaled — so phantom-inbound ("an event in the
session with no matching room event") becomes detectable instead of silent.
Three tables live in state.db next to the ctl_* set
(schema: hermes_state_common.SCHEMA_SQL):

* ``transport_observations`` — one row per raw observation on one channel
  (``sync`` | ``event_fetch`` | ``relations`` | ``local_echo``);
* ``event_observations`` — one row per distinct external event, with status
  tracked apart from the normalized messages;
* ``reconciliation_checks`` — one row per interface re-check.

Design rules from the spec:

* An observation's default status is ``unverified_transport_observation`` —
  not a room fact, not a basis for external action.
* A failed fetch is a reconciliation result WITH an error code
  (``result='fetch_failed'`` + ``error_code``), never a silent "event did not
  exist".
* No single ``verified`` flag: "is this event verified?" is computed from the
  six characteristics of the newest check (``method``, ``against_source``,
  ``result``, ``error_code``, ``checked_at``, ``evidence_ref``).
* A re-check against the same homeserver proves the INTERFACE answered
  (recorded in ``against_source``), not independent truth.

Stages: 1 (this module + the adapter write-path) only records — nothing is
gated on it yet. Stage 2 wires ``pending``/``reconcile`` to a periodic runner;
stage 3 gates external actions on the unverified observations.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid

# Channels: where the raw observation was made.
CHANNELS = ("sync", "event_fetch", "relations", "local_echo")

# event_observations.status values.
UNVERIFIED = "unverified_transport_observation"
CONFIRMED = "confirmed_via_interface"
DIVERGENT = "divergent"
NOT_FOUND = "not_found_in_source"

# reconciliation_checks.result values.
CHECK_RESULTS = ("matched", "diverged", "not_found", "fetch_failed")

# No single "verified" flag: this is the full characteristic set of one check;
# a verdict is always derived from these, never asserted as a boolean.
CHECK_FIELDS = ("method", "against_source", "result", "error_code", "checked_at", "evidence_ref")

_tls = threading.local()


def connection() -> sqlite3.Connection:
    """Per-thread state.db connection — same opener as controller.model
    (state.db next to the ctl_* set; adapter callbacks arrive on worker
    threads and sqlite3 enforces thread affinity, so no module-global conn).
    The profile scope is whatever was active at first use in the thread:
    the Matrix adapter calls this inside connect(), which runs in the owning
    profile's scope."""
    conn = getattr(_tls, "conn", None)
    if conn is None:
        from controller import model
        conn = model.connect()
        _tls.conn = conn
    return conn


def payload_ref(payload) -> str | None:
    """Stable identity reference for a raw payload WITHOUT storing the
    payload itself (E2EE room content must not be duplicated into a log
    table): ``sha256:<hex>`` of the canonical JSON. Two identical raw
    payloads share one ref, so a phantom duplicate of the same event shows up
    as repeated refs without ever persisting the content."""
    if payload is None:
        return None
    if isinstance(payload, (bytes, bytearray)):
        raw = bytes(payload)
    elif isinstance(payload, str):
        raw = payload.encode("utf-8")
    else:
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                         default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def record_observation(conn, *, connector: str, account: str, device: str | None = None,
                       channel: str, room_id: str, external_event_id: str | None = None,
                       sync_batch_id: str | None = None, raw_payload_ref: str | None = None,
                       event_kind: str | None = None, sender: str | None = None,
                       observed_at: float | None = None) -> str | None:
    """Record one raw transport observation and upsert the event-level row for
    the same external event (first-seen wins; later re-observations only
    advance ``last_observed_at`` — they never re-verify an event). Returns the
    event_observations obs_id, or None when the observation has no external
    event id to key on. ``observed_at`` is message_time (§16.6): the
    transport/server timestamp when known, else now()."""
    if channel not in CHANNELS:
        raise ValueError(f"unknown channel {channel!r}; expected one of {CHANNELS}")
    now = float(observed_at) if observed_at else time.time()
    conn.execute(
        "INSERT INTO transport_observations"
        " (obs_id, connector, account, device, channel, room_id, external_event_id,"
        "  sync_batch_id, raw_payload_ref, observed_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (f"to-{uuid.uuid4().hex[:12]}", connector, account, device, channel, room_id,
         external_event_id, sync_batch_id, raw_payload_ref, now))
    event_obs_id: str | None = None
    if external_event_id:
        event_obs_id = f"eo-{uuid.uuid4().hex[:12]}"
        conn.execute(
            "INSERT INTO event_observations"
            " (obs_id, connector, external_event_id, room_id, event_kind, sender,"
            "  status, first_observed_at, last_observed_at)"
            " VALUES (?,?,?,?,?,?,?, ?,?)"
            " ON CONFLICT(connector, external_event_id) DO UPDATE SET"
            "  last_observed_at=excluded.last_observed_at,"
            "  room_id=excluded.room_id,"
            "  event_kind=COALESCE(excluded.event_kind, event_observations.event_kind),"
            "  sender=COALESCE(excluded.sender, event_observations.sender)",
            (event_obs_id, connector, external_event_id, room_id, event_kind,
             sender, UNVERIFIED, now, now))
        event_obs_id = conn.execute(
            "SELECT obs_id FROM event_observations"
            " WHERE connector=? AND external_event_id=?",
            (connector, external_event_id)).fetchone()[0]
    conn.commit()
    return event_obs_id


def event_obs_for(conn, connector: str, external_event_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM event_observations WHERE connector=? AND external_event_id=?",
        (connector, external_event_id)).fetchone()
    return dict(row) if row else None


def link_journal(conn, connector: str, external_event_id: str, journal_ref: str) -> None:
    """Tie the observation to the semantic-journal row it produced (stage 3
    reads this to know which session content rests on which transport fact;
    until then it is inert)."""
    conn.execute(
        "UPDATE event_observations SET journal_ref=?"
        " WHERE connector=? AND external_event_id=?",
        (journal_ref, connector, external_event_id))
    conn.commit()


def record_check(conn, event_obs_id: str, *, method: str, against_source: str,
                 result: str, error_code: str | None = None,
                 evidence_ref: str | None = None, detail: str | None = None,
                 checked_at: float | None = None) -> str:
    """Record one reconciliation check — the six characteristics, no boolean.
    Status update rules: matched -> confirmed_via_interface, diverged ->
    divergent, not_found -> not_found_in_source; fetch_failed leaves the
    status UNCHANGED (a failed check proves nothing; the event stays
    unverified and the check row itself carries the error code)."""
    if result not in CHECK_RESULTS:
        raise ValueError(f"unknown check result {result!r}; expected one of {CHECK_RESULTS}")
    if result == "fetch_failed" and not error_code:
        raise ValueError(
            "fetch_failed without an error_code: a failed fetch must carry the machine "
            "code, not a verdict")
    now = float(checked_at) if checked_at else time.time()
    row = conn.execute("SELECT obs_id FROM event_observations WHERE obs_id=?",
                       (event_obs_id,)).fetchone()
    if row is None:
        raise LookupError(f"no such event observation: {event_obs_id}")
    check_id = f"rc-{uuid.uuid4().hex[:12]}"
    conn.execute(
        "INSERT INTO reconciliation_checks"
        " (check_id, obs_id, checked_at, method, against_source, result, error_code,"
        "  detail, evidence_ref, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (check_id, event_obs_id, now, method, against_source,
         result, error_code, detail, evidence_ref, now))
    if result == "fetch_failed":
        conn.execute(
            "UPDATE event_observations SET last_checked_at=?, last_check_result=?"
            " WHERE obs_id=?", (now, result, event_obs_id))
    else:
        status = {"matched": CONFIRMED, "diverged": DIVERGENT, "not_found": NOT_FOUND}[result]
        conn.execute(
            "UPDATE event_observations SET status=?, last_checked_at=?, last_check_result=?"
            " WHERE obs_id=?", (status, now, result, event_obs_id))
    conn.commit()
    return check_id


def is_verified(conn, event_obs_id: str) -> bool:
    """DERIVED verdict, never a stored flag (spec: no single `verified`):
    True iff the NEWEST check for this event is a clean interface match —
    result='matched', no error_code, and an against_source recorded (a check
    against nothing proves nothing)."""
    row = conn.execute(
        "SELECT result, error_code, against_source FROM reconciliation_checks"
        " WHERE obs_id=? ORDER BY checked_at DESC, created_at DESC LIMIT 1",
        (event_obs_id,)).fetchone()
    if row is None:
        return False
    return row["result"] == "matched" and row["error_code"] is None and bool(row["against_source"])


def pending(conn, *, limit: int = 100, now: float | None = None,
            min_gap_s: float = 300.0) -> list[dict]:
    """Unverified event observations due for a re-check, oldest first.

    Only status ``unverified_transport_observation`` is eligible: a
    fetch_failed check keeps that status (so the event is retried), while
    matched/diverged/not_found have reached a verdict and are not re-queued
    here. ``min_gap_s`` throttles re-checks: an event whose last check is
    younger than the gap is not retried (a failing homeserver must not be
    hammered every tick). Stage 2's periodic runner walks this list; rows
    carry ``stored_ref`` (the newest raw_payload_ref) so a re-fetch result
    can be compared without a second query."""
    ts = time.time() if now is None else float(now)
    rows = conn.execute(
        "SELECT e.obs_id, e.connector, e.room_id, e.external_event_id, e.status,"
        "  e.first_observed_at, e.last_checked_at,"
        "  (SELECT t.raw_payload_ref FROM transport_observations t"
        "   WHERE t.obs_id=e.obs_id AND t.raw_payload_ref IS NOT NULL"
        "   ORDER BY t.observed_at DESC LIMIT 1) AS stored_ref"
        " FROM event_observations e"
        " WHERE e.status=? AND e.external_event_id IS NOT NULL"
        "   AND (e.last_checked_at IS NULL OR e.last_checked_at <= ?)"
        " ORDER BY e.first_observed_at ASC LIMIT ?",
        (UNVERIFIED, ts - min_gap_s, limit)).fetchall()
    return [dict(r) for r in rows]


def reconcile(conn, fetch, *, limit: int = 100, source_label: str = "",
              min_gap_s: float = 300.0, now: float | None = None) -> dict:
    """One periodic reconciliation pass (spec §16.9: a re-check against the
    same homeserver verifies the interface, not independent truth).

    ``fetch(room_id, external_event_id)`` re-queries the live transport and
    returns ``(raw_payload_ref, error_code)``:
      * both None           -> the source does not report the event: 'not_found'
      * error_code not None -> the fetch itself failed: 'fetch_failed' with the
                               machine code; the observation stays UNVERIFIED
      * raw_payload_ref     -> compared with the stored ref: equal -> 'matched',
                               different -> 'diverged' (evidence_ref = new ref)
    A fetch callable that raises is captured as 'fetch_failed' with
    ``error_code_of``. Every outcome writes one reconciliation_checks row
    (method='event_refetch', against_source=source_label). Returns a count
    summary; the rows are the audit, the counts are all a caller needs."""
    due = pending(conn, limit=limit, now=now, min_gap_s=min_gap_s)
    checked_at = time.time() if now is None else float(now)
    summary = {"checked": 0, "matched": 0, "diverged": 0, "not_found": 0, "fetch_failed": 0}
    for row in due:
        try:
            ref, err = fetch(row["room_id"], row["external_event_id"])
        except Exception as exc:
            ref, err = None, error_code_of(exc)
        summary["checked"] += 1
        if err:
            result, evidence, detail = "fetch_failed", None, None
        elif ref is None:
            result, evidence, detail = "not_found", None, None
        elif row.get("stored_ref") and ref != row["stored_ref"]:
            result, evidence, detail = "diverged", ref, \
                "re-fetched ref differs from stored raw_payload_ref"
        else:
            result, evidence, detail = "matched", ref, None
        record_check(conn, row["obs_id"], method="event_refetch",
                     against_source=source_label, result=result,
                     error_code=err, evidence_ref=evidence, detail=detail,
                     checked_at=checked_at)
        summary[result] += 1
    return summary


def error_code_of(exc: BaseException) -> str:
    """Machine error code for a failed transport fetch. Matrix-first: mautrix
    raises MatrixRequestError with ``errcode``/``http_status`` for every
    non-2xx (M_NOT_FOUND, M_FORBIDDEN, ...); timeouts and dropped links get
    their own codes. A check MUST carry one — "the event did not exist"
    without a code is exactly the silent failure this layer exists to kill."""
    errcode = getattr(exc, "errcode", None)
    if isinstance(errcode, str) and errcode.strip():
        return errcode.strip().upper()
    status = getattr(exc, "http_status", None)
    if isinstance(status, int):
        return f"http_{status}"
    import asyncio
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "timeout"
    try:
        from mautrix.api import MatrixConnectionError
        if isinstance(exc, MatrixConnectionError):
            return "connection"
    except Exception:
        pass
    return "unknown"
