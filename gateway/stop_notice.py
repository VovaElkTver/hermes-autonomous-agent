"""One-shot user-stop recovery note.

When the user stops a live turn (``/stop``, or a ``/new`` / ``/reset`` that latches a Stop on the
running generation) the interrupted plan stays in the transcript. The generic partial-failed-turn
boundary the runner writes for it is a ``display_kind=failed_turn`` row that the context compressor
strips from the model's history, so the model never learns the plan was killed and keeps re-running
it on the next message ("pre-stop context" continuation).

This module fixes that by staging a model-visible control note for the session's NEXT turn (an
api_content sidecar, like the auto-reset and dialogue-watcher notes) that says the plan is no longer
active and the next message is about the post-stop state. A user stop is the OPPOSITE of a gateway
restart/shutdown: a restart auto-continues (``resume_pending``), a user stop must not.

State lives in ``state.db state_meta`` under a one-shot key (it survives a restart). It is written
at the interrupted turn's settlement and consumed exactly once, on the following turn of the
session. Both entry points are best-effort and never raise: a failure here must not break a turn's
settlement or a turn's start.
"""
from __future__ import annotations

import json
import logging
import time
from collections import Counter

logger = logging.getLogger(__name__)

_KEY_PREFIX = "user_stop_note:"


def _key(session_id: str) -> str:
    return _KEY_PREFIX + session_id


def _default_note() -> str:
    from agent.turn_failure_copy import USER_STOP_RECOVERY_NOTE
    return USER_STOP_RECOVERY_NOTE


def compose_stop_note(db, session_id: str) -> str | None:
    """Data block for the stopped turn, built from the tool ledger (never from narration).

    Baskets, side-effecting tools only (the read-only tail is noise): CONFIRMED = a result row
    without the executor's interrupt marker; UNKNOWN = interrupted mid-run, or requested with no
    result row, or persisted with ``effect_disposition='unknown'``. Returns None when the turn
    holds no side-effecting calls (the caller then keeps the static note). Never raises."""
    try:
        from agent.replay_cleanup import is_interrupted_tool_result
        from agent.tool_result_classification import tool_may_have_side_effect
    except Exception:
        return None
    try:
        with db._read_ctx() as conn:
            anchor = conn.execute(
                "SELECT MAX(id) FROM messages WHERE session_id=? AND role='user'",
                (session_id,)).fetchone()[0]
            if anchor is None:
                return None
            rows = conn.execute(
                "SELECT role, tool_name, tool_call_id, tool_calls, content, effect_disposition "
                "FROM messages WHERE session_id=? AND id>? ORDER BY id",
                (session_id, anchor)).fetchall()
    except Exception:
        logger.debug('stop_notice: ledger read failed for session %s', session_id, exc_info=True)
        return None
    try:
        requested: dict[str, str] = {}
        for role, _tname, _tcid, tcs, _content, _disp in rows:
            if role != "assistant" or not tcs:
                continue
            try:
                calls = json.loads(tcs)
            except Exception:
                calls = []
            for call in calls or []:
                fn = str((call.get("function") or {}).get("name") or "")
                cid = str(call.get("id") or call.get("call_id") or "")
                if fn and cid:
                    requested[cid] = fn
        done: Counter[str] = Counter()
        unknown: Counter[str] = Counter()
        answered: set[str] = set()
        for role, tname, tcid, _tcs, content, disp in rows:
            if role != "tool" or not tcid:
                continue
            answered.add(str(tcid))
            name = tname or requested.get(str(tcid), "")
            if not name or not tool_may_have_side_effect(name):
                continue
            if disp == "unknown" or is_interrupted_tool_result(content):
                unknown[name] += 1
            else:
                done[name] += 1
        for cid, name in requested.items():
            if cid not in answered and tool_may_have_side_effect(name):
                unknown[name] += 1
        if not done and not unknown:
            return None

        def _fmt(counter: Counter) -> str:
            return ", ".join(f"{k}×{v}" if v > 1 else k for k, v in sorted(counter.items())) or "none observed"

        return (_default_note()
                + "\nSide effects confirmed executed (result rows in the tool ledger): " + _fmt(done)
                + "\nSide effects with UNKNOWN outcome (interrupted mid-run or never answered): " + _fmt(unknown)
                + "\nInspect the UNKNOWN ones before retrying anything; do not re-execute the stopped plan.")
    except Exception:
        logger.debug('stop_notice: compose failed for session %s', session_id, exc_info=True)
        return None


def is_user_stop(result, outcome, row=None) -> bool:
    """True when the settled turn was interrupted by the USER (a /stop or a /new /reset that latched
    a Stop), never by a gateway restart/shutdown (those auto-continue, opposite semantics) or a
    timeout/eviction. ``result`` is the in-process result wrapper (``{'result': {...}}``); the cause
    is the ``interrupt_message`` the turn recorded when it was interrupted."""
    if outcome != 'interrupted':
        return False
    cause = ((result or {}).get('result') or {}).get('interrupt_message')
    if not cause:
        return False
    # Imported lazily: gateway.run is a heavyweight module and must not be pulled in at import time
    # of this settlement-time helper.
    from gateway.run import _INTERRUPT_REASON_RESET, _INTERRUPT_REASON_STOP
    return cause in (_INTERRUPT_REASON_STOP, _INTERRUPT_REASON_RESET)


def mark_stop_note(db, session_id: str, note: str | None = None) -> None:
    """Stage a one-shot user-stop note for ``session_id``; the next ``_hmwa_prepare_turn`` pops it.

    Idempotent (``ON CONFLICT DO UPDATE``). Never raises."""
    if not session_id:
        return
    payload = json.dumps({'note': note or compose_stop_note(db, session_id) or _default_note(),
                          'ts': time.time()})
    try:
        db._write_sql(
            "INSERT INTO state_meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (_key(session_id), payload),
        )
    except Exception:
        logger.debug('stop_notice: mark failed for session %s', session_id, exc_info=True)


def pop_stop_note(db, session_id: str) -> list[str]:
    """Consume the pending user-stop note(s) for this session (once per turn).

    Returns the note texts (at most one) or ``[]`` when none is pending. Never raises."""
    notes: list[str] = []
    if not session_id:
        return notes
    key = _key(session_id)
    try:
        row = db._read_one("SELECT value FROM state_meta WHERE key=?", (key,))
        if row is None:
            return notes
        db._write_rowcount("DELETE FROM state_meta WHERE key=?", (key,))
        data = json.loads(row[0])
        note = data.get('note') if isinstance(data, dict) else None
        if note:
            notes.append(note)
    except Exception:
        logger.debug('stop_notice: pop failed for session %s', session_id, exc_info=True)
    return notes
