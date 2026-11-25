"""Success verdicts as first-class, revocable records (owner diagnosis 23.11).

The failure this closes: an agent grades its own success in prose, and a grade once
uttered cannot be corrected — it lives only as an assistant message, so every later
turn must stay consistent with it, and "fixing" the grade costs the agent more than
a new unauthorized action does. There is no object to point at and overturn.

Here a verdict is a row: who issued it (agent|external), what it claims, what it is
grounded in (basis: message ids / tool_call_uids — facts from the tool ledger, never
narration), and a state with legal transitions only:

    proposed(agent) -> confirmed      only via confirm(), never by the agent's own voice
    proposed|confirmed -> overturned  legal from ANY live state, by anyone, with a reason

overturned rows are never deleted: the record of "I claimed success and was wrong" is
the mechanism that makes admitting it cheap. The turn-preparation seam re-injects the
live rows (and recently overturned ones) as a data-shaped sidecar note, so the model
narrates against the register, not against its own memory.

Honest limit: on a single-user host the agent CAN reach confirm() via terminal; what
is machine-enforced is that every grade has an issuer, a state, and an addressable
overturn — not a cryptographic wall between agent and owner.

CLI for owner/critic/third-party verifiers (same sqlite file, no gateway round-trip):

    python -m gateway.verdict_ledger propose <session> <subject> <verdict> [--basis JSON]
    python -m gateway.verdict_ledger confirm <id> --by NAME [--proof TEXT]
    python -m gateway.verdict_ledger overturn <id> --by NAME --reason TEXT
    python -m gateway.verdict_ledger list [session]
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time

logger = logging.getLogger(__name__)

ISSUERS = ("agent", "external")
STATES = ("proposed", "confirmed", "overturned")
# How long an overturned verdict keeps showing in the injection note (seconds).
OVERTURNED_TAIL_S = 86400.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS verdicts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  subject TEXT NOT NULL,
  issuer TEXT NOT NULL CHECK(issuer IN ('agent','external')),
  state TEXT NOT NULL CHECK(state IN ('proposed','confirmed','overturned')),
  verdict TEXT NOT NULL,
  basis TEXT,
  by_who TEXT,
  reason TEXT,
  supersedes INTEGER,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS verdicts_session_state ON verdicts(session_id, state);
"""


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)


# ── direct-connection core (single source of transition truth) ─────────────

