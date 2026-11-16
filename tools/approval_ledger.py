"""Durable pending-approval ledger (state.db / pending_approvals).

Fixes the dead-letter at the heart of #133514: when a detached background
child (or any gateway path with no live notify callback) hits an approval
gate, ``tools/approval._pending_result`` used to park the request in a
module-global ``_pending`` dict that **no surface ever read** — the request
was unanswerable and silently overwritten by the next one.

This ledger persists every such request so ``/approve`` / ``/deny`` stay
answerable after the parent turn ends, across processes, and (once merged)
across restarts. Rows FIFO-resolve under the same session key the live
gateway queue uses (background children inherit the parent's approval
contextvars via ``thread_context``, so the child's request is already keyed
to the parent conversation).

Design notes:
- Default OFF (``approvals.durable_pending_ledger: false``): behavior is
  byte-identical to the dict path until a deployment flips the flag. The
  in-memory dict is kept as a fast cache so no existing reader regresses.
- Single short-lived connection per operation (same pattern as
  ``tools/async_delegation``): the approval path must never hold DB locks
  across user interaction. Every entry point swallows DB errors — the
  ledger may degrade to the legacy dict, it must never break a tool call.
- No wake plumbing here: resolving a row also records the pattern as
  session-approved (``approve_session``) so a re-run in the same session
  passes the gate without a second card. Notifying the agent that the
  answer arrived is the caller's next turn's job (see PR body for the
  deliberate scope cut).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_approvals (
    approval_id TEXT PRIMARY KEY,
    session_key TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    resolution TEXT,
    resolved_at REAL,
    reason TEXT,
    command TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    pattern_key TEXT NOT NULL DEFAULT '',
    extra_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_pending_approvals_lookup
    ON pending_approvals(session_key, state, created_at);
"""


def _enabled() -> bool:
    try:
        from tools.approval_context import _get_approval_config

        return bool(_get_approval_config().get("durable_pending_ledger", False))
    except Exception:
        # Config subsystem unavailable: treat as flag-off (legacy dict path).
        logger.debug("approval ledger: config read failed; staying on dict path", exc_info=True)
        return False


def _ttl_seconds() -> int:
    try:
        from tools.approval_context import _get_approval_config

        return max(30, int(_get_approval_config().get("pending_ttl_seconds", 1800)))
    except Exception:
        logger.debug("approval ledger: ttl config read failed; using default", exc_info=True)
        return 1800


def _b2_active() -> bool:
    """``approvals.once_and_ttl`` (B2, TZ-2026-11-16-dsh-B): structured
    answers, the pending TTL key, and the denial-code traces in this ledger."""
    try:
        from tools import approval_contract
        return approval_contract.once_and_ttl_enabled()
    except Exception:
        return False


def _b2_ttl() -> int:
    """``approvals.pending_ttl_s`` under the B2 flag (default 3600; 0 = never
    expires). Legacy keeps its own clamped ``pending_ttl_seconds``."""
    try:
        from tools import approval_contract
        return approval_contract.pending_ttl_s()
    except Exception:
        return 3600


def _b2_resolution(choice: str, state: str) -> str:
    """The ledger trace for a resolved row: under the B2 flag a denied row
    carries the stable code ``APPROVAL_REJECTED`` (B2d) instead of the raw
    choice word; grants keep the raw choice (codes are denial codes)."""
    if _b2_active() and state == "denied":
        from tools import approval_contract
        return approval_contract.APPROVAL_REJECTED
    return choice


def _connect() -> sqlite3.Connection:
    from hermes_cli.sqlite_util import open_db
    from hermes_constants import mkdir_under_hermes_home, get_hermes_home
    from hermes_state import _secure_state_db_files

    path = get_hermes_home() / "state.db"
    mkdir_under_hermes_home(path.parent)
    _secure_state_db_files(path, create_main=True)
    conn = open_db(
        path,
        db_label="state.db (approval_ledger)",
        busy_timeout_ms=10_000,
        wal=False,
        row_factory=None,
        initialize=None,
    )
    _secure_state_db_files(path)
    conn.executescript(_SCHEMA)
    return conn


