"""controller.topic_memory — scope-isolated topic memory (stage003, steps 1–2).

Implements the ToDo stage003 contract: уставки и корректировки не утекают из
темы в глобальный контекст. «Отсутствие указанного scope не является
разрешением на глобализацию»: the default write target of a scoped writer is
its own task scope, never global; global is read-only for them.

Scope resolution order (read rule — filter BEFORE any search):
    task items → bound project items → global items (last, policy only).
An item applies where it was written; semantic similarity between topics
grants NO read access — crossing topics goes through :func:`import_item`,
which records provenance and never edits the source.

Supersede rule: a new item replaces the older ACTIVE one on equal
(scope_id, subject) only. agent-proposal/hypothesis sources start
unconfirmed and act as rules only after :func:`confirm` (Вл 01.11:
механизм различает источники, reasoning не ограничивает).
"""
from __future__ import annotations

import time

from .model import new_id

#: Scope kinds by applicability, farthest first.
KINDS = ("task", "project", "global")
_ITEM_KINDS = ("fact", "constraint", "decision", "preference")
_SOURCES = ("user-directive", "tool-result", "agent-proposal", "hypothesis")


# ---- scopes -----------------------------------------------------------------

def ensure_scope(conn, kind: str, ref: str = "") -> str:
    """Idempotent scope lookup/creation. ``global`` has exactly one (ref='')."""
    if kind not in KINDS:
        raise ValueError(f"unknown scope kind: {kind}")
    row = conn.execute(
        "SELECT scope_id FROM ctl_mem_scope WHERE kind=? AND ref=?", (kind, ref)).fetchone()
    if row:
        return row["scope_id"]
    sid = new_id("memscope")
    conn.execute("INSERT INTO ctl_mem_scope (scope_id,kind,ref,created_at) VALUES (?,?,?,?)",
                 (sid, kind, ref, time.time()))
    conn.commit()
    return sid


def get_scope(conn, kind: str, ref: str = "") -> str | None:
    row = conn.execute(
        "SELECT scope_id FROM ctl_mem_scope WHERE kind=? AND ref=?", (kind, ref)).fetchone()
    return row["scope_id"] if row else None


def global_scope(conn) -> str:
    return ensure_scope(conn, "global", "")


def bind_parent(conn, scope_id: str, parent_scope_id: str) -> None:
    """Declare what a scope inherits from (task → project). Rebinding is allowed;
    cycles are refused — the chain walk would never terminate."""
    seen = {scope_id}
    cur, n = parent_scope_id, 0
    while cur and n < 8:  # depth cap: chain is task→project→global in practice
        if cur in seen:
            raise ValueError("memory binding cycle")
        seen.add(cur)
        row = conn.execute(
            "SELECT parent_scope_id AS p FROM ctl_mem_binding WHERE scope_id=?",
            (cur,)).fetchone()
        cur = row["p"] if row else None
        n += 1
    conn.execute(
        "INSERT INTO ctl_mem_binding (scope_id,parent_scope_id,created_at) VALUES (?,?,?)"
        " ON CONFLICT(scope_id) DO UPDATE SET parent_scope_id=excluded.parent_scope_id",
        (scope_id, parent_scope_id, time.time()))
    conn.commit()


def resolve_chain(conn, task_scope_id: str) -> list[dict]:
    """Applicability chain for a task scope, nearest first, global LAST.
    A missing binding or a dangling parent shortens the chain honestly."""
    chain, cur, n = [], task_scope_id, 0
    while cur and n < 8:
        row = conn.execute(
            "SELECT s.* , b.parent_scope_id AS parent FROM ctl_mem_scope s"
            " LEFT JOIN ctl_mem_binding b ON b.scope_id=s.scope_id WHERE s.scope_id=?",
            (cur,)).fetchone()
        if row is None:
            break
        chain.append(dict(row))
        cur = row["parent"]
        n += 1
    g = get_scope(conn, "global", "")
    if g is None:
        g = ensure_scope(conn, "global", "")
    if not any(s["scope_id"] == g for s in chain):
        chain.append({"scope_id": g, "kind": "global", "ref": "",
                      "created_at": None, "parent": None})
    return chain


def effective_items(conn, task_scope_id: str, *, include_unconfirmed=False) -> list[dict]:
    """Items applicable to a task scope: nearest-scope-first, scope-filtered in SQL.
    Unconfirmed proposals are invisible unless asked for explicitly."""
    out = []
    for sc in resolve_chain(conn, task_scope_id):
        sql = ("SELECT i.*, ? AS from_kind, ? AS from_ref FROM ctl_mem_item i"
               " WHERE i.scope_id=? AND i.status='active'")
        args = [sc["kind"], sc["ref"], sc["scope_id"]]
        if not include_unconfirmed:
            sql += " AND i.confirmed=1"
        rows = conn.execute(sql + " ORDER BY i.created_at DESC", args).fetchall()
        out.extend(dict(r) for r in rows)
    return out


# ---- writes -------------------------------------------------------------------