def propose_c(conn, session_id: str, subject: str, verdict: str,
              basis: str | None = None, supersedes: int | None = None) -> int:
    """An agent-side success claim. Issuer is hardcoded 'agent': this entry point
    can propose, never confirm — that is the whole point."""
    ensure_table(conn)
    now = time.time()
    cur = conn.execute(
        "INSERT INTO verdicts(session_id,subject,issuer,state,verdict,basis,supersedes,"
        "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (session_id, subject, "agent", "proposed", verdict, basis, supersedes, now, now))
    conn.commit()
    return int(cur.lastrowid)


def confirm_c(conn, verdict_id: int, by: str, proof: str | None = None) -> dict | None:
    """proposed -> confirmed. `by` is the external verifier's name (a third-party
    reproduction, per the 12.11 critic rule, is what belongs there)."""
    ensure_table(conn)
    row = _get(conn, verdict_id)
    if row is None:
        raise ValueError(f"verdict {verdict_id} does not exist")
    if row["state"] != "proposed":
        raise ValueError(f"verdict {verdict_id} is {row['state']}; only 'proposed' can be confirmed")
    conn.execute("UPDATE verdicts SET state='confirmed', by_who=?, basis=COALESCE(?,basis), updated_at=? WHERE id=?",
                 (by, proof, time.time(), verdict_id))
    conn.commit()
    return _get(conn, verdict_id)


def overturn_c(conn, verdict_id: int, by: str, reason: str) -> dict | None:
    """Legal from ANY live state — including 'confirmed': a confirmation can itself
    turn out wrong. Without this transition, a once-issued grade is unfixable."""
    ensure_table(conn)
    row = _get(conn, verdict_id)
    if row is None:
        raise ValueError(f"verdict {verdict_id} does not exist")
    if row["state"] == "overturned":
        raise ValueError(f"verdict {verdict_id} is already overturned")
    if not reason.strip():
        raise ValueError("overturn requires a reason (it is the record, not a status edit)")
    conn.execute("UPDATE verdicts SET state='overturned', by_who=?, reason=?, updated_at=? WHERE id=?",
                 (by, reason.strip(), time.time(), verdict_id))
    conn.commit()
    return _get(conn, verdict_id)


def _get(conn, verdict_id: int) -> dict | None:
    ensure_table(conn)
    r = conn.execute("SELECT * FROM verdicts WHERE id=?", (verdict_id,)).fetchone()
    return dict(r) if r else None


def live_verdicts_c(conn, session_id: str) -> list[dict]:
    """Latest non-overturned row per subject — what the agent currently 'stands by'."""
    ensure_table(conn)
    # Only the LATEST row per subject can be live: an overturned claim stays dead
    # until a NEWER proposal replaces it — older un-overturned rows must not revive.
    rows = conn.execute(
        "SELECT v.* FROM verdicts v JOIN (SELECT subject, MAX(id) mid FROM verdicts "
        "WHERE session_id=? GROUP BY subject) t ON v.id=t.mid "
        "WHERE v.state!='overturned' ORDER BY v.id",
        (session_id,)).fetchall()
    return [dict(r) for r in rows]


def overturned_recently_c(conn, session_id: str, tail_s: float = OVERTURNED_TAIL_S) -> list[dict]:
    ensure_table(conn)
    rows = conn.execute(
        "SELECT v.* FROM verdicts v JOIN (SELECT subject, MAX(id) mid FROM verdicts "
        "WHERE session_id=? AND state='overturned' AND updated_at>? GROUP BY subject) t ON v.id=t.mid ORDER BY v.id",
        (session_id, time.time() - tail_s)).fetchall()
    return [dict(r) for r in rows]


def render_note_c(conn, session_id: str) -> str | None:
    """Model-visible register for this turn: data-shaped, with provenance, no advice
    beyond the semantics of the states themselves. None when the register is empty —
    most sessions never use it and pay nothing."""
    live = live_verdicts_c(conn, session_id)
    dead = overturned_recently_c(conn, session_id)
    if not live and not dead:
        return None
    lines = ["<verdict-register>"]
    for v in live:
        if v["state"] == "proposed":
            lines.append(f"#{v['id']} subject={v['subject']!r}: SELF-ASSESSED, NOT CONFIRMED "
                         f"(issuer={v['issuer']}, claim={v['verdict']!r}) — do not narrate as success; "
                         "confirmation comes from an external verifier, not from repetition.")
        else:
            lines.append(f"#{v['id']} subject={v['subject']!r}: CONFIRMED by {v['by_who']!r} "
                         f"(claim={v['verdict']!r})")
    for v in dead:
        lines.append(f"#{v['id']} subject={v['subject']!r}: OVERTURNED (by {v['by_who']!r}: {v['reason']}) "
                     "— do not restate or silently re-propose it without new evidence.")
    lines.append("</verdict-register>")
    return "\n".join(lines)


# ── store-wrapper conveniences (gateway call sites hand us `db` objects) ───

def _read(store, fn, *a, **kw):
    with store._read_ctx() as conn:
        return fn(conn, *a, **kw)


def _write(store, fn, *a, **kw):
    conn = sqlite3.connect(store.db_path)
    try:
        out = fn(conn, *a, **kw)
        conn.commit()
        return out
    finally:
        conn.close()


def propose(store, session_id: str, subject: str, verdict: str, basis=None) -> int:
    try:
        return _write(store, propose_c, session_id, subject, verdict,
                      json.dumps(basis) if basis is not None else None)
    except Exception:
        logger.debug("verdict_ledger: propose failed", exc_info=True)
        return 0


def confirm(store, verdict_id: int, by: str, proof: str | None = None) -> dict | None:
    return _write(store, confirm_c, verdict_id, by, proof)


def overturn(store, verdict_id: int, by: str, reason: str) -> dict | None:
    return _write(store, overturn_c, verdict_id, by, reason)


def render_note(store, session_id: str) -> str | None:
    try:
        return _read(store, render_note_c, session_id)
    except Exception:
        logger.debug("verdict_ledger: render failed", exc_info=True)
        return None


# ── CLI (owner/critic/third party; agent CAN reach it — issuer stays recorded) ──

def _cli(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="verdict_ledger")
    ap.add_argument("action", choices=["propose", "confirm", "overturn", "list"])
    ap.add_argument("args", nargs="*", default=[])
    ap.add_argument("--by", default="")
    ap.add_argument("--reason", default="")
    ap.add_argument("--basis", default=None)
    ap.add_argument("--proof", default=None)
    ap.add_argument("--db", default=os.path.join(os.environ.get("HERMES_HOME", "~/.hermes"), "state.db"))
    ns = ap.parse_args(argv)
    conn = sqlite3.connect(os.path.expanduser(ns.db))
    conn.row_factory = sqlite3.Row
    if ns.action == "propose":
        session, subject, claim = ns.args[0], ns.args[1], ns.args[2]
        print(propose_c(conn, session, subject, claim, ns.basis))
    elif ns.action == "confirm":
        print(json.dumps(confirm_c(conn, int(ns.args[0]), ns.by or "unnamed", ns.proof), default=str))
    elif ns.action == "overturn":
        print(json.dumps(overturn_c(conn, int(ns.args[0]), ns.by or "unnamed", ns.reason), default=str))
    else:
        sess = ns.args[0] if ns.args else ""
        rows = (live_verdicts_c(conn, sess) + overturned_recently_c(conn, sess)) if sess else \
            [dict(r) for r in conn.execute("SELECT * FROM verdicts ORDER BY id DESC LIMIT 50")]
        for r in rows:
            print(json.dumps(r, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