def _gc_expired(conn: sqlite3.Connection, session_key: str | None, now: float) -> None:
    """Lazy GC of pending rows that outlived their TTL (no scheduler; the GC
    runs on every ledger read, same connection, same transaction).

    Legacy: rows flip to 'expired'. B2 flag ON: a pending that outlived its
    TTL auto-resolves — state 'cancelled' with the stable code
    ``APPROVAL_CANCELLED`` in ``resolution`` (B2b: the denial that outran the
    window; a later ``/approve`` cannot revive it, the SELECTs below only see
    ``state='pending'``). ``expires_at = 0`` rows (B2 ``pending_ttl_s: 0`` =
    never expires) are never GC'd."""
    scope = " AND session_key=?" if session_key else ""
    if _b2_active():
        from tools import approval_contract
        conn.execute(
            "UPDATE pending_approvals SET state='cancelled', resolution=?"
            " WHERE state='pending' AND expires_at > 0 AND expires_at < ?" + scope,
            (approval_contract.APPROVAL_CANCELLED, now) + ((session_key,) if session_key else ()),
        )
        return
    conn.execute(
        "UPDATE pending_approvals SET state='expired' WHERE state='pending'"
        " AND expires_at < ?" + scope,
        (now,) + ((session_key,) if session_key else ()),
    )


def record_pending(session_key: str, pending: dict) -> str | None:
    """Durably store one unanswerable approval request. Returns its id, or
    None when the feature is off or the write failed (legacy dict still
    holds the copy in that case). Multiple rows per session accumulate —
    unlike the dict, a later request never overwrites an earlier one."""
    if not _enabled() or not session_key:
        return None
    approval_id = f"ap-{uuid.uuid4().hex[:12]}"
    now = time.time()
    if _b2_active():
        # B2b: approvals.pending_ttl_s (0 = never expires: the GC's
        # ``expires_at > 0`` guard skips the row, a later /approve still
        # answers it — the documented forever-pending mode).
        ttl = _b2_ttl()
        expires_at = 0 if ttl == 0 else now + ttl
    else:
        expires_at = now + _ttl_seconds()
    extra = {
        k: v
        for k, v in pending.items()
        if k not in ("command", "description", "pattern_key", "pattern_keys")
    }
    try:
        conn = _connect()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO pending_approvals (approval_id, session_key, created_at,"
                    " expires_at, state, command, description, pattern_key, extra_json)"
                    " VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
                    (
                        approval_id,
                        session_key,
                        now,
                        expires_at,
                        str(pending.get("command", "")),
                        str(pending.get("description", "")),
                        str(pending.get("pattern_key", "")),
                        json.dumps(extra, default=str),
                    ),
                )
        finally:
            conn.close()
        return approval_id
    except Exception:
        logger.debug(
            "approval ledger: record failed for %s (dict fallback stays)",
            session_key,
            exc_info=True,
        )
        return None


def list_pending(session_key: str) -> list[dict]:
    """Unexpired pending rows for a session, oldest first. Rows past their
    TTL are flipped to 'expired' as a side effect (lazy GC, no scheduler)."""
    if not _enabled() or not session_key:
        return []
    now = time.time()
    try:
        conn = _connect()
        try:
            with conn:
                _gc_expired(conn, session_key, now)
                rows = conn.execute(
                    "SELECT approval_id, created_at, command, description, pattern_key"
                    " FROM pending_approvals"
                    " WHERE session_key=? AND state='pending' ORDER BY created_at ASC",
                    (session_key,),
                ).fetchall()
        finally:
            conn.close()
        return [
            {
                "approval_id": r[0],
                "created_at": r[1],
                "command": r[2],
                "description": r[3],
                "pattern_key": r[4],
            }
            for r in rows
        ]
    except Exception:
        logger.debug("approval ledger: list failed for %s", session_key, exc_info=True)
        return []


