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
CREATE TABLE IF NOT EXISTS tk_hold (
    chat_id  TEXT PRIMARY KEY,
    held_at  REAL NOT NULL
);
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
    # v0.2 additive migration (idempotent): fan-out parentage + open-question
    # register. Constant defaults only — SQLite forbids non-constant ADD COLUMN.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tk_task)")}
    if "parent_id" not in cols:
        conn.execute("ALTER TABLE tk_task ADD COLUMN parent_id TEXT")
    if "open_q" not in cols:
        conn.execute("ALTER TABLE tk_task ADD COLUMN open_q TEXT NOT NULL DEFAULT '[]'")


def _meta(task: dict) -> dict:
    """Note column as a dict; pre-note plain strings yield {} (never raise)."""
    raw = task.get("note") or ""
    if not raw:
        return {}
    try:
        m = json.loads(raw)
    except ValueError:
        return {}
    return m if isinstance(m, dict) else {}


def _jaccard(a: str, b: str) -> float:
    import re
    ta = set(re.findall(r"[\w\-]+", a.lower()))
    tb = set(re.findall(r"[\w\-]+", b.lower()))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


#: proximity seam (mechanism 3): default lexical Jaccard; swap for an
#: embedding-based function when a vector layer is wired in (>=8-bit vectors).
similarity_fn = _jaccard


def _task_line(conn: sqlite3.Connection, task: dict) -> str:
    """Render the delivery line: plain task / branch proposal / join digest."""
    meta = _meta(task)
    if meta.get("join"):
        j = meta["join"]
        rows = conn.execute(
            "SELECT seq, title, body, state, note, open_q FROM tk_task"
            " WHERE parent_id=? ORDER BY seq", (task["task_id"],)).fetchall()
        lines = [f"[задача] СЛИЯНИЕ «{j.get('goal', '')}» — {len(rows)} ветки:"]
        for r in rows:
            m = _meta(dict(r))
            fin = f" | вывод: {m['final']}" if m.get("final") else ""
            try:
                oq = json.loads(r["open_q"] or "[]")
            except ValueError:
                oq = []
            oqs = f" | открытых вопросов: {len(oq)}" if oq else ""
            lines.append(f"  [{r['seq']}] {r['title']}: {r['body'][:200]}{fin}"
                         f"{oqs} (состояние: {r['state']})")
        if j.get("criteria"):
            lines.append(f"критерии выбора: {j['criteria']}")
        lines.append("ack = решение: названная ветка или третье решение.")
        base = "\n".join(lines)
    elif meta.get("branch_of"):
        base = (f"[задача] [ветка {meta.get('branch_no', '?')}"
                f"/{meta.get('branch_total', '?')}] "
                f"{task['title']}: {task['body']}").strip(": ")
    else:
        base = f"[задача] {task['title']}: {task['body']}".strip(": ")
    # wake-time validity gate (Вл 12.11): the machine, not the agent's mood,
    # decides whether the data behind the task is fit for its criteria.
    # INVALID is a rework instruction, never an excuse to ask the owner.
    rc = _wake_check_rc(meta)
    if rc is None:
        return base
    task["wake_check_ok"] = rc == 0
    if rc == 0:
        return f"[данные: OK]\n{base}"
    return (f"[данные: НЕВАЛИДНЫ (rc={rc})]\n{base}\n"
            "[гейт] отчёт не прошёл машинную проверку достаточности; критерии "
            "задачи над ним не исполняются и владельцу не докладываются. "
            "Следующий шаг: перегенерировать данные (другим способом, если "
            "тот же даёт ту же пустоту) и перепроверить.")


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
    target = _meta(task).get("deliver_target", "")
    if not target:
        return False
    line = task.get("_line") or f"[задача] {task['title']}: {task['body']}".strip(": ")
    try:
        p = subprocess.run(["hermes", "send", "-t", target, "-q", line],
                           capture_output=True, text=True, timeout=60, check=False)
        return p.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


deliver_fn = deliver_default  # overridable seam for tests / in-process adapters


def _wake_check_rc(meta: dict) -> int | None:
    """Run the bound validity command; None when no check is bound."""
    cmd = meta.get("wake_check")
    if not cmd:
        return None
    try:
        return subprocess.run(cmd, shell=True, capture_output=True,
                              timeout=120, check=False).returncode
    except (OSError, subprocess.TimeoutExpired):
        return -1