def add_item(conn, scope_id: str, kind: str, subject: str, body: str,
             source: str, run_id: str | None = None,
             confirmed: bool | None = None) -> str:
    """Write one memory item. A CONFIRMED write supersedes the active item with
    the same subject IN THIS SCOPE (and nothing else). An UNCONFIRMED one
    (agent-proposal/hypothesis) coexists invisibly until :func:`confirm` — a
    proposal never retires a standing rule just by being written.
    Default confirmation: user-directive/tool-result are rules at once."""
    if kind not in _ITEM_KINDS:
        raise ValueError(f"unknown item kind: {kind}")
    if source not in _SOURCES:
        raise ValueError(f"unknown source: {source}")
    if not subject.strip() or not body.strip():
        raise ValueError("subject and body are required")
    if confirmed is None:
        confirmed = source in ("user-directive", "tool-result")
    confirmed_flag = 1 if confirmed else 0
    now = time.time()
    item_id = new_id("memitem")
    old = None
    # One transaction: supersede + insert (a failed insert must not retire the old value).
    if confirmed_flag:
        old = conn.execute(
            "SELECT item_id FROM ctl_mem_item WHERE scope_id=? AND subject=?"
            " AND status='active'", (scope_id, subject)).fetchone()
        if old:
            conn.execute("UPDATE ctl_mem_item SET status='superseded', superseded_by=?"
                         " WHERE item_id=?", (item_id, old["item_id"]))
    conn.execute(
        "INSERT INTO ctl_mem_item (item_id,scope_id,kind,subject,body,source,status,"
        "confirmed,created_at,created_run_id) VALUES (?,?,?,?,?,?,'active',?,?,?)",
        (item_id, scope_id, kind, subject, body, source, confirmed_flag, now, run_id))
    if confirmed_flag and old:
        # V-O9: supersede chain mirrors to a causal edge (both nodes now exist).
        from .causal import link as _causal_link
        _causal_link(conn, frm=f"mem:{old['item_id']}", to=f"mem:{item_id}",
                     kind="supersedes", commit=False, strict=False)
    conn.commit()
    return item_id


def confirm(conn, item_id: str) -> bool:
    """A confirmation makes a proposal a standing RULE: atomically retire other
    active same-subject items in this scope (the deferred half of the supersede
    rule) and mark it confirmed."""
    cur = conn.execute(
        "UPDATE ctl_mem_item SET status='superseded', superseded_by=?"
        " WHERE scope_id=(SELECT scope_id FROM ctl_mem_item"
        "   WHERE item_id=?) AND subject=(SELECT subject FROM ctl_mem_item"
        "   WHERE item_id=?) AND status='active' AND item_id<>?",
        (item_id, item_id, item_id, item_id))
    cur = conn.execute("UPDATE ctl_mem_item SET confirmed=1"
                       " WHERE item_id=? AND confirmed=0", (item_id,))
    # V-O9: every item this confirmation just retired gets a supersedes edge.
    from .causal import link as _causal_link
    for r in conn.execute(
            "SELECT item_id FROM ctl_mem_item WHERE superseded_by=? AND status='superseded'",
            (item_id,)).fetchall():
        _causal_link(conn, frm=f"mem:{r[0]}", to=f"mem:{item_id}",
                     kind="supersedes", commit=False, strict=False)
    conn.commit()
    return bool(cur.rowcount)


def supersede_subject(conn, scope_id: str, subject: str) -> bool:
    """Retire every ACTIVE item with this subject in this scope without a
    replacement (the tool-level ``remove``). Returns True when something retired."""
    cur = conn.execute(
        "UPDATE ctl_mem_item SET status='superseded'"
        " WHERE scope_id=? AND subject=? AND status='active'", (scope_id, subject))
    conn.commit()
    return bool(cur.rowcount)


def import_item(conn, src_item_id: str, dst_scope_id: str,
                run_id: str | None = None, as_subject: str | None = None) -> str:
    """Copy an item into another scope with provenance (ctl_mem_import row).
    The copy starts UNCONFIRMED in the destination even if the source was a
    user-directive: adoption in a new topic is a fresh decision there."""
    src = conn.execute("SELECT * FROM ctl_mem_item WHERE item_id=?",
                       (src_item_id,)).fetchone()
    if src is None:
        raise LookupError(src_item_id)
    dst_scope = conn.execute("SELECT s.kind AS k FROM ctl_mem_scope s"
                             " WHERE s.scope_id=?", (dst_scope_id,)).fetchone()
    if dst_scope is None:
        raise LookupError(dst_scope_id)
    if dst_scope["k"] == "global":
        # Global is the read-only policy end of the hierarchy for scoped writers.
        raise ValueError("importing into the global scope is not allowed; write globally directly")
    subject = as_subject or src["subject"]
    new_id_ = add_item(conn, dst_scope_id, src["kind"], subject,
                       src["body"], src["source"], run_id=run_id, confirmed=False)
    conn.execute("INSERT INTO ctl_mem_import"
                 " (import_id,dst_item_id,src_item_id,imported_at,by_run_id)"
                 " VALUES (?,?,?,?,?)",
                 (new_id("memimport"), new_id_, src_item_id, time.time(), run_id))
    conn.commit()
    return new_id_
