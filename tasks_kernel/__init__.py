"""tasks_kernel — task objects with event-driven continuation (fork v0).

A task is a row in ``state.db`` (table ``tk_task``), not a line in someone's
memory. The lifecycle is a cyclic state machine:

    ready --> running --> wait_human --> running --> ... --> done
                          wait_event ─────┘            ↘ cancelled

``wait_human`` and ``wait_event`` are ordinary parked states: the task sleeps,
nothing polls it. It resumes when an external producer fires its event:

* the Matrix bridge fires ``on_owner_message(chat_id)`` for every inbound owner
  message — a queue head in ``wait_human`` is completed and the next ``ready``
  task starts (its "action" is delivering one message to the chat);
* any other producer calls :func:`emit(event_id)`; tasks that set
  ``wait_event`` to that id resume.

Design constraints carried from the 06.11 review:
- платформенная нейтральность: ядро не знает Matrix, deliver_target — строка
  грамматики ``hermes send -t`` (напр. ``matrix:!ROOM:host``);
- state живёт на диске и переживает рестарт шлюза;
- delivery детерминированный — subprocess ``hermes send``, без LLM-хода
  (тот же приём, что controller/alerts.py);
- цикл «съел ход владельца → следующая задача» НЕ требует памяти агента:
  порядок задач = ORDER BY seq в БД.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
import uuid

READY = "ready"
RUNNING = "running"
WAIT_HUMAN = "wait_human"
DONE = "done"
CANCELLED = "cancelled"

_OPEN_STATES = (READY, RUNNING, WAIT_HUMAN)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tk_task (
    task_id   TEXT PRIMARY KEY,
    chat_id   TEXT NOT NULL,
    seq       INTEGER NOT NULL,
    title     TEXT NOT NULL,
    body      TEXT NOT NULL DEFAULT '',
    state     TEXT NOT NULL DEFAULT 'ready',
    wait_event TEXT,
    note      TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS tk_task_chat ON tk_task (chat_id, seq);
"""


def _db_path() -> str:
    return os.environ.get(
        "HERMES_STATE_DB",
        os.path.join(os.environ.get("HERMES_HOME")
                     or os.path.expanduser("~/.hermes"),
                     "state.db"))


def _connect(db_path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or _db_path(), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def create_batch(chat_id: str, items: list[dict], deliver_target: str = "",
                 db_path: str | None = None) -> list[str]:
    """Append tasks (one per item {title, body}) preserving order. Returns ids."""
    now = time.time()
    with _connect(db_path) as conn:
        init_schema(conn)
        base = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM tk_task "
                            "WHERE chat_id=?", (chat_id,)).fetchone()[0]
        ids = []
        for off, item in enumerate(items, start=1):
            tid = uuid.uuid4().hex[:12]
            conn.execute(
                "INSERT INTO tk_task (task_id, chat_id, seq, title, body, state,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (tid, chat_id, base + off, item.get("title") or f"task-{off}",
                 item.get("body", ""), READY, now, now))
            ids.append(tid)
        conn.commit()
    if deliver_target:
        # remember target on every task of this batch for standalone delivery
        with _connect(db_path) as conn:
            for tid in ids:
                conn.execute("UPDATE tk_task SET note=? WHERE task_id=? AND note IS NULL",
                             (json.dumps({"deliver_target": deliver_target}), tid))
            conn.commit()
    return ids


def deliver_default(task: dict) -> bool:
    """Deterministic delivery through ``hermes send`` (no LLM, no agent loop)."""
    note = task.get("note") or ""
    try:
        meta = json.loads(note) if note else {}
    except ValueError:
        meta = {}
    target = meta.get("deliver_target", "")
    if not target:
        return False
    line = f"[задача] {task['title']}: {task['body']}".strip(": ")
    try:
        p = subprocess.run(["hermes", "send", "-t", target, "-q", line],
                           capture_output=True, text=True, timeout=60, check=False)
        return p.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


deliver_fn = deliver_default  # overridable seam for tests / in-process adapters