def _deliver_and_park(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    """running → deliver → wait_human. Delivery failure keeps RUNNING and is
    raised as ValueError: caller decides retry policy; never silently lost."""
    task = _row_to_dict(row)
    assert task is not None
    conn.execute("UPDATE tk_task SET state=?, updated_at=? WHERE task_id=?",
                 (RUNNING, time.time(), task["task_id"]))
    conn.commit()
    task["_line"] = _task_line(conn, task)
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

    Tasks parked on an event (wait_event set) are NOT owner-ackable — only an
    emit(event_id) wakes them. Returns the task that was just delivered (or the
    still-active head if the queue is already drained), None otherwise.
    """
    with _connect(db_path) as conn:
        init_schema(conn)
        head = conn.execute(
            "SELECT * FROM tk_task WHERE chat_id=? AND state=? AND wait_event IS NULL"
            " ORDER BY seq LIMIT 1", (chat_id, WAIT_HUMAN)).fetchone()
        if head is None:
            # maybe only a failed-delivery RUNNING row exists; treat as head too
            head = conn.execute(
                "SELECT * FROM tk_task WHERE chat_id=? AND state=? ORDER BY seq LIMIT 1",
                (chat_id, RUNNING)).fetchone()
            if head is None:
                return None
            return _row_to_dict(head)
        # completion gate (mechanism 2): while the head carries open questions,
        # an owner ack does NOT close it — the head is re-delivered with the
        # register instead. Postponing is /skip's job, closing is not.
        try:
            open_q = json.loads(head["open_q"] or "[]")
        except (ValueError, IndexError):
            open_q = []
        if open_q:
            task = _row_to_dict(head)
            assert task is not None
            task["_line"] = (f"[задача] {task['title']}: {len(open_q)} открытых "
                             f"вопросов — закрой или назначь работу (/skip чтобы "
                             f"отложить):\n  - " + "\n  - ".join(open_q))
            deliver_fn(task)
            return {"task_id": task["task_id"], "title": task["title"],
                    "refused_open_questions": len(open_q)}
        # validity gate (Вл 12.11): a bound wake_check that fails blocks the
        # close exactly like an open question — invalid data yields rework,
        # never a reportable verdict. One subprocess run: _task_line renders
        # the verdict into the line and sets wake_check_ok.
        task = _row_to_dict(head)
        assert task is not None
        task["_line"] = _task_line(conn, task)
        if task.get("wake_check_ok") is False:
            deliver_fn(task)
            return {"task_id": task["task_id"], "title": task["title"],
                    "refused_wake_check": True}
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
            "SELECT * FROM tk_task WHERE chat_id=? AND state=? AND wait_event IS NULL"
            " ORDER BY seq LIMIT 1", (chat_id, WAIT_HUMAN)).fetchone()
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


def hold(chat_id: str, db_path: str | None = None) -> bool:
    """Park this chat's queue: bridge acks (/next, reply) are ignored until
    resume(). Tasks keep their states. True when the pause was set now."""
    with _connect(db_path) as conn:
        init_schema(conn)
        cur = conn.execute("INSERT OR IGNORE INTO tk_hold (chat_id, held_at) VALUES (?,?)",
                           (chat_id, time.time()))
        conn.commit()
        return cur.rowcount > 0


def resume(chat_id: str, db_path: str | None = None) -> bool:
    """Lift the hold. The parked head (if any) waits for the next owner ack."""
    with _connect(db_path) as conn:
        init_schema(conn)
        cur = conn.execute("DELETE FROM tk_hold WHERE chat_id=?", (chat_id,))
        conn.commit()
        return cur.rowcount > 0


def is_held(chat_id: str, db_path: str | None = None) -> bool:
    with _connect(db_path) as conn:
        init_schema(conn)
        row = conn.execute("SELECT 1 FROM tk_hold WHERE chat_id=?", (chat_id,)).fetchone()
        return row is not None


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


def fanout(chat_id: str, goal: str, branches: list[dict], criteria: str = "",
           deliver_target: str = "", duplicate_threshold: float = 0.85,
           db_path: str | None = None) -> dict:
    """Mechanisms 1+3: spawn N competing branch tasks and a JOIN row that
    arrives LAST with the digest of every branch. Branches walk the ordinary
    ack-driven queue; the join ack is the owner's decision (a named branch or
    a third option the agent composes). Near-duplicate branches are refused up
    front (proximity check via :data:`similarity_fn`)."""
    if len(branches) < 2:
        raise ValueError("fanout needs >=2 competing branches")
    texts = []
    for i, b in enumerate(branches, start=1):
        title = (b.get("title") or "").strip()
        if not title:
            raise ValueError(f"branch {i} has no title")
        texts.append(f"{title} {b.get('body', '')}")
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            sim = similarity_fn(texts[i], texts[j])
            if sim >= duplicate_threshold:
                raise ValueError(
                    f"branch {i + 1} and {j + 1} are near-duplicates "
                    f"(similarity {sim:.2f}) — dedupe first")
    now = time.time()
    with _connect(db_path) as conn:
        init_schema(conn)
        base = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM tk_task "
                            "WHERE chat_id=?", (chat_id,)).fetchone()[0]
        join_id = uuid.uuid4().hex[:12]
        jmeta: dict = {"join": {"goal": goal, "criteria": criteria}}
        if deliver_target:
            jmeta["deliver_target"] = deliver_target
        conn.execute(
            "INSERT INTO tk_task (task_id, chat_id, seq, title, body, state,"
            " note, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (join_id, chat_id, base + len(branches) + 1, f"СЛИЯНИЕ: {goal}",
             criteria, READY, json.dumps(jmeta, ensure_ascii=False), now, now))
        ids = []
        for off, b in enumerate(branches, start=1):
            bid = uuid.uuid4().hex[:12]
            bm = {"branch_of": join_id, "branch_no": off,
                  "branch_total": len(branches)}
            if deliver_target:
                bm["deliver_target"] = deliver_target
            conn.execute(
                "INSERT INTO tk_task (task_id, chat_id, seq, title, body,"
                " state, note, parent_id, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (bid, chat_id, base + off, (b.get("title") or "").strip(),
                 b.get("body", ""), READY,
                 json.dumps(bm, ensure_ascii=False), join_id, now, now))
            ids.append(bid)
        conn.commit()
    return {"join_id": join_id, "branch_ids": ids}


def _merge_note(task_id: str, updates: dict, db_path: str | None) -> dict:
    with _connect(db_path) as conn:
        init_schema(conn)
        row = conn.execute("SELECT * FROM tk_task WHERE task_id=?",
                           (task_id,)).fetchone()
        if row is None:
            raise ValueError(f"no such task: {task_id}")
        task = _row_to_dict(row)
        assert task is not None
        if task["state"] in (DONE, CANCELLED):
            raise ValueError(f"task {task_id} is {task['state']}, not open")
        meta = _meta(task)
        meta.update(updates)
        conn.execute("UPDATE tk_task SET note=?, updated_at=? WHERE task_id=?",
                     (json.dumps(meta, ensure_ascii=False), time.time(), task_id))
        conn.commit()
        return meta


def annotate(task_id: str, text: str, db_path: str | None = None) -> bool:
    """Attach the branch's verdict/conclusion to its note (``final``); the join
    digest surfaces it, so the decision is made on stated grounds."""
    _merge_note(task_id, {"final": text}, db_path)
    return True


def add_question(task_id: str, q: str, db_path: str | None = None) -> int:
    """Register an open question / unverified assumption on a task. Returns
    the new open count; while it is >0 the task cannot be ack-closed."""
    _merge_note(task_id, {}, db_path)  # validates existence + state
    with _connect(db_path) as conn:
        row = conn.execute("SELECT open_q FROM tk_task WHERE task_id=?",
                           (task_id,)).fetchone()
        try:
            qs = json.loads(row["open_q"] or "[]")
        except ValueError:
            qs = []
        if not isinstance(qs, list):
            qs = []
        qs.append(q)
        conn.execute("UPDATE tk_task SET open_q=?, updated_at=? WHERE task_id=?",
                     (json.dumps(qs, ensure_ascii=False), time.time(), task_id))
        conn.commit()
        return len(qs)


def drop_question(task_id: str, q, db_path: str | None = None) -> int:
    """Close one registered question: by 1-based position (int) or by exact /
    substring match. Returns the remaining open count."""
    with _connect(db_path) as conn:
        init_schema(conn)
        row = conn.execute("SELECT open_q FROM tk_task WHERE task_id=?",
                           (task_id,)).fetchone()
        if row is None:
            raise ValueError(f"no such task: {task_id}")
        try:
            qs = json.loads(row["open_q"] or "[]")
        except ValueError:
            qs = []
        idx = None
        if isinstance(q, int):
            if 1 <= q <= len(qs):
                idx = q - 1
        else:
            needle = str(q)
            if needle in qs:
                idx = qs.index(needle)
            else:
                for k, item in enumerate(qs):
                    if needle in item:
                        idx = k
                        break
        if idx is None:
            raise ValueError(f"no open question matching {q!r} on {task_id}")
        qs.pop(idx)
        conn.execute("UPDATE tk_task SET open_q=?, updated_at=? WHERE task_id=?",
                     (json.dumps(qs, ensure_ascii=False), time.time(), task_id))
        conn.commit()
        return len(qs)


def set_wake_check(task_id: str, cmd: str, db_path: str | None = None) -> bool:
    """Bind a machine validity check (shell; rc==0 means the data behind the
    task is sufficient for its criteria) to a task. Runs at EVERY delivery,
    including event-resume: INVALID turns the wake-up into a rework order."""
    _merge_note(task_id, {"wake_check": cmd}, db_path)
    return True


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
            "SELECT task_id, seq, title, state, note, open_q FROM tk_task"
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
