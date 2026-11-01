"""controller.disposition — where an absorbed event went (port #1).

Ported from dsh-superagent-prototype ``intake.ts``/``model.ts``
TriggerDisposition + ``followupOwners`` @6337fc8 (audit 2026-11-01; mechanism
donor, runtime NOT adopted). ``ctl_event.idempotency_key`` already absorbs a
duplicate delivery; the disposition records what became of it:

- ``fanout``: task ids the event reached as an input;
- ``context``: of those, tasks that only FOLLOW the thread for context — it is
  none of theirs and grants nothing;
- ``handbacks``: runs that returned the event as beyond their task, once each,
  with why;
- ``acked``: runs that acknowledged/answered it, taking it up.

:func:`followup_owners` is the routing verdict for a human follow-up: which
live claims decide it. An event no owner decides goes back to central
assessment instead of being lost between tasks.
"""
from __future__ import annotations

import json
import time


def record(conn, event_id: str, *, fanout=(), context=(), handbacks=(),
           acked=(), requeued=False, note="") -> None:
    """Create or extend a disposition. Handbacks accumulate, one per task."""
    conn.execute(
        "INSERT INTO ctl_event_disposition"
        " (event_id,fanout,context,handbacks,acked,requeued,note,at)"
        " VALUES (?,?,?,?,?,?,?,?)"
        " ON CONFLICT(event_id) DO UPDATE SET"
        " context=excluded.context, handbacks=excluded.handbacks,"
        " acked=excluded.acked, requeued=excluded.requeued, note=excluded.note",
        (event_id, json.dumps(list(fanout)), json.dumps(list(context)),
         json.dumps(list(handbacks)), json.dumps(list(acked)),
         int(requeued), note, time.time()))


def handback(conn, event_id: str, task_id: str, reason: str) -> bool:
    """A run returns a follow-up as beyond its task; each task hands back once."""
    d = get(conn, event_id)
    if d is None:
        raise LookupError(event_id)
    if any(h["task_id"] == task_id for h in d["handbacks"]):
        return False
    hs = d["handbacks"] + [{"task_id": task_id, "reason": reason}]
    record(conn, event_id, fanout=d["fanout"], context=d["context"],
           handbacks=hs, acked=d["acked"], requeued=d["requeued"],
           note=d["note"])
    return True


def get(conn, event_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM ctl_event_disposition WHERE event_id=?", (event_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    for key in ("fanout", "context", "handbacks", "acked"):
        d[key] = json.loads(d[key] or "[]")
    d["requeued"] = bool(d["requeued"])
    return d


def followup_owners(conn, event_id: str, *, human: bool,
                    live_task_ids) -> list[str]:
    """Which live tasks decide a person's follow-up (donor ``followupOwners``).

    A non-human event owns nothing. Handed-back and context-only deliveries do
    not create ownership; an explicit requeue to assessment drops ownership.
    """
    if not human:
        return []
    d = get(conn, event_id)
    if d is None or d["requeued"]:
        return []
    returned = {h["task_id"] for h in d["handbacks"]}
    ctx = set(d["context"])
    reached = [t for t in d["fanout"]
               if t not in returned and t not in ctx]
    live = set(live_task_ids)
    return [t for t in reached if t in live]


def unowned_tasks(conn, event_id: str, live_task_ids) -> list[str]:
    """Tasks among `live_task_ids` whose claim the disposition negates."""
    d = get(conn, event_id)
    if d is None:
        return []
    owned = set(followup_owners(conn, event_id, human=True,
                                live_task_ids=live_task_ids))
    return [t for t in live_task_ids if t not in owned]
