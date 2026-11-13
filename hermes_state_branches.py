"""Fork/Join conversation branches: DB CRUD on the ``session_branches`` table.

Design (fork-join-design.md / fork-join-implementation.md in
~/hermes-scheduler-study/): a branch is a lazy overlay over its parent
transcript — the child session row references ``(parent_session_id,
base_message_uid)`` and only stores messages appended after forking.  All
methods here are additive; sessions without branches never touch this table.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid

BRANCH_STATUSES = ("active", "joined", "abandoned")

logger = logging.getLogger("hermes_state")


class SessionBranchMixin:
    """Branch CRUD + state queries. Mixed into SessionDB."""

    def create_branch_row(
        self, branch_id: str, parent_session_id: str, child_session_id: str,
        base_message_uid: str | None, created_by_turn: str | None = None,
        title: str | None = None,
    ) -> None:
        """Register a new active branch. Idempotent per branch_id.

        ``title`` is stored immutably in ``meta_json`` — the console name a
        fork speaks with (``⎇ <title>:``).  The session title is display-only
        and gets re-summarized by the auto-titler, so it cannot be the label.
        """
        meta = json.dumps({"title": title}) if title else None

        def _do(conn: sqlite3.Connection) -> None:
            cur = conn.execute(
                """INSERT OR IGNORE INTO session_branches (
                       branch_id, parent_session_id, child_session_id, base_message_uid,
                       created_at, created_by_turn, status, meta_json)
                   VALUES (?, ?, ?, ?, ?, ?, 'active', ?)""",
                (branch_id, parent_session_id, child_session_id,
                 base_message_uid, time.time(), created_by_turn, meta),
            )
            if cur.rowcount:
                # V-O9 shadow writer: the parent line causes this branch creation.
                # Bookkeeping only — a causal failure must never sink the fork.
                try:
                    from controller import causal
                    causal.link(conn, frm=f"sess:{parent_session_id}",
                                to=f"br:{branch_id}", kind="causes",
                                commit=False, strict=False)
                except Exception:
                    logger.debug("causal fork edge failed", exc_info=True)
        self._execute_write(_do, patience_s=self._TRANSCRIPT_WRITE_PATIENCE_S)

    def set_branch_title(self, branch_id: str, title: str) -> None:
        """Merge ``title`` into the branch's meta_json (idempotent)."""
        def _do(conn: sqlite3.Connection) -> None:
            row = conn.execute(
                "SELECT meta_json FROM session_branches WHERE branch_id = ?",
                (branch_id,)).fetchone()
            meta = {}
            if row and row[0]:
                try:
                    meta = json.loads(row[0])
                except Exception:
                    meta = {}
            meta["title"] = title
            conn.execute(
                "UPDATE session_branches SET meta_json = ? WHERE branch_id = ?",
                (json.dumps(meta), branch_id))
        self._execute_write(_do, patience_s=self._TRANSCRIPT_WRITE_PATIENCE_S)

    @staticmethod
    def new_branch_id() -> str:
        return "b" + uuid.uuid4().hex[:8]

    def _branch_row(self, conn: sqlite3.Connection, *, child_session_id: str | None = None,
                    branch_id: str | None = None) -> sqlite3.Row | None:
        if child_session_id is not None:
            return conn.execute(
                "SELECT * FROM session_branches WHERE child_session_id = ?",
                (child_session_id,),
            ).fetchone()
        return conn.execute(
            "SELECT * FROM session_branches WHERE branch_id = ?", (branch_id,)
        ).fetchone()

    def get_branch_for_child(self, child_session_id: str) -> dict | None:
        """The branch record whose overlay source is ``child_session_id``, or None."""
        try:
            row = self._read_one(
                "SELECT * FROM session_branches WHERE child_session_id = ?",
                (child_session_id,))
        except Exception:
            return None
        return dict(row) if row else None

    def get_branch(self, branch_id: str) -> dict | None:
        try:
            row = self._read_one(
                "SELECT * FROM session_branches WHERE branch_id = ?", (branch_id,))
        except Exception:
            return None
        return dict(row) if row else None

    def get_active_blocking_branch(self, child_session_id: str) -> dict | None:
        """Branch row for ``child_session_id`` iff it is an ACTIVE branch whose child session
        owns its own chat thread (``/forkb``). This row is the sticky lock: while it stays active,
        the thread's console must not be repointed to another stream except by closing the branch
        (``/return`` / ``/leave``) — Вл 31.10."""
        try:
            row = self._read_one(
                """SELECT b.* FROM session_branches b
                   JOIN sessions s ON s.id = b.child_session_id
                   WHERE b.child_session_id = ? AND b.status = 'active'
                     AND s.thread_id IS NOT NULL AND s.thread_id <> ''""",
                (child_session_id,))
        except Exception:
            return None
        return dict(row) if row else None

    def get_any_active_blocking_branch(self) -> dict | None:
        """The oldest ACTIVE blocking branch (child session owning its own chat
        thread), regardless of which session asks — the variant-A focus holder
        (Вл 13.11). While it exists, /forkb must not open another."""
        try:
            row = self._read_one(
                """SELECT b.* FROM session_branches b
                   JOIN sessions s ON s.id = b.child_session_id
                   WHERE b.status = 'active'
                     AND s.thread_id IS NOT NULL AND s.thread_id <> ''
                   ORDER BY b.created_at LIMIT 1""")
        except Exception:
            return None
        return dict(row) if row else None

    def _fork_tree_root(self, session_id: str) -> str | None:
        seen: set[str] = set()
        cur: str | None = session_id
        while cur and cur not in seen:
            seen.add(cur)
            try:
                row = self._read_one(
                    "SELECT parent_session_id FROM sessions WHERE id = ?", (cur,))
            except Exception:
                return cur or None
            if row is None:
                break
            nxt = row["parent_session_id"]
            if not nxt or nxt == cur:
                break
            cur = nxt
        return cur

    def same_fork_tree(self, session_a: str, session_b: str) -> bool:
        """True when both sessions hang off one fork lineage (same root by the
        ``parent_session_id`` chain). Sessions outside any fork share no root row
        only if their chains never meet; standalone un-forked chats resolve to
        themselves as roots and so count as separate trees."""
        if not session_a or not session_b:
            return False
        if session_a == session_b:
            return True
        ra, rb = self._fork_tree_root(session_a), self._fork_tree_root(session_b)
        return bool(ra and rb and ra == rb)

    def list_active_branches(self, parent_session_id: str) -> list[dict]:
        """Active branches of a session, oldest first."""
        try:
            rows = self._read_all(
                """SELECT * FROM session_branches
                   WHERE parent_session_id = ? AND status = 'active'
                   ORDER BY created_at""",
                (parent_session_id,))
        except Exception:
            return []
        return [dict(r) for r in rows]

    def has_active_branches(self, parent_session_id: str) -> bool:
        """Fast existence probe (index-served); used by busy-mode enforcement."""
        try:
            row = self._read_one(
                """SELECT 1 FROM session_branches
                   WHERE parent_session_id = ? AND status = 'active' LIMIT 1""",
                (parent_session_id,))
        except Exception:
            return False
        return row is not None

    def set_branch_status(
        self, branch_id: str, status: str, *, join_strategy: str | None = None,
    ) -> bool:
        """Move a branch into ``joined``/``abandoned``. Returns True when updated."""
        if status not in BRANCH_STATUSES:
            raise ValueError(f"invalid branch status {status!r}")
        try:
            rc = self._write_rowcount(
                """UPDATE session_branches
                   SET status = ?, join_strategy = COALESCE(?, join_strategy),
                       joined_at = CASE WHEN ? = 'joined' THEN ? ELSE joined_at END
                   WHERE branch_id = ? AND status = 'active'""",
                (status, join_strategy, status, time.time(), branch_id))
        except Exception:
            return False
        if rc > 0 and status == "joined":
            # V-O9 shadow writer: join = convergence event node with a merge
            # edge from the absorbed branch (idempotent per branch).
            try:
                row = self._read_one(
                    "SELECT parent_session_id FROM session_branches WHERE branch_id = ?",
                    (branch_id,))
                if row is not None:
                    from controller import causal as _causal

                    def _join(conn: sqlite3.Connection) -> None:
                        _causal.record_branch_join(
                            conn, branch_id=branch_id,
                            parent_session_id=row["parent_session_id"], commit=False)
                    self._execute_write(_join)
            except Exception:
                logger.debug("causal join edges failed for %s", branch_id, exc_info=True)
        return rc > 0

    def min_active_base_rowid(self, parent_session_id: str) -> int | None:
        """Compression watermark floor: minimum messages.id among active branches'
        base points. Returns None when no active branch pins anything."""
        try:
            row = self._read_one(
                """SELECT MIN(m.id) AS floor_id
                   FROM session_branches b
                   JOIN messages m ON m.message_uid = b.base_message_uid
                    AND m.session_id = b.parent_session_id
                   WHERE b.parent_session_id = ? AND b.status = 'active'""",
                (parent_session_id,))
        except Exception:
            return None
        if not row or row["floor_id"] is None:
            return None
        return int(row["floor_id"])

    def get_last_message_uid(self, session_id: str) -> str | None:
        """message_uid of the newest live row of ``session_id`` (fork base point)."""
        try:
            row = self._read_one(
                """SELECT message_uid FROM messages
                   WHERE session_id = ? AND active = 1 AND message_uid IS NOT NULL
                   ORDER BY id DESC LIMIT 1""",
                (session_id,))
        except Exception:
            return None
        return row["message_uid"] if row else None

    def get_session_key(self, session_id: str) -> str | None:
        try:
            row = self._read_one(
                "SELECT session_key FROM sessions WHERE id = ?", (session_id,))
        except Exception:
            return None
        return row["session_key"] if row else None

    def move_branch_rows_to_parent(self, branch: dict) -> bool:
        """Public join-append entry point (stage001 п.5).

        ``gateway.slash_commands_fork.join_branch`` awaits this name through the
        AsyncSessionDB facade; only the ``_…_sync`` implementation existed, so
        ``/join <name> --append`` died on AttributeError. Pure alias — same
        semantics, same guardrails as ``_move_branch_rows_to_parent_sync``.
        """
        return self._move_branch_rows_to_parent_sync(branch)

    def _move_branch_rows_to_parent_sync(self, branch: dict) -> bool:
        """Append strategy: reparent the branch's own rows onto the parent.

        Allowed only while the parent tail after the fork point is uncompressed
        (no summary markers / inactive rows between the base and now), so raw
        rows keep their alternation context. Rows keep timestamps; ownership of
        tool_call rows follows their parents.
        """
        parent_id = branch["parent_session_id"]
        child_id = branch["child_session_id"]
        base_uid = branch.get("base_message_uid")

        def _do(conn):
            cur = conn.execute("SELECT id FROM sessions WHERE id = ?", (parent_id,))
            if cur.fetchone() is None:
                return 0
            floor = None
            if base_uid:
                row = conn.execute(
                    "SELECT id FROM messages WHERE session_id = ? AND message_uid = ?",
                    (parent_id, base_uid)).fetchone()
                if row is None:
                    return 0
                floor = row[0]
                # parent tail must be clean: no compacted/summary rows after the base.
                # Value check, not IS NOT NULL: the writer stores 0/1 integers
                # (`1 if msg.get(...) else 0`), never NULL — a null-check flags every
                # ordinary row and makes append-join impossible once the parent kept
                # living after the fork (live натурка 02.11). Legacy rows may hold
                # NULL/summary-text, hence COALESCE + explicit flag truthiness.
                dirty = conn.execute(
                    """SELECT COUNT(*) FROM messages
                       WHERE session_id = ? AND id > ?
                         AND (COALESCE(_compressed_summary, 0) NOT IN (0, '')
                              OR active = 0 OR compacted = 1)""",
                    (parent_id, floor)).fetchone()
                if dirty and dirty[0]:
                    return 0
            # take the branch's own rows
            rows = conn.execute(
                """SELECT role, content, tool_call_id, tool_calls, tool_name,
                          effect_disposition, timestamp, token_count, finish_reason,
                          reasoning, reasoning_content, reasoning_details,
                          codex_reasoning_items, codex_message_items, platform_message_id,
                          observed, api_content, display_kind, display_metadata,
                          display_identity, display_order, message_uid, absorbed_message_uids,
                          tool_call_uids, tool_call_uid
                   FROM messages WHERE session_id = ? ORDER BY id""", (child_id,)).fetchall()
            if not rows:
                return 1  # nothing to move — trivially done
            conn.executemany(
                """INSERT INTO messages (session_id, role, content, tool_call_id, tool_calls,
                      tool_name, effect_disposition, timestamp, token_count, finish_reason,
                      reasoning, reasoning_content, reasoning_details, codex_reasoning_items,
                      codex_message_items, platform_message_id, observed, api_content,
                      display_kind, display_metadata, display_identity, display_order,
                      message_uid, absorbed_message_uids, tool_call_uids, tool_call_uid)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(parent_id,) + tuple(None if i == 22 else v for i, v in enumerate(r))
                 for r in rows])
            # detach moved rows from the child so the overlay never replays them twice
            conn.execute("DELETE FROM messages WHERE session_id = ?", (child_id,))
            return len(rows)

        rc = self._execute_write(_do, patience_s=self._TRANSCRIPT_WRITE_PATIENCE_S)
        return bool(rc)