def pending_count(session_key: str | None = None) -> int:
    """Unexpired pending rows for one session (or across all sessions when
    *session_key* is None). Same lazy-GC side effect as list_pending."""
    if not _enabled():
        return 0
    now = time.time()
    try:
        conn = _connect()
        try:
            with conn:
                _gc_expired(conn, session_key, now)
                q = "SELECT COUNT(*) FROM pending_approvals WHERE state='pending'" + (
                    " AND session_key=?" if session_key else ""
                )
                n = conn.execute(q, (session_key,) if session_key else ()).fetchone()[0]
        finally:
            conn.close()
        return int(n)
    except Exception:
        logger.debug("approval ledger: count failed (session_key=%s)", session_key, exc_info=True)
        return 0


def _chat_target(session_key: str) -> tuple[str, str] | None:
    """Parse (platform, chat_id) out of a gateway session key
    (``agent:<profile>:<platform>:<scope>:<chat...>``). The chat id itself
    carries colons (``!room:server``), so rebuild it from the first token that
    looks like a room/user id. None when the key has no chat routing."""
    parts = session_key.split(":")
    if len(parts) < 5:
        return None
    platform = parts[2]
    for i, tok in enumerate(parts[3:], start=3):
        if tok.startswith(("!", "#", "@")):
            chat = [tok]
            nxt = parts[i + 1] if i + 1 < len(parts) else ""
            if nxt and "." in nxt and not nxt.startswith("@"):
                chat.append(nxt)  # room/user ids carry their own server part
            return platform, ":".join(chat)
    rest = ":".join(parts[4:])  # numeric chat ids (telegram) have no sigil
    return (platform, rest) if rest and rest.replace(".", "").isdigit() else None


def sweep_restart_orphans(boot_ts: float) -> list[dict]:
    """Return approvals left armed/expired by a gateway restart, grouped per
    chat with a ready-to-send notice (П4, contour gateway-deafness 15.11: a
    restart silently killed the armed waiters — the owner never learned the
    command did not run).

    Rows created *after* *boot_ts* belong to the current process and stay
    untouched. Rows are NOT flipped here: the caller flips only what it
    delivered (``mark_restart_lost``), so a failed delivery retries next
    boot. Never raises — a broken ledger sweep must not affect boot."""
    if not _enabled():
        return []
    try:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT approval_id, session_key, command FROM pending_approvals"
                " WHERE state IN ('pending', 'expired') AND created_at < ?"
                " AND (resolution IS NULL OR resolution = '')",
                (boot_ts,),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        logger.debug("approval ledger: restart-orphan sweep failed", exc_info=True)
        return []
    grouped: dict[str, list[dict]] = {}
    for approval_id, session_key, command in rows:
        grouped.setdefault(session_key, []).append(
            {"approval_id": approval_id, "command": command}
        )
    notices: list[dict] = []
    for session_key, items in grouped.items():
        target = _chat_target(session_key)
        if not target:
            logger.info(
                "approval sweep: %d restart-orphan(s) on unroutable session %s",
                len(items), session_key,
            )
            continue
        commands = ", ".join(i["command"][:60] for i in items[:5])
        notices.append({
            "platform": target[0],
            "chat_id": target[1],
            "approval_ids": [i["approval_id"] for i in items],
            "text": (
                f"[approvals] Gateway restarted with {len(items)} awaiting decision(s)"
                f" — they were NOT executed: {commands}."
                f" Ids: {', '.join(i['approval_id'] for i in items)}"
            ),
        })
    return notices


