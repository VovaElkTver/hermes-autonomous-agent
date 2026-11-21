"""controller.decide — S2.6 (ТЗ thinking-enable): the «решить, что делать» event.

A wake-turn is proposed to the live agent ONLY on non-empty material:
  * new terminal transitions (needs_human / completed / blocked) past the
    watermark — the queue produced a fact that needs a decision;
  * same-reason noop counters that crossed the escalation threshold —
    someone (possibly this agent) has been sitting N times in a row;
  * queue empty + permission on + material on file (drafts, parked
    needs_human, open todo notes, unassessed observations) — the quiet
    moment is exactly when backlog decisions fit.

Empty queue WITHOUT material never wakes anyone: "пустая очередь ≠ терн"
(Вл 21.11 — the anti-theatre-of-activity rule; periodic LLM ticks are a
load generator, not liveliness, doctrine 25.10).

Outcomes are closed and enumerated for the woken agent: draft (ctl_enqueue),
reprioritize / noop (ctl_decision). noop is an ASSERTED SUCCESS there, with
the same reason counter policy as steps (threshold provisional, Вл 21.11).

State: one monotonic watermark per trigger family in ``ctl_decision_state``.
The watermark survives restarts (state.db is the memory); first run anchors
at boot ts so historic rows never produce a wake storm — historic
needs_human tasks remain visible through the material branch.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time

from . import model

#: terminal states whose arrival is decision material
TRIGGER_STATES = ("needs_human", "completed", "blocked")

#: escalation threshold for repeated identical noop reasons. PROVISIONAL
#: (Вл 21.11: «число три условное, жизнь покажет»); the named direction is
#: timestamp-parametrization, which needs only ctl_event.ts — data already
#: being written since day one.
NOOP_ESCALATE_AT = 3

_WATERMARK_KEY = "decide:watermark"
_MAX_LINES = 12  # hard cap: the digest is a wake signal, not a report


def ensure_schema(conn: sqlite3.Connection) -> None:
    # statement-by-statement: callers may hold an open write transaction
    conn.execute("CREATE TABLE IF NOT EXISTS ctl_decision_state"
                 "(key TEXT PRIMARY KEY, value REAL NOT NULL)")


def _watermark(conn) -> float:
    row = conn.execute("SELECT value v FROM ctl_decision_state WHERE key=?",
                       (_WATERMARK_KEY,)).fetchone()
    if row is not None:
        return float(row["v"])
    now = time.time()
    conn.execute("INSERT OR IGNORE INTO ctl_decision_state(key, value) VALUES (?,?)",
                 (_WATERMARK_KEY, now))
    conn.commit()
    return now


def _advance(conn, ts: float) -> None:
    conn.execute("INSERT INTO ctl_decision_state(key, value) VALUES (?,?)"
                 " ON CONFLICT(key) DO UPDATE SET value=excluded.value WHERE"
                 " ctl_decision_state.value < excluded.value",
                 (_WATERMARK_KEY, ts))
    conn.commit()


def _counts(conn) -> dict:
    def one(sql):
        return conn.execute(sql).fetchone()[0]
    return {
        "drafts": one("SELECT COUNT(*) FROM ctl_task WHERE state='draft'"),
        "needs_human": one("SELECT COUNT(*) FROM ctl_task WHERE state='needs_human'"),
        "todo_open": one("SELECT COUNT(*) FROM ctl_todo"),
        "unassessed": one("SELECT COUNT(*) FROM investigation_observations"
                          " WHERE assessed=0"),
    }


def scan(conn, *, now: float | None = None) -> tuple[list[str], dict]:
    """Pure reader: (digest lines, stats). Advances nothing — the caller
    advances the watermark only after the wake was attempted, so a failed
    delivery gets another look next tick (durable in state.db, no loss)."""
    ensure_schema(conn)
    now = time.time() if now is None else now
    wm = _watermark(conn)
    lines: list[str] = []
    hi = wm
    trig = conn.execute(
        "SELECT ts, task_id, to_state, basis FROM ctl_state_transition"
        " WHERE ts > ? AND to_state IN ('needs_human','completed','blocked')"
        " ORDER BY ts LIMIT 20", (wm,)).fetchall()
    for r in trig:
        hi = max(hi, float(r["ts"]))
        lines.append(f"- переход {r['task_id'][-8:]} → {r['to_state']}: "
                     f"{(r['basis'] or '')[:80]}")
    noop_rows = conn.execute(
        "SELECT ts, kind, payload FROM ctl_event WHERE kind IN"
        " ('step_noop','decision_noop') AND ts > ?"
        " ORDER BY ts LIMIT 50", (wm,)).fetchall()
    for r in noop_rows:
        hi = max(hi, float(r["ts"]))
        try:
            p = json.loads(r["payload"])
        except (ValueError, TypeError):
            continue
        if int(p.get("same_reason_count") or 0) == NOOP_ESCALATE_AT:
            lines.append(f"- noop x{NOOP_ESCALATE_AT} с одной причиной "
                         f"({r['kind'] if 'kind' in r.keys() else 'noop'}, "
                         f"step {str(p.get('step_id') or '')[-8:] or '—'}): "
                         f"{str(p.get('reason') or '')[:80]} — пора решить, "
                         f"а не посидеть ещё")
    stats = _counts(conn)
    material = stats["drafts"] + stats["needs_human"] + stats["todo_open"] \
        + stats["unassessed"]
    queue_empty = conn.execute(
        "SELECT COUNT(*) FROM ctl_task WHERE state IN ('ready','running')"
    ).fetchone()[0] == 0
    perm_on = conn.execute(
        "SELECT 1 FROM ctl_autonomy_permission WHERE enabled=1 LIMIT 1"
    ).fetchone() is not None
    if queue_empty and perm_on and material:
        lines.append("- очередь пуста, разрешение вкл; материал: "
                     + ", ".join(f"{k}={v}" for k, v in stats.items() if v)
                     + " — решаемое окно")
    return lines[:_MAX_LINES], {"material": material, "queue_empty": queue_empty,
                                "perm_on": perm_on, "hi": hi, "wm": wm}


def decide_tick(conn, *, deliver_fn, now: float | None = None) -> bool:
    """One decide tick. True when a wake was sent. Delivery rides the same
    chat channel as alerts (alert_target); the digest header spells out the
    closed outcome menu so the woken turn cannot drift into theatre."""
    lines, st = scan(conn, now=now)
    if not lines:
        # nothing new: still move past scanned-but-dry transition rows only
        # when they produced no line (impossible today) — keep wm untouched
        return False
    header = "DECIDE (S2.6): событийный терн «решить, что делать». Выходы: " \
             "ctl_enqueue (draft), ctl_decision reprioritize|noop(reason). " \
             "Третьего нет; noop без причины не ассертится."
    body = header + "\n" + "\n".join(lines)
    sig = hashlib.sha1(body.encode()).hexdigest()[:12]
    if _shown_recently(conn, sig):
        # same digest within cooldown: not a new decision moment; the
        # watermark still advances — this moment has already been shown.
        if st["hi"] > st["wm"]:
            _advance(conn, st["hi"])
        return False
    if not deliver_fn(body):
        return False  # transport down: try again next beat (watermark kept)
    _mark_shown(conn, sig)
    model.emit(conn, kind="decision-wake", origin="controller.decide",
               payload={"lines": len(lines), "sig": sig})
    if st["hi"] > st["wm"]:
        _advance(conn, st["hi"])
    return True


def _shown_recently(conn, sig: str) -> bool:
    """Dedup the SAME digest for an hour: re-waking for an unchanged decision
    moment is the storm this whole design exists to prevent. Checked before
    delivery, marked ONLY after — a failed send must not consume the hour."""
    row = conn.execute("SELECT value v FROM ctl_decision_state WHERE key=?",
                       (f"decide:last:{sig}",)).fetchone()
    return row is not None and time.time() - float(row["v"]) < 3600.0


def _mark_shown(conn, sig: str) -> None:
    conn.execute("INSERT INTO ctl_decision_state(key, value) VALUES (?,?)"
                 " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (f"decide:last:{sig}", time.time()))
    conn.commit()
