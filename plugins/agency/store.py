"""agency-state.db ledger — the single write point of the agency toolset (TZ v0.2 §6).

Prototype implementation of variant Г (synchronous core inside async comms):
short serialized transactions, thread-local connections, WAL. Fixtures stand in
for the position registry and issue creation in v0.2 (TZ: 'тупой механизм').

Invariants (TZ §6): no partially applied operations; committed events survive
restart; a repeat does not create a second event; seq is unique & monotonic
(legal gaps after rollback are fine); every projection replays from the ledger.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime

POLICY_VERSION = "agency-policy/0.2"

# --- fixtures: positions / mandates / issues (v0.2 stands-in for the registry) --
POSITIONS: dict[str, dict] = {
    "admin-1":    {"role": "administrator", "competencies": [], "mandate_version": 1},
    "expert-a":   {"role": "expert", "competencies": ["contracts"], "mandate_version": 1},
    "expert-b":   {"role": "expert", "competencies": ["supplier-audit"], "mandate_version": 1},
}
REVOKED: set[tuple[str, int]] = set()
ISSUES: set[str] = {"i-001"}          # seeded issue for tests/demo


def seed(db_path: str) -> None:
    """Create schema + fixture rows if needed. Idempotent."""
    conn = connect(db_path)
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS ledger(
                 seq INTEGER PRIMARY KEY AUTOINCREMENT,
                 ts TEXT NOT NULL,
                 actor_position_id TEXT NOT NULL,
                 mandate_version INTEGER NOT NULL,
                 policy_version TEXT NOT NULL,
                 kind TEXT NOT NULL,             -- observation|hypothesis|verified (B11; grows, O6)
                 type TEXT NOT NULL,             -- survey_request|branch|commitment|escalation|client_reply|...
                 issue_id TEXT NOT NULL,
                 payload_json TEXT NOT NULL,
                 idem_key TEXT UNIQUE,
                 result_json TEXT)""")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_issue ON ledger(issue_id, seq)")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS issues(
                 issue_id TEXT PRIMARY KEY,
                 title TEXT NOT NULL,
                 created_ts TEXT NOT NULL)""")
        # Coworking registry (Вл 07.11): positions self-register into the ledger;
        # admins approve. Source of truth = ledger replay, POSITIONS below is
        # only the v0.2 stand-in for role/competency checks until migration.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS positions(
                 position_id TEXT PRIMARY KEY,
                 role TEXT NOT NULL,            -- administrator|expert
                 competencies_json TEXT NOT NULL DEFAULT '[]',
                 mandate_version INTEGER NOT NULL DEFAULT 1,
                 status TEXT NOT NULL DEFAULT 'pending',   -- pending|active|revoked
                 registered_by TEXT NOT NULL,   -- session self-registration marker
                 approved_by TEXT,
                 created_ts TEXT NOT NULL)""")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS sessions(
                 session_id TEXT PRIMARY KEY,
                 position_id TEXT NOT NULL,
                 bound_ts TEXT NOT NULL)""")
        # materialized projections (loop-maintained cache over replay, TZ §6);
        # source of truth stays the ledger — a row is only an accelerated view
        conn.execute(
            """CREATE TABLE IF NOT EXISTS projections(
                 issue_id TEXT PRIMARY KEY,
                 as_of_seq INTEGER NOT NULL,
                 proj_json TEXT NOT NULL)""")
        for iid in sorted(ISSUES):
            conn.execute("INSERT OR IGNORE INTO issues(issue_id,title,created_ts) VALUES(?,?,?)",
                         (iid, "seeded issue", _now()))
        # reserved core actor: never a registrable position
        conn.execute(
            "INSERT OR IGNORE INTO positions(position_id,role,status,registered_by,created_ts)"
            " VALUES('agency-loop','core','active','system',?)", (_now(),))


def revoke_mandate(position_id: str, version: int) -> None:
    REVOKED.add((position_id, version))


def mandate_active(position_id: str, version: int) -> bool:
    return (position_id, version) not in REVOKED


def issue_exists(db_path: str, issue_id: str) -> bool:
    with _conn(db_path) as c:
        return c.execute("SELECT 1 FROM issues WHERE issue_id=?", (issue_id,)).fetchone() is not None


def ensure_issue(db_path: str, issue_id: str, title: str) -> None:
    with _conn(db_path) as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute("INSERT OR IGNORE INTO issues(issue_id,title,created_ts) VALUES(?,?,?)",
                  (issue_id, title, _now()))
        ISSUES.add(issue_id)


# ------------------------------------------------------------------ core ------
_local = threading.local()
_busy_timeout_ms = 5000


def set_busy_timeout(ms: int) -> None:
    global _busy_timeout_ms
    _busy_timeout_ms = ms


def _dbfile():
    from hermes_constants import get_hermes_home
    return str(get_hermes_home() / "agency-state.db")


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or _dbfile()
    conn = getattr(_local, "conn", None)
    key = ("db", path)
    if conn is None or getattr(_local, "key", None) != key:
        conn = sqlite3.connect(path, timeout=_busy_timeout_ms / 1000.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={_busy_timeout_ms}")
        _local.conn = conn
        _local.key = key
    return conn


def _conn(db_path):
    conn = connect(db_path)
    # first-boot / legacy-upgrade bootstrap: seed() is idempotent, so run it
    # whenever ANY core table is missing (ledger alone may predate newer tables)
    try:
        conn.execute("SELECT 1 FROM ledger LIMIT 1").fetchone()
        conn.execute("SELECT 1 FROM positions LIMIT 1")
        conn.execute("SELECT 1 FROM projections LIMIT 1")
    except sqlite3.OperationalError:
        seed(db_path)
    return conn


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


class LedgerError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable


def submit(db_path: str | None, *, type_: str, kind: str, issue_id: str,
           actor_position_id: str, mandate_version: int, payload: dict,
           idem_key: str | None = None, payload_fn=None) -> dict:
    """One short serialized transaction: validate → insert → return event view.

    Idempotency: same key + same operation args → stored result; same key +
    different args → IDEMPOTENCY_CONFLICT; nothing partially written either way.
    payload_fn(conn, self_key) may finalize payload fields INSIDE the locked
    transaction (e.g. derived counters), making them race-free; it must exclude
    the caller's own previous row (self_key) from any counting.
    """
    try:
        conn = connect(db_path)
        with conn:
            conn.execute("BEGIN IMMEDIATE")     # serialize writers at one point
            if payload_fn is not None:
                payload = payload_fn(conn, idem_key)
            op_canon = _canon(payload)
            if idem_key:
                row = conn.execute("SELECT payload_json,type,result_json FROM ledger"
                                   " WHERE idem_key=?", (idem_key,)).fetchone()
                if row is not None:
                    same_op = (row["type"] == type_
                               and row["payload_json"] == op_canon)
                    if not same_op:
                        raise LedgerError("IDEMPOTENCY_CONFLICT",
                                          "same key used with different arguments")
                    return json.loads(row["result_json"])
            seq_row = conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM ledger").fetchone()
            seq = seq_row[0]
            ts = _now()
            if not idem_key:
                idem_key = uuid.uuid4().hex
            result = {"ok": True, "event_seq": seq, "ts": ts, "idem_key": idem_key}
            conn.execute(
                "INSERT INTO ledger(ts,actor_position_id,mandate_version,policy_version,"
                "kind,type,issue_id,payload_json,idem_key,result_json)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (ts, actor_position_id, mandate_version, POLICY_VERSION, kind, type_,
                 issue_id, op_canon, idem_key, _canon(result)))
        return result
    except LedgerError:
        raise
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc) or "busy" in str(exc):
            raise LedgerError("LOCK_TIMEOUT", f"ledger busy: {exc}", retryable=True)
        raise LedgerError("VALIDATION_FAILED", f"ledger write failed: {exc}")


def replay_projection(db_path: str | None, issue_id: str) -> dict:
    """Rebuild the client-visible projection by replaying the ledger from zero."""
    with _conn(db_path) as c:
        rows = c.execute("SELECT * FROM ledger WHERE issue_id=? ORDER BY seq",
                         (issue_id,)).fetchall()
    proj = {"phases": [], "commitments": [], "branches": [], "waiting_on": []}
    for r in rows:
        p = json.loads(r["payload_json"])
        t = r["type"]
        if t == "branch":
            proj["branches"].append({"direction_id": p["direction_id"], "title": p["title"]})
        elif t == "survey_request":
            proj["phases"].append({"phase": "SURVEY", "to": p["expert_id"],
                                   "kind": r["kind"], "seq": r["seq"]})
            proj["waiting_on"].append({"expert_id": p["expert_id"], "what": "consent"})
        elif t == "commitment":
            proj["commitments"].append({"who": p["who"], "what": p["what"],
                                        "due": p["due"], "refs": p.get("refs", [])})
            waiting: list = proj["waiting_on"]
            proj["waiting_on"] = [w for w in waiting if w["expert_id"] != p["who"]]
        elif t == "escalation":
            proj["phases"].append({"phase": "ESCALATED", "reason": p["reason"], "seq": r["seq"]})
        elif t == "client_reply":
            proj["phases"].append({"phase": "CLIENT_NOTIFIED", "seq": r["seq"]})
    proj["as_of_seq"] = rows[-1]["seq"] if rows else 0
    return proj


# ------------------------------------------------- projection cache (loop) ----

def _refresh_projections(db_path: str | None, budget: int = 50) -> int:
    """Loop duty (TZ §6 'обслуживание проекций'): bring the materialized cache
    up to date for every issue whose ledger tail outgrew its snapshot.
    Deterministic order, per-issue single UPDATE inside one short transaction;
    budget caps work per tick so a huge backlog drains over several ticks."""
    conn = _conn(db_path)
    done = 0
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        stale = conn.execute(
            "SELECT i.issue_id,"
            " COALESCE((SELECT MAX(seq) FROM ledger l WHERE l.issue_id=i.issue_id),0) AS top,"
            " COALESCE(p.as_of_seq,-1) AS cached"
            " FROM issues i LEFT JOIN projections p ON p.issue_id=i.issue_id"
            " WHERE p.as_of_seq IS NULL OR p.as_of_seq < top"
            " ORDER BY i.issue_id LIMIT ?", (budget,)).fetchall()
        for r in stale:
            proj = replay_projection(db_path, r["issue_id"])   # honest rebuild
            conn.execute(
                "INSERT INTO projections(issue_id,as_of_seq,proj_json) VALUES(?,?,?)"
                " ON CONFLICT(issue_id) DO UPDATE SET as_of_seq=excluded.as_of_seq,"
                " proj_json=excluded.proj_json",
                (r["issue_id"], proj["as_of_seq"], _canon(proj)))
            done += 1
    return done


def projection_for_status(db_path: str | None, issue_id: str) -> dict:
    """Read path: return the projection at the current ledger tip.

    Reads prefer the loop-maintained cache when it is at tip; otherwise falls
    back to a full replay and (best-effort) leaves the cache fresher. ADOPTED
    INVARIANT: cached view MUST equal replay_projection at the same seq —
    acceptance tests assert parity.
    """
    c = _conn(db_path)
    top = c.execute("SELECT COALESCE(MAX(seq),0) FROM ledger WHERE issue_id=?",
                    (issue_id,)).fetchone()[0]
    row = c.execute("SELECT as_of_seq,proj_json FROM projections WHERE issue_id=?",
                    (issue_id,)).fetchone()
    if row is not None and row["as_of_seq"] == top:
        return json.loads(row["proj_json"])
    proj = replay_projection(db_path, issue_id)
    try:
        with c:
            c.execute("BEGIN IMMEDIATE")
            c.execute(
                "INSERT INTO projections(issue_id,as_of_seq,proj_json) VALUES(?,?,?)"
                " ON CONFLICT(issue_id) DO UPDATE SET as_of_seq=excluded.as_of_seq,"
                " proj_json=excluded.proj_json",
                (issue_id, proj["as_of_seq"], _canon(proj)))
    except sqlite3.OperationalError:
        pass                       # busy tip — next reader/loop refreshes instead
    return proj


def wal_checkpoint(db_path=None) -> None:
    """Storage maintenance duty of the loop (TZ §6 'optimization'). Best-effort."""
    try:
        connect(db_path).execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
    except sqlite3.OperationalError:
        pass  # busy readers/writers — checkpoint is advisory, retry next tick


def refresh_projections(db_path: str | None = None, budget: int = 50) -> int:
    """Public hook for the loop; see _refresh_projections."""
    return _refresh_projections(db_path, budget)


LOOP_ACTOR = ("agency-loop", 0)   # the core itself; reserved id, not a position


def sweep_overdue(db_path: str | None = None, now_iso: str | None = None) -> list[int]:
    """Loop duty between calls: mark commitments whose `due` has passed.

    One serialized transaction per tick. Each commitment is marked at most once
    ever: the marker's idem_key is deterministically derived from the original
    event's key/seq, so replays and competing sweeps cannot duplicate it.
    Open-ended obligations (no due) are never late. Marker type
    `commitment_overdue` is deliberately outside the v0.2 projection verbs —
    replay ignores unknown types while as_of_seq still advances.
    Returns the seqs of markers written by this sweep.
    """
    now_iso = now_iso or _now()
    conn = _conn(db_path)
    marked: list[int] = []
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT seq,idem_key,payload_json,issue_id FROM ledger"
            " WHERE type='commitment' ORDER BY seq").fetchall()
        for r in rows:
            payload = json.loads(r["payload_json"])
            due = payload.get("due") or ""
            if not due or due >= now_iso:
                continue                     # open-ended or still in time
            base = r["idem_key"] or f"seq{r['seq']}"
            mkey = f"overdue:{base}"
            dup = conn.execute("SELECT 1 FROM ledger WHERE idem_key=?",
                               (mkey,)).fetchone()
            if dup:
                continue
            seq = conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM ledger").fetchone()[0]
            result = {"ok": True, "event_seq": seq, "ts": now_iso, "idem_key": mkey}
            conn.execute(
                "INSERT INTO ledger(ts,actor_position_id,mandate_version,"
                "policy_version,kind,type,issue_id,payload_json,idem_key,result_json)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (now_iso, LOOP_ACTOR[0], LOOP_ACTOR[1], POLICY_VERSION,
                 "observation", "commitment_overdue", r["issue_id"],
                 _canon({"commitment_ref": base, "overdue_as_of": now_iso}),
                 mkey, _canon(result)))
            marked.append(seq)
    return marked


def new_idempotency_key() -> str:
    return uuid.uuid4().hex


# -------------------------------------------------- coworking registry --------
# Вл 07.11: агентство работает как коворкинг — эксперт сам регистрируется как
# эксперт, админ как админ; активация (и повышение до администратора) проходит
# через одобрение. Источник истины — леджер; таблица positions/sessions служит
# индексом текущего состояния и восстанавливается реплеем.
REGISTRY_ISSUE = "registry"          # sentinel issue track for registry events
_ROLES = ("administrator", "expert")


def _registry_event(conn, *, type_: str, kind: str, actor: str, mandate: int,
                    payload: dict, idem_key: str) -> int:
    seq_row = conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM ledger").fetchone()
    seq = seq_row[0]
    ts = _now()
    result = {"ok": True, "event_seq": seq, "ts": ts, "idem_key": idem_key}
    conn.execute(
        "INSERT INTO ledger(ts,actor_position_id,mandate_version,policy_version,"
        "kind,type,issue_id,payload_json,idem_key,result_json)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (ts, actor, mandate, POLICY_VERSION, kind, type_, REGISTRY_ISSUE,
         _canon(payload), idem_key, _canon(result)))
    return seq


def register_position(db_path, position_id: str, role: str,
                      competencies: list, registered_by: str) -> dict:
    """Self-service registration (Вл 07.11). Idempotent per position_id."""
    if role not in _ROLES:
        raise LedgerError("VALIDATION_FAILED", f"unknown role {role!r}")
    pid = position_id.strip().lower()
    if len(pid) < 3 or any(ch not in "-abcdefghijklmnopqrstuvwxyz0123456789" for ch in pid):
        raise LedgerError("VALIDATION_FAILED",
                          "position_id must be [a-z0-9-], length >= 3")
    conn = _conn(db_path)
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT status FROM positions WHERE position_id=?",
                           (pid,)).fetchone()
        if row is not None:
            return {"status": row["status"], "already_registered": True}
        conn.execute(
            "INSERT INTO positions(position_id,role,competencies_json,status,"
            "registered_by,created_ts) VALUES(?,?,?,'pending',?,?)",
            (pid, role, _canon(sorted(competencies or [])), registered_by, _now()))
        seq = _registry_event(
            conn, type_="registration", kind="observation", actor=pid, mandate=1,
            payload={"role": role, "competencies": sorted(competencies or []),
                     "registered_by": registered_by},
            idem_key=f"reg:{pid}")
    return {"status": "pending", "event_seq": seq, "already_registered": False}


def approve_position(db_path, position_id: str, approved_by: str,
                     new_role: str | None = None) -> dict:
    """Admin activation gate: pending → active. Returns the resulting status."""
    pid = position_id.strip().lower()
    conn = _conn(db_path)
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT role,status FROM positions WHERE position_id=?",
                           (pid,)).fetchone()
        if row is None:
            raise LedgerError("NOT_FOUND", f"position {pid!r} is not registered")
        if row["status"] == "active" and new_role in (None, row["role"]):
            return {"status": "active", "already_approved": True}
        role = new_role or row["role"]
        if new_role is not None and new_role not in _ROLES:
            raise LedgerError("VALIDATION_FAILED", f"unknown role {new_role!r}")
        conn.execute(
            "UPDATE positions SET status='active', approved_by=?, role=?"
            " WHERE position_id=?", (approved_by, role, pid))
        seq = _registry_event(
            conn, type_="approval", kind="verified", actor=approved_by,
            mandate=1, payload={"position_id": pid, "role": role},
            idem_key=f"appr:{pid}")
    return {"status": "active", "event_seq": seq, "role": role,
            "already_approved": False}


def bind_session(db_path, session_id: str, position_id: str) -> None:
    with _conn(db_path) as c:
        c.execute("INSERT OR REPLACE INTO sessions(session_id,position_id,bound_ts)"
                  " VALUES(?,?,?)", (session_id, position_id, _now()))


def position_for_session(db_path, session_id: str):
    row = connect(db_path).execute(
        "SELECT p.* FROM sessions s JOIN positions p ON p.position_id=s.position_id"
        " WHERE s.session_id=?", (session_id,)).fetchone()
    return row


def get_position(db_path, position_id: str):
    return connect(db_path).execute(
        "SELECT * FROM positions WHERE position_id=?",
        (position_id.lower(),)).fetchone()