def mark_restart_lost(approval_ids: list[str]) -> None:
    """Flip delivered orphan rows to ``lost_notice`` (dedup: a row is noticed
    exactly once, ever)."""
    if not approval_ids:
        return
    try:
        conn = _connect()
        try:
            with conn:
                conn.executemany(
                    "UPDATE pending_approvals SET state='lost_notice',"
                    " resolution='restart-lost', resolved_at=?"
                    " WHERE approval_id=? AND state IN ('pending', 'expired')",
                    [(time.time(), aid) for aid in approval_ids],
                )
        finally:
            conn.close()
    except Exception:
        logger.debug("approval ledger: mark_restart_lost failed", exc_info=True)


def resolve_pending(
    session_key: str,
    choice: str,
    *,
    resolve_all: bool = False,
    reason: str | None = None,
    request_id: str | None = None,
) -> list[dict]:
    """Answer durable rows the way ``/approve``/``/deny`` answer the live
    queue: oldest-first FIFO, or ``all``, or one by *request_id*. Marks
    state and returns the resolved rows (for pattern pre-approval and
    logging). A row already resolved/expired/canceled is never double-
    answered: the UPDATE is guarded on state='pending' and rowcount."""
    if not _enabled() or not session_key:
        return []
    now = time.time()
    resolved: list[dict] = []
    try:
        conn = _connect()
        try:
            with conn:
                _gc_expired(conn, session_key, now)
                if request_id:
                    rows = conn.execute(
                        "SELECT approval_id, command, description, pattern_key FROM"
                        " pending_approvals WHERE session_key=? AND state='pending'"
                        " AND approval_id=?",
                        (session_key, request_id),
                    ).fetchall()
                elif resolve_all:
                    rows = conn.execute(
                        "SELECT approval_id, command, description, pattern_key FROM"
                        " pending_approvals WHERE session_key=? AND state='pending'"
                        " ORDER BY created_at ASC",
                        (session_key,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT approval_id, command, description, pattern_key FROM"
                        " pending_approvals WHERE session_key=? AND state='pending'"
                        " ORDER BY created_at ASC LIMIT 1",
                        (session_key,),
                    ).fetchall()
                for r in rows:
                    # B2: the card-tap word "approve" is canonicalized before
                    # the state mapping (legacy maps it to "denied" — a
                    # pre-existing quirk that stays byte-identical under the
                    # legacy flag).
                    eff_choice = "approved" if (_b2_active() and choice == "approve") else choice
                    state_word = (
                        eff_choice
                        if eff_choice in ("approved", "denied")
                        else (
                            "approved"
                            if eff_choice in ("once", "session", "always")
                            else "denied"
                        )
                    )
                    cur = conn.execute(
                        "UPDATE pending_approvals SET state=?, resolution=?, resolved_at=?,"
                        " reason=? WHERE approval_id=? AND state='pending'",
                        (
                            state_word,
                            _b2_resolution(eff_choice, state_word),
                            now,
                            reason,
                            r[0],
                        ),
                    )
                    if cur.rowcount == 1:
                        resolved.append(
                            {
                                "approval_id": r[0],
                                "command": r[1],
                                "description": r[2],
                                "pattern_key": r[3],
                            }
                        )
        finally:
            conn.close()
        return resolved
    except Exception:
        logger.debug(
            "approval ledger: resolve failed for %s", session_key, exc_info=True
        )
        return []


def cancel_session(session_key: str) -> int:
    """Subtree teardown (/stop, /new, session end): every still-pending row
    for the session becomes 'cancelled'. Returns rows cancelled."""
    if not _enabled() or not session_key:
        return 0
    try:
        conn = _connect()
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE pending_approvals SET state='cancelled', resolved_at=?"
                    " WHERE session_key=? AND state='pending'",
                    (time.time(), session_key),
                )
                return cur.rowcount
        finally:
            conn.close()
    except Exception:
        logger.debug(
            "approval ledger: cancel failed for %s", session_key, exc_info=True
        )
        return 0
