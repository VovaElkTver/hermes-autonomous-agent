"""Update-interrupt receipt — a kill that knows it was a kill (fleet Д1b, incident 28.11 Win).

When ``hermes update`` pauses the gateway, the gateway defers stop() up to
``_restart_after_turn_timeout`` so turns drain, but the updater (old builds) or a service
deadline can still SIGKILL mid-turn. The turn then fences ``unknown`` at boot and its FIFO
bounces every future message — although the pause moment KNEW the turn was unfinished.

The gateway therefore writes a durable receipt naming the in-flight sessions at pause time;
boot recovery settles their ``unknown`` rows once, as ``update_interrupted`` (never replayed,
with a user-facing notice), instead of leaving the session permanently paused. TTL bounds the
receipt to one update window: an entry older than the largest deferral budget is stale.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Receipts outlive only the update that wrote them: the largest deferral budget (1800s)
# plus teardown and boot grace. Past that, an unfinalised entry says nothing reliable.
RECEIPT_TTL_S = 3600.0


def _receipt_path(home) -> Path:
    return Path(home) / "state" / "update_interrupt_receipt.json"


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _store(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".uir-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def active_turn_sessions(db) -> list[str]:
    """Conversation ids holding a turn lease right now (best effort; never raises)."""
    try:
        with db._read_ctx() as conn:
            return [r[0] for r in conn.execute(
                "SELECT conversation_id FROM session_turn_leases")]
    except Exception:
        logger.debug("update-interrupt receipt: lease query failed", exc_info=True)
        return []


def write_receipt(home, session_ids, *, now: float | None = None) -> dict:
    """Stamp the sessions in flight at pause time. Merges with any unpaid receipt."""
    now = time.time() if now is None else now
    path = _receipt_path(home)
    data = _load(path)
    entries = data.get("sessions") or {}
    for sid in session_ids:
        entries[str(sid)] = {"written_at": now}
    data["sessions"] = entries
    _store(path, data)
    return data


def take_verdict(home, session_id, *, now: float | None = None) -> dict | None:
    """Consume one receipt entry for ``session_id``: verdict on first read only, and only
    inside the TTL. Persists the remainder."""
    now = time.time() if now is None else now
    path = _receipt_path(home)
    data = _load(path)
    entries = data.get("sessions") or {}
    entry = entries.pop(str(session_id), None)
    if entry is None:
        return None
    if entries:
        _store(path, data)
    else:
        try:
            path.unlink()
        except OSError:
            pass
    written = float(entry.get("written_at") or 0.0)
    if now - written > RECEIPT_TTL_S:
        return None
    return entry


def settle_update_interrupted(db, epoch, home) -> int:
    """Boot recovery beside ``settle_recovered_unattended``: an ``unknown`` row for a session
    named by a live receipt is a KNOWN update interruption — settle it terminal (never
    replayed) with a person-facing notice, so the FIFO moves instead of bouncing every message.
    Runs synchronously off the loop, before any FIFO is scheduled."""
    from agent.turn_failure_copy import UPDATE_INTERRUPTED_NOTICE
    from gateway.session_ingress_media import release_admission_media
    from gateway.session_results import close_discarded_turn
    from hermes_state_runtime import RuntimeStoreError, _row, resolve_unknown_session_input

    with db._read_ctx() as conn:
        rows = [r for r in (_row(x) for x in conn.execute(
            "SELECT * FROM session_admissions WHERE status='unknown' ORDER BY seq").fetchall())
            if r is not None]
    settled = 0
    for row in rows:
        if take_verdict(home, row.get("target_session_id")) is None:
            continue
        try:
            resolve_unknown_session_input(
                db, epoch=epoch, admission_id=row["admission_id"], generation=row["generation"],
                _terminal_write=lambda conn, lost: close_discarded_turn(
                    db, conn, lost, UPDATE_INTERRUPTED_NOTICE))
        except RuntimeStoreError as exc:
            logger.warning("Update-interrupted admission %s left unknown: %s",
                           row["admission_id"], exc.reason)
            continue
        release_admission_media(db, row["admission_id"])
        settled += 1
        logger.info("Admission %s on session %s settled as update_interrupted (not replayed)",
                    row["admission_id"], row.get("target_session_id"))
    return settled
