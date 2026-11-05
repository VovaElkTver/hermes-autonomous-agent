"""controller.outbox — reliable outbound message delivery (port #5).

Ported from dsh-superagent-prototype ``src/app/outbox.ts`` + ``store.ts`` receipts
@6337fc8 (audit 2026-11-01; mechanism donor, runtime NOT adopted). Semantics kept:

- message ids are idempotent: recording the same id twice keeps its first body
  and no second effect is queued;
- messages to one conversation of one task go out in queue order: a later message
  waits behind an earlier pending one until that one is sent — or has failed
  FAILURES_BEFORE_ERROR times in a row, which surfaces it as a problem instead;
- a message settles as ``sent``, or as ``dropped`` with a permanent reason when its
  target can never receive it; a dropped receipt is kept forever so the id never
  queues again;
- a message withdrawn while being sent is recorded with its settled state, so its
  id cannot queue again (donor ``settle`` recreate-on-race guard).

Delivery goes through :class:`controller.effects.Effects` kind ``outbox_reply``
(retry 5 s → 300 s like the donor), so sends survive restarts.
"""
from __future__ import annotations

import time

from .effects import FAILURES_BEFORE_ERROR, Later


class Outbox:
    """Outbound receipts on ``ctl_outbox``; actual sending via ``deliver``."""

    def __init__(self, conn, deliver, effects=None, now=None):
        self.conn = conn
        self._deliver_fn = deliver
        self._now = now or time.time
        self.effects = effects
        if effects is not None:
            effects.handle("outbox_reply", self._deliver_effect, retry=(5.0, 300.0))

    # -- producers ------------------------------------------------------------

    def send(self, msg_id: str, text: str, conv_key: str,
             task_id: str | None = None, kind: str = "reply") -> bool:
        """Record a receipt and enqueue delivery; returns whether newly recorded."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO ctl_outbox"
            " (msg_id,task_id,conv_key,kind,text,status,created_at)"
            " VALUES (?,?,?,?,?, 'pending', ?)",
            (msg_id, task_id, conv_key, kind, text, self._now()))
        fresh = bool(cur.rowcount)
        if fresh and self.effects is not None:
            self.effects.enqueue("outbox_reply", msg_id)
        return fresh

    def withdraw(self, msg_id: str) -> bool:
        """Take back a not-yet-sent message. Sent or dropped receipts stay."""
        cur = self.conn.execute(
            "DELETE FROM ctl_outbox WHERE msg_id=? AND status='pending'", (msg_id,))
        if self.effects is not None and cur.rowcount:
            self.effects.cancel("outbox_reply", msg_id)
        return bool(cur.rowcount)

    # -- reads ------------------------------------------------------------------

    def get(self, msg_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM ctl_outbox WHERE msg_id=?", (msg_id,)).fetchone()
        return dict(row) if row else None

    def status(self, msg_id: str):
        r = self.get(msg_id)
        return r["status"] if r else None

    def waiting(self, conv_key: str | None = None) -> list[dict]:
        sql = "SELECT * FROM ctl_outbox WHERE status='pending'"
        args: tuple = ()
        if conv_key is not None:
            sql += " AND conv_key=?"
            args = (conv_key,)
        rows = self.conn.execute(sql + " ORDER BY created_at, rowid", args).fetchall()
        return [dict(r) for r in rows]

    def behind(self, msg_id: str, failures: int = FAILURES_BEFORE_ERROR) -> bool:
        """True while an earlier pending message of the same task+conversation still
        blocks this one (donor ``receipts.behind``): same conversation key, same task
        (NULL matches NULL), earlier in this table's insertion order, and fewer than
        ``failures`` recorded delivery attempts so far."""
        row = self.conn.execute(
            "SELECT 1 FROM ctl_outbox earlier JOIN ctl_outbox msg ON msg.msg_id=?"
            " LEFT JOIN ctl_effect eff ON eff.kind='outbox_reply' AND eff.key=earlier.msg_id"
            " WHERE earlier.conv_key=msg.conv_key AND earlier.rowid<msg.rowid"
            "   AND ((earlier.task_id IS msg.task_id)"
            "        OR (earlier.task_id IS NULL AND msg.task_id IS NULL))"
            "   AND IFNULL(eff.attempts,0) < ? LIMIT 1", (msg_id, failures))
        return row.fetchone() is not None

    # -- dispatch (the Effects handler for `outbox_reply`) -----------------------

    def _deliver_effect(self, _payload, key: str) -> None:
        receipt = self.get(key)
        if receipt is None or receipt["status"] != "pending":
            return
        if self.behind(key):
            raise Later("an earlier message of this task to this conversation waits")
        result = self._deliver_fn(dict(receipt))
        if result == "gone":
            self._settle(receipt, sent=False, dropped="conversationGone")
        elif result == "sent":
            self._settle(receipt, sent=True)
        else:
            raise ValueError(f"deliver() returned {result!r}; want 'sent' or 'gone'")

    def _settle(self, receipt: dict, *, sent: bool, dropped: str | None = None) -> None:
        """Mark sent/dropped permanently; on a pending-row miss (withdrawn mid-send),
        re-record the receipt already settled, per donor ``settle``."""
        status = "sent" if sent else "dropped"
        cur = self.conn.execute(
            "UPDATE ctl_outbox SET status=?, dropped_reason=?"
            " WHERE msg_id=? AND status='pending'",
            (status, dropped, receipt["msg_id"]))
        if not cur.rowcount:
            self.conn.execute(
                "INSERT OR IGNORE INTO ctl_outbox"
                " (msg_id,task_id,conv_key,kind,text,status,dropped_reason,created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (receipt["msg_id"], receipt["task_id"], receipt["conv_key"],
                 receipt["kind"], receipt["text"], status, dropped,
                 receipt["created_at"]))
