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
    with _conn(db_path) as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute(
            """CREATE TABLE IF NOT EXISTS ledger(
                 seq INTEGER PRIMARY KEY AUTOINCREMENT,
                 ts TEXT NOT NULL,
                 actor_position_id TEXT NOT NULL,
                 mandate_version INTEGER NOT NULL,
                 policy_version TEXT NOT NULL,
                 kind TEXT NOT NULL,             -- observation|hypothesis|verified (B11; grows, O6)
                 type TEXT NOT NULL,             -- survey_request|branch|commitment|escalation|client_reply
                 issue_id TEXT NOT NULL,
                 payload_json TEXT NOT NULL,
                 idem_key TEXT UNIQUE,
                 result_json TEXT)""")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_issue ON ledger(issue_id, seq)")
        c.execute(
            """CREATE TABLE IF NOT EXISTS issues(
                 issue_id TEXT PRIMARY KEY,
                 title TEXT NOT NULL,
                 created_ts TEXT NOT NULL)""")
        for iid in sorted(ISSUES):
            c.execute("INSERT OR IGNORE INTO issues(issue_id,title,created_ts) VALUES(?,?,?)",
                      (iid, "seeded issue", _now()))


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
    # first-boot bootstrap: create schema lazily if missing (idempotent)
    try:
        conn.execute("SELECT 1 FROM ledger LIMIT 1").fetchone()
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


def new_idempotency_key() -> str:
    return uuid.uuid4().hex
