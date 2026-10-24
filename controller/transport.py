"""controller.transport — the transport-observations layer (agency-model.md §16.9,
hermes-agency-003 §7).

The layer between the platform adapters and the semantic journal. It exists
because the transport itself can lie to the journal (phantom-inbound: an event
the agent saw that the room does not contain — synthetic re-insertions after a
tool-result or a session restore). Three append-only tables live next to the
ctl_* set in state.db:

  transport_observations  raw "the transport delivered this" records
                          (connector/account/device, channel sync|event_fetch|
                          relations|local_echo, raw_payload_ref, sync_batch_id,
                          external_event_id, observed_at)
  event_observations      canonical event identity plus SIX independent
                          characteristics. No single `verified` flag by spec:
                          identity_consistency / server_retrievability /
                          decryption_status / redaction_status /
                          timeline_placement / normalization_consistency
  reconciliation_checks   one row per reconciliation pass over an observation;
                          a failed fetch is stored as a result WITH an error
                          code — it is evidence about the interface, not proof
                          the event never existed

Statuses: an inbound that cannot be tied back to its source stays
`unverified_transport_observation` — not a room fact, not a basis for an
external action (the external-action gate is stage 3). An event the transport
re-delivers in a LATER sync batch (the phantom-inbound signature) is stamped
`synthetic_redelivery` at record time.

All writes to the transport_* / event_observations / reconciliation_checks
tables go through this module — same single-writer invariant as ctl_*.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid

from controller import model as _model

CHANNELS = frozenset({"sync", "event_fetch", "relations", "local_echo"})

CHARACTERISTICS = (
    "identity_consistency",
    "server_retrievability",
    "decryption_status",
    "redaction_status",
    "timeline_placement",
    "normalization_consistency",
)

# transport_observations.status values
UNVERIFIED = "unverified_transport_observation"
SYNTHETIC = "synthetic_redelivery"
VERIFIED = "verified"

# reconciliation_checks.result values
CHECK_FETCH_FAILED = "fetch_failed"
CHECK_METHOD_REFETCH = "refetch_same_homeserver"  # interface check, not independent truth (§7)

#: After this many failed refetches an observation is left `unverified_transport_observation`
#: terminally (a check row with error_code='retry_exhausted' records why) instead of
#: re-hammering a homeserver that keeps answering with an error.
FETCH_RETRY_LIMIT = 3


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def open_db() -> sqlite3.Connection:
    """Canonical state.db opener: same barriers/authority as ctl_* (one DDL authority)."""
    return _model._connect()


def record_observation(
    conn: sqlite3.Connection, *, connector: str, channel: str, room_id: str,
    event_id: str, event_type: str = "", content_hash: str = "", sender: str = "",
    relation: dict | None = None, account: str = "", device: str = "",
    sync_batch_id: str = "", observed_at: float | None = None,
) -> dict:
    """Record one transport observation plus its canonical event observation.

    Returns {"observation_id", "event_obs_id", "status"}. An event already
    observed by the same account in a DIFFERENT sync batch is stamped
    `synthetic_redelivery` — the transport re-injected it (phantom-inbound),
    so it must not be read as a fresh room fact.

    decryption_status is settled at record time from what the adapter actually
    saw: a plaintext event is `plain`; an event that still arrived encrypted
    (no decryptor could run) is `undecryptable` and can never verify.
    """
    if channel not in CHANNELS:
        raise ValueError(f"unknown transport channel: {channel!r}")
    now = time.time() if observed_at is None else observed_at
    dup = conn.execute(
        "SELECT 1 FROM transport_observations"
        " WHERE connector=? AND account=? AND external_event_id=? AND sync_batch_id<>?"
        " LIMIT 1",
        (connector, account, event_id, sync_batch_id),
    ).fetchone()
    status = SYNTHETIC if dup else UNVERIFIED
    decryption = "undecryptable" if event_type == "m.room.encrypted" else "plain"
    obs_id, ev_id = _new_id("tobs"), _new_id("evobs")
    conn.execute(
        "INSERT INTO transport_observations"
        " (observation_id, connector, account, device, channel, room_id,"
        "  external_event_id, event_type, raw_payload_ref, sync_batch_id, status, observed_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (obs_id, connector, account, device, channel, room_id, event_id,
         event_type, content_hash, sync_batch_id, status, now),
    )
    conn.execute(
        "INSERT INTO event_observations"
        " (event_obs_id, observation_id, room_id, event_id, event_type, sender,"
        "  content_hash, relation, normalized_ref, identity_consistency,"
        "  server_retrievability, decryption_status, redaction_status,"
        "  timeline_placement, normalization_consistency, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ev_id, obs_id, room_id, event_id, event_type, sender, content_hash,
         json.dumps(relation or {}, ensure_ascii=False, sort_keys=True), "",
         "pending", "pending", decryption, "pending", "pending", "pending", now),
    )
    conn.commit()
    return {"observation_id": obs_id, "event_obs_id": ev_id, "status": status}


def _select_pending(conn: sqlite3.Connection, account: str | None, limit: int) -> list[sqlite3.Row]:
    """Event observations still worth a refetch: retrievability pending, no terminal
    check yet, optionally scoped to one account."""
    return conn.execute(
        "SELECT eo.event_obs_id, eo.observation_id, eo.room_id, eo.event_id,"
        "       eo.event_type, eo.sender, eo.relation, eo.decryption_status,"
        "       tobs.status AS obs_status, tobs.account AS account"
        " FROM event_observations eo"
        " JOIN transport_observations tobs ON tobs.observation_id = eo.observation_id"
        " WHERE (? IS NULL OR tobs.account = ?)"
        "   AND eo.server_retrievability = 'pending'"
        "   AND NOT EXISTS (SELECT 1 FROM reconciliation_checks rc"
        "                   WHERE rc.observation_id = tobs.observation_id"
        "                     AND rc.result IN (?,?,?))"
        " ORDER BY eo.created_at LIMIT ?",
        (account, account, SYNTHETIC, VERIFIED, UNVERIFIED, limit),
    ).fetchall()


def _placement(relation_json: str) -> str:
    """Timeline placement from the stored relation metadata (§7: relations may not land in
    any single timeline — "not in the UI" != "a fantasy"; placement is a property of the
    observation, settled here from what the sync batch told us)."""
    try:
        rel = json.loads(relation_json or "{}")
    except (ValueError, TypeError):
        return "unknown"
    if not isinstance(rel, dict):
        return "unknown"
    if rel.get("m.thread"):
        return "thread_timeline"
    if rel.get("m.in_reply_to"):
        return "main_fallback_reply"
    return "main"


def _record_check(conn: sqlite3.Connection, row: sqlite3.Row, *, method: str,
                  result: str, error_code: str = "", detail: str = "") -> dict:
    check_id = _new_id("recon")
    conn.execute(
        "INSERT INTO reconciliation_checks"
        " (check_id, observation_id, method, account, result, error_code, detail, checked_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (check_id, row["observation_id"], method, row["account"], result,
         error_code, detail, time.time()),
    )
    conn.commit()
    return {"check_id": check_id, "observation_id": row["observation_id"],
            "result": result, "error_code": error_code}


async def reconcile(conn: sqlite3.Connection, *, fetch_event, account: str | None = None,
                    limit: int = 50) -> list[dict]:
    """One reconciliation pass over pending event_observations.

    ``fetch_event`` is an async callable ``(room_id, event_id) -> dict``:
      success: {"type": str, "sender": str, "content": dict|None, "redacted": bool}
      failure: {"error_code": str}  — it NEVER raises; a failed fetch is a
      check result with an error code, not a claim the event was not there.

    The refetch goes to the SAME homeserver that delivered the event: an
    interface cross-check, deliberately not an independent truth claim (§7).
    Returns one check record per observation processed.
    """
    results: list[dict] = []
    for row in _select_pending(conn, account, limit):
        if row["obs_status"] == SYNTHETIC:
            # Terminal, no fetch: the transport itself re-injected the event in a
            # later batch. It is cut off here — this layer must not let it
            # become a room fact.
            results.append(_record_check(
                conn, row, method=CHECK_METHOD_REFETCH, result=SYNTHETIC,
                detail="re-delivered by the transport in a later sync batch; not a room fact"))
            continue
        failed = conn.execute(
            "SELECT COUNT(*) AS c FROM reconciliation_checks"
            " WHERE observation_id=? AND result=?",
            (row["observation_id"], CHECK_FETCH_FAILED),
        ).fetchone()["c"]
        if failed >= FETCH_RETRY_LIMIT:
            conn.execute(
                "UPDATE transport_observations SET status=? WHERE observation_id=?",
                (UNVERIFIED, row["observation_id"]))
            conn.commit()
            results.append(_record_check(
                conn, row, method=CHECK_METHOD_REFETCH, result=UNVERIFIED,
                error_code="retry_exhausted",
                detail=f"{failed} failed refetches; left unverified_transport_observation"))
            continue
        fetched = await fetch_event(row["room_id"], row["event_id"])
        error_code = str(fetched.get("error_code") or "")
        if error_code:
            # The fetch failed: store the RESULT with the error code. Do not
            # touch server_retrievability — "unknown" stays unknown until a
            # refetch succeeds or retries exhaust.
            results.append(_record_check(
                conn, row, method=CHECK_METHOD_REFETCH, result=CHECK_FETCH_FAILED,
                error_code=error_code,
                detail="failed fetch is a check result, not proof of absence"))
            continue
        identity = "ok" if (fetched.get("sender") == row["sender"]
                            and fetched.get("type") == row["event_type"]) else "mismatch"
        redaction = "redacted" if fetched.get("redacted") else "ok"
        placement = _placement(row["relation"])
        # Verified needs agreement on identity + retrievability + redaction, and
        # the event must not be undecryptable. normalization_consistency stays
        # pending until a journal reference is recorded (stage 3) — the verdict
        # must not silently depend on it.
        verified = (identity == "ok" and redaction == "ok"
                    and row["decryption_status"] != "undecryptable")
        result = VERIFIED if verified else UNVERIFIED
        conn.execute(
            "UPDATE event_observations SET identity_consistency=?,"
            " server_retrievability='ok', redaction_status=?, timeline_placement=?"
            " WHERE event_obs_id=?",
            (identity, redaction, placement, row["event_obs_id"]),
        )
        conn.execute(
            "UPDATE transport_observations SET status=? WHERE observation_id=?",
            (result, row["observation_id"]),
        )
        conn.commit()
        results.append(_record_check(
            conn, row, method=CHECK_METHOD_REFETCH, result=result,
            detail="" if verified else "characteristic mismatch; not a room fact"))
    return results