def _deliver_and_park(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    """running → deliver → wait_human. Delivery failure keeps RUNNING and is
    raised as ValueError: caller decides retry policy; never silently lost."""
    task = _row_to_dict(row)
    assert task is not None
    conn.execute("UPDATE tk_task SET state=?, updated_at=? WHERE task_id=?",
                 (RUNNING, time.time(), task["task_id"]))
    conn.commit()
    ok = deliver_fn(task)
    if not ok:
        raise ValueError(f"delivery failed for task {task['task_id']}")
    conn.execute("UPDATE tk_task SET state=?, updated_at=? WHERE task_id=?",
                 (WAIT_HUMAN, time.time(), task["task_id"]))
    conn.commit()
    return task


def start(chat_id: str, db_path: str | None = None) -> dict | None:
    """Begin the oldest ready task of the chat (if nothing runs yet)."""
    with _connect(db_path) as conn:
        init_schema(conn)
        active = conn.execute(
            "SELECT * FROM tk_task WHERE chat_id=? AND state IN (?,?)"
            " ORDER BY seq LIMIT 1", (chat_id, RUNNING, WAIT_HUMAN)).fetchone()
        if active is not None:
            return _row_to_dict(active)          # already walking
        head = conn.execute(
            "SELECT * FROM tk_task WHERE chat_id=? AND state=? ORDER BY seq LIMIT 1",
            (chat_id, READY)).fetchone()
        if head is None:
            return None
        return _deliver_and_park(conn, head)


def on_owner_message(chat_id: str, db_path: str | None = None) -> dict | None:
    """Owner spoke in this chat: close the wait_human head, start the next ready.

    Returns the task that was just delivered (or the still-active head if the
    queue is already drained), None when there was nothing to advance.
    """
    with _connect(db_path) as conn:
        init_schema(conn)
        head = conn.execute(
            "SELECT * FROM tk_task WHERE chat_id=? AND state=? ORDER BY seq LIMIT 1",
            (chat_id, WAIT_HUMAN)).fetchone()
        if head is None:
            # maybe only a failed-delivery RUNNING row exists; treat as head too
            head = conn.execute(
                "SELECT * FROM tk_task WHERE chat_id=? AND state=? ORDER BY seq LIMIT 1",
                (chat_id, RUNNING)).fetchone()
            if head is None:
                return None
            return _row_to_dict(head)
        conn.execute("UPDATE tk_task SET state='done', note=COALESCE(note,''),"
                     " updated_at=? WHERE task_id=?", (time.time(), head["task_id"]))
        nxt = conn.execute(
            "SELECT * FROM tk_task WHERE chat_id=? AND state=? ORDER BY seq LIMIT 1",
            (chat_id, READY)).fetchone()
        if nxt is None:
            return None                            # queue exhausted
        return _deliver_and_park(conn, nxt)


def skip(chat_id: str, db_path: str | None = None) -> dict | None:
    """Temporarily postpone the wait_human head: back to ``ready`` and requeued
    at the tail; the next ready task starts immediately. A lone skipped task
    therefore comes right back around. Never marks anything done.

    Returns ``{"skipped": <task>, "delivered": <task|None>}``, None when the
    chat has no wait_human head to skip.
    """
    with _connect(db_path) as conn:
        init_schema(conn)
        head = conn.execute(
            "SELECT * FROM tk_task WHERE chat_id=? AND state=? ORDER BY seq LIMIT 1",
            (chat_id, WAIT_HUMAN)).fetchone()
        if head is None:
            return None
        skipped = _row_to_dict(head)
        assert skipped is not None
        tail = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM tk_task"
                            " WHERE chat_id=?", (chat_id,)).fetchone()[0]
        conn.execute("UPDATE tk_task SET state=?, seq=?, updated_at=? WHERE task_id=?",
                     (READY, tail + 1, time.time(), head["task_id"]))
        conn.commit()
        nxt = conn.execute(
            "SELECT * FROM tk_task WHERE chat_id=? AND state=? ORDER BY seq LIMIT 1",
            (chat_id, READY)).fetchone()
        delivered = None
        if nxt is not None:
            try:
                delivered = _deliver_and_park(conn, nxt)
            except ValueError:
                # stays RUNNING; on_owner_message treats it as head for retry
                pass
    return {"skipped": skipped, "delivered": delivered}


def emit(event_id: str, db_path: str | None = None) -> int:
    """Fire a named event: resume every wait_event subscriber (cyclic reuse)."""
    resumed = 0
    with _connect(db_path) as conn:
        init_schema(conn)
        rows = conn.execute(
            "SELECT * FROM tk_task WHERE state=? AND wait_event=? ORDER BY seq",
            (WAIT_HUMAN, event_id)).fetchall()
        for row in rows:
            conn.execute("UPDATE tk_task SET state='ready', wait_event=NULL,"
                         " updated_at=? WHERE task_id=?", (time.time(), row["task_id"]))
            conn.commit()
            fresh = conn.execute("SELECT * FROM tk_task WHERE task_id=?",
                                 (row["task_id"],)).fetchone()
            try:
                _deliver_and_park(conn, fresh)
                resumed += 1
            except ValueError:
                conn.execute("UPDATE tk_task SET state='running', updated_at=?"
                             " WHERE task_id=?", (time.time(), row["task_id"]))
                conn.commit()
    return resumed


def cancel(task_id: str, db_path: str | None = None) -> bool:
    with _connect(db_path) as conn:
        init_schema(conn)
        cur = conn.execute("UPDATE tk_task SET state=?, updated_at=? WHERE task_id=?"
                           " AND state IN (?,?,?)",
                           (CANCELLED, time.time(), task_id, * _OPEN_STATES))
        conn.commit()
        return cur.rowcount > 0


def status(chat_id: str, db_path: str | None = None) -> dict:
    with _connect(db_path) as conn:
        init_schema(conn)
        rows = conn.execute(
            "SELECT task_id, seq, title, state, note FROM tk_task"
            " WHERE chat_id=? ORDER BY seq", (chat_id,)).fetchall()
    out = [dict(r) for r in rows]
    remaining = sum(1 for t in out if t["state"] in _OPEN_STATES)
    return {"chat_id": chat_id, "remaining": remaining, "tasks": out}


def waiting_head_for_bridge(chat_id: str, db_path: str | None = None) -> bool:
    """Cheap check used by the platform bridge: does ANY open task exist here?"""
    with _connect(db_path) as conn:
        init_schema(conn)
        n = conn.execute("SELECT COUNT(*) FROM tk_task WHERE chat_id=? AND state IN (?,?,?)",
                         (chat_id, *_OPEN_STATES)).fetchone()[0]
        return n > 0
