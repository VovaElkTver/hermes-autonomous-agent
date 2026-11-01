"""Investigation self-check mechanism: DB CRUD on the ``investigations`` tables.

Design: docs/design/investigation-mechanism.md. An investigation is the agent's
own hypothesis-checking state — question, hypotheses, probes with predictions
recorded BEFORE the probe runs, observations attached by the runtime from real
tool-calls / branch results, and a model-authored close. The core stores
conditions only (provenance, delivery dedup, restart recovery); it never
decides which hypothesis is right — the model's ``assess``/``close`` do.

State lives as rows in the session DB (same shape as ``session_branches``:
canonical DDL in SCHEMA_SQL here in the schema file's neighborhood, this mixin
owns only queries). All methods are additive; sessions without investigations
never touch these tables. Duplicate delivery of the same tool-call or the same
branch join is idempotent: ``dedup_key`` is UNIQUE, a re-delivery inserts
nothing and returns None.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid

INVESTIGATION_STATUSES = ("open", "closed")
PROBE_STATUSES = ("pending", "dispatched", "observed", "unknown_outcome")

# Content cap for a captured observation body; mirrors the fork join-note ceiling
# (gateway/slash_commands_fork.py JOIN_NOTE_MAX_CHARS) so tool spam cannot bloat
# the store; the runtime renders the tail marker.
OBSERVATION_MAX_CHARS = 8000

_CLOSE_DIAGNOSTIC_KEY = "close_diagnostic"


class InvestigationMixin:
    """Investigation CRUD + state queries. Mixed into SessionDB."""

    # ------------------------------------------------------------ ids + dedup keys

    @staticmethod
    def new_investigation_id() -> str:
        return "inv" + uuid.uuid4().hex[:10]

    @staticmethod
    def observation_dedup_key_call(session_id: str, call_id: str) -> str:
        """Delivery-dedup key for a tool-call capture: the same call_id is one
        observation, however many times the result is re-delivered."""
        return f"{session_id}|call:{call_id}"

    @staticmethod
    def observation_dedup_key_branch(session_id: str, branch_id: str) -> str:
        """Delivery-dedup key for a joined-branch capture: one join of one
        branch contributes one observation."""
        return f"{session_id}|branch:{branch_id}"

    @staticmethod
    def new_hypothesis_id() -> str:
        return "hyp" + uuid.uuid4().hex[:10]

    @staticmethod
    def new_probe_id() -> str:
        return "prb" + uuid.uuid4().hex[:10]

    # ------------------------------------------------------- investigations

    def open_investigation(
        self, investigation_id: str, session_id: str, question: str,
        meta: dict | None = None,
    ) -> tuple[str, bool]:
        """Register a new open investigation. Returns ``(investigation_id,
        created)``. Idempotent per ``investigation_id``, and — because a
        session carries at most one open investigation — re-opening when one is
        already active returns the ACTIVE row instead of a second one
        (delivery dedup: a replayed ``open`` must not fork the state).
        """
        existing = self.get_open_investigation(session_id)
        if existing is not None:
            return existing["investigation_id"], False
        self._execute_write(
            lambda conn: conn.execute(
                """INSERT OR IGNORE INTO investigations
                       (investigation_id, session_id, question, status, revision,
                        opened_at, meta_json)
                   VALUES (?, ?, ?, 'open', 1, ?, ?)""",
                (investigation_id, session_id, str(question), time.time(),
                 json.dumps(meta, ensure_ascii=False) if meta else None),
            ),
            patience_s=self._TRANSCRIPT_WRITE_PATIENCE_S,
        )
        return investigation_id, True

    def get_investigation(self, investigation_id: str) -> dict | None:
        row = self._read_one(
            "SELECT * FROM investigations WHERE investigation_id = ?",
            (investigation_id,))
        return dict(row) if row else None

    def get_open_investigation(self, session_id: str) -> dict | None:
        """The session's single open investigation (partial unique index), or None."""
        row = self._read_one(
            """SELECT * FROM investigations
               WHERE session_id = ? AND status = 'open'""", (session_id,))
        return dict(row) if row else None

    def _bump_revision(self, conn: sqlite3.Connection, investigation_id: str) -> None:
        conn.execute(
            "UPDATE investigations SET revision = revision + 1 WHERE investigation_id = ?",
            (investigation_id,))

    def set_investigation_meta(self, investigation_id: str, patch: dict) -> None:
        """Merge ``patch`` into meta_json (idempotent)."""
        def _do(conn: sqlite3.Connection) -> None:
            row = conn.execute(
                "SELECT meta_json FROM investigations WHERE investigation_id = ?",
                (investigation_id,)).fetchone()
            meta = {}
            if row and row[0]:
                try:
                    meta = json.loads(row[0])
                except (ValueError, TypeError):
                    meta = {}
            meta.update(patch)
            conn.execute(
                "UPDATE investigations SET meta_json = ? WHERE investigation_id = ?",
                (json.dumps(meta, ensure_ascii=False), investigation_id))
        self._execute_write(_do, patience_s=self._TRANSCRIPT_WRITE_PATIENCE_S)

    # ---------------------------------------------------------- hypotheses

    def add_hypothesis(self, investigation_id: str, statement: str,
                       hypothesis_id: str | None = None) -> str | None:
        """Add a hypothesis version; returns its id, or None when the
        investigation is not open (closed state is terminal for edits)."""
        hyp_id = hypothesis_id or self.new_hypothesis_id()
        def _do(conn: sqlite3.Connection) -> int:
            open_row = conn.execute(
                "SELECT 1 FROM investigations WHERE investigation_id = ? AND status = 'open'",
                (investigation_id,)).fetchone()
            if open_row is None:
                return 0
            conn.execute(
                """INSERT OR IGNORE INTO investigation_hypotheses
                       (hypothesis_id, investigation_id, statement, status, created_at)
                   VALUES (?, ?, ?, 'standing', ?)""",
                (hyp_id, investigation_id, str(statement), time.time()))
            self._bump_revision(conn, investigation_id)
            return 1
        if not self._execute_write(_do, patience_s=self._TRANSCRIPT_WRITE_PATIENCE_S):
            return None
        return hyp_id

    def list_hypotheses(self, investigation_id: str) -> list[dict]:
        return [dict(r) for r in self._read_all(
            """SELECT * FROM investigation_hypotheses
               WHERE investigation_id = ? ORDER BY created_at""", (investigation_id,))]

    def set_hypothesis_status(self, hypothesis_id: str, status: str) -> bool:
        """Record the model's verdict word for one hypothesis (the core stores
        it verbatim; it never derives one)."""
        rc = self._write_rowcount(
            "UPDATE investigation_hypotheses SET status = ? WHERE hypothesis_id = ?",
            (status, hypothesis_id))
        return rc > 0

    # --------------------------------------------------------------- probes

    def add_probe(self, investigation_id: str, description: str,
                  probe_id: str | None = None, branch_id: str | None = None) -> str | None:
        """Add a check plan (the tool-call or /fork the probe will run as)."""
        pid = probe_id or self.new_probe_id()
        def _do(conn: sqlite3.Connection) -> int:
            open_row = conn.execute(
                "SELECT 1 FROM investigations WHERE investigation_id = ? AND status = 'open'",
                (investigation_id,)).fetchone()
            if open_row is None:
                return 0
            conn.execute(
                """INSERT OR IGNORE INTO investigation_probes
                       (probe_id, investigation_id, description, status, created_at, branch_id)
                   VALUES (?, ?, ?, 'pending', ?, ?)""",
                (pid, investigation_id, str(description), time.time(), branch_id))
            self._bump_revision(conn, investigation_id)
            return 1
        if not self._execute_write(_do, patience_s=self._TRANSCRIPT_WRITE_PATIENCE_S):
            return None
        return pid

    def get_probe(self, probe_id: str) -> dict | None:
        row = self._read_one(
            "SELECT * FROM investigation_probes WHERE probe_id = ?", (probe_id,))
        return dict(row) if row else None

    def list_probes(self, investigation_id: str) -> list[dict]:
        return [dict(r) for r in self._read_all(
            """SELECT * FROM investigation_probes
               WHERE investigation_id = ? ORDER BY created_at""", (investigation_id,))]

    def set_probe_status(self, probe_id: str, status: str) -> bool:
        if status not in PROBE_STATUSES:
            raise ValueError(f"invalid probe status {status!r}")
        rc = self._write_rowcount(
            "UPDATE investigation_probes SET status = ? WHERE probe_id = ?",
            (status, probe_id))
        return rc > 0

    def bind_probe_branch(self, probe_id: str, branch_id: str) -> bool:
        """Attach a /fork branch id to a probe (structural provenance: the
        branch's meta_json carries the same pair, see the spec § Fork hook)."""
        rc = self._write_rowcount(
            "UPDATE investigation_probes SET branch_id = ? WHERE probe_id = ?",
            (branch_id, probe_id))
        return rc > 0

    def set_branch_meta(self, branch_id: str, patch: dict) -> bool:
        """Merge ``patch`` (e.g. ``{"investigation_id": …, "probe_id": …,
        "purpose": …}``) into the branch's ``session_branches.meta_json`` —
        same merge shape as ``SessionBranchMixin.set_branch_title``
        (hermes_state_branches.py:47-63). The structural provenance a joined
        branch result carries back to its investigation (no embedding)."""
        def _do(conn: sqlite3.Connection) -> int:
            row = conn.execute(
                "SELECT meta_json FROM session_branches WHERE branch_id = ?",
                (branch_id,)).fetchone()
            if row is None:
                return 0
            meta = {}
            if row[0]:
                try:
                    meta = json.loads(row[0])
                except (ValueError, TypeError):
                    meta = {}
            meta.update(patch)
            conn.execute(
                "UPDATE session_branches SET meta_json = ? WHERE branch_id = ?",
                (json.dumps(meta, ensure_ascii=False), branch_id))
            return 1
        return bool(self._execute_write(_do, patience_s=self._TRANSCRIPT_WRITE_PATIENCE_S))

    # --------------------------------------------------------- predictions

    def add_prediction(self, probe_id: str, hypothesis_id: str, expected: str) -> tuple[str, bool]:
        """Record a differentiating expectation for one (probe, hypothesis)
        pair BEFORE the probe runs. ``created`` is False when the probe already
        has an observation (a prediction written after the fact is not a
        prediction) or the pair was already predicted (idempotent replay)."""
        pred_id = "prd" + uuid.uuid4().hex[:10]
        def _do(conn: sqlite3.Connection) -> int:
            if conn.execute(
                    "SELECT 1 FROM investigation_observations WHERE probe_id = ?",
                    (probe_id,)).fetchone() is not None:
                return 0
            if conn.execute(
                    "SELECT 1 FROM investigation_predictions WHERE probe_id = ? AND hypothesis_id = ?",
                    (probe_id, hypothesis_id)).fetchone() is not None:
                return 0
            conn.execute(
                """INSERT INTO investigation_predictions
                       (prediction_id, probe_id, hypothesis_id, expected, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (pred_id, probe_id, hypothesis_id, str(expected), time.time()))
            row = conn.execute(
                "SELECT investigation_id FROM investigation_probes WHERE probe_id = ?",
                (probe_id,)).fetchone()
            if row is not None:
                self._bump_revision(conn, row["investigation_id"])
            return 1
        created = bool(self._execute_write(_do, patience_s=self._TRANSCRIPT_WRITE_PATIENCE_S))
        return pred_id, created

    def list_predictions(self, probe_id: str) -> list[dict]:
        return [dict(r) for r in self._read_all(
            "SELECT * FROM investigation_predictions WHERE probe_id = ? ORDER BY created_at",
            (probe_id,))]

    # -------------------------------------------------------- observations

    def capture_observation(
        self, investigation_id: str, *, dedup_key: str, source: str, content: str,
        probe_id: str | None = None, meta: dict | None = None,
    ) -> str | None:
        """Attach a runtime observation candidate (from a real tool-call or a
        joined branch). Idempotent per ``dedup_key`` — a duplicate delivery of
        the same call/branch inserts nothing and returns None, so the same
        evidence is never counted twice. Returns the new observation_id.

        The candidate is NOT yet assessed: it becomes accounted only through
        ``assess_observation`` (the model's compare step).
        """
        obs_id = "obs" + uuid.uuid4().hex[:10]
        body = str(content)
        if len(body) > OBSERVATION_MAX_CHARS:
            body = body[:OBSERVATION_MAX_CHARS] + "…[truncated]"
        meta = dict(meta or {})
        if probe_id is not None and not self.list_predictions(probe_id):
            # Provenance diagnostic: a result arrived for a probe whose
            # discriminating expectations were never written down.
            meta["no_predictions"] = True

        def _do(conn: sqlite3.Connection) -> int:
            return conn.execute(
                """INSERT OR IGNORE INTO investigation_observations
                       (observation_id, investigation_id, probe_id, source, dedup_key,
                        content, created_at, assessed, meta_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)""",
                (obs_id, investigation_id, probe_id, source, dedup_key,
                 body, time.time(), json.dumps(meta, ensure_ascii=False))).rowcount
        fresh = self._execute_write(_do, patience_s=self._TRANSCRIPT_WRITE_PATIENCE_S)
        if not fresh:
            return None
        if probe_id is not None:
            self.set_probe_status(probe_id, "observed")
        return obs_id

    def get_observation(self, observation_id: str) -> dict | None:
        row = self._read_one(
            "SELECT * FROM investigation_observations WHERE observation_id = ?",
            (observation_id,))
        return dict(row) if row else None

    def list_observations(self, investigation_id: str, assessed: bool | None = None) -> list[dict]:
        if assessed is None:
            sql = """SELECT * FROM investigation_observations
                     WHERE investigation_id = ? ORDER BY created_at"""
            params: tuple = (investigation_id,)
        else:
            sql = """SELECT * FROM investigation_observations
                     WHERE investigation_id = ? AND assessed = ? ORDER BY created_at"""
            params = (investigation_id, 1 if assessed else 0)
        return [dict(r) for r in self._read_all(sql, params)]

    def unassessed_count(self, investigation_id: str) -> int:
        row = self._read_one(
            """SELECT COUNT(*) AS n FROM investigation_observations
               WHERE investigation_id = ? AND assessed = 0""", (investigation_id,))
        return int(row["n"]) if row else 0

    def assess_observation(self, observation_id: str, verdict: str,
                           hypothesis_updates: list[dict] | None = None) -> bool:
        """The model's compare step: an observation-candidate becomes
        accounted ONLY here. ``verdict`` and the hypothesis status words are
        stored verbatim; the core interprets nothing. ``hypothesis_updates`` is
        ``[{"hypothesis_id": …, "status": …}]``."""
        def _do(conn: sqlite3.Connection) -> int:
            row = conn.execute(
                """SELECT investigation_id, assessed FROM investigation_observations
                   WHERE observation_id = ?""", (observation_id,)).fetchone()
            if row is None:
                return 0
            conn.execute(
                """UPDATE investigation_observations
                   SET assessed = 1, assessed_at = ?, verdict = ?
                   WHERE observation_id = ?""",
                (time.time(), str(verdict), observation_id))
            for upd in hypothesis_updates or []:
                hid, status = upd.get("hypothesis_id"), upd.get("status")
                if hid and status:
                    conn.execute(
                        "UPDATE investigation_hypotheses SET status = ? WHERE hypothesis_id = ?",
                        (status, hid))
            self._bump_revision(conn, row["investigation_id"])
            return 1
        return bool(self._execute_write(_do, patience_s=self._TRANSCRIPT_WRITE_PATIENCE_S))

    # ----------------------------------------------------------- closing

    def close_investigation(self, investigation_id: str, summary: str,
                            constraints: str = "") -> dict:
        """Close with the model's conclusion + limitations. Closes only when
        every observation candidate has been assessed; otherwise the
        investigation STAYS OPEN and records a diagnostic (the turn may still
        end and answer the human — closing is optional, silently dropping
        evidence is not)."""
        def _do(conn: sqlite3.Connection) -> dict:
            row = conn.execute(
                "SELECT status FROM investigations WHERE investigation_id = ?",
                (investigation_id,)).fetchone()
            if row is None:
                return {"closed": False, "error": "no such investigation"}
            un = conn.execute(
                """SELECT COUNT(*) AS n FROM investigation_observations
                   WHERE investigation_id = ? AND assessed = 0""",
                (investigation_id,)).fetchone()["n"]
            if un:
                meta_row = conn.execute(
                    "SELECT meta_json FROM investigations WHERE investigation_id = ?",
                    (investigation_id,)).fetchone()
                try:
                    meta = json.loads(meta_row[0]) if meta_row and meta_row[0] else {}
                except (ValueError, TypeError):
                    meta = {}
                meta[_CLOSE_DIAGNOSTIC_KEY] = {
                    "unassessed": int(un), "at": time.time(),
                    "pending_summary": str(summary)}
                conn.execute(
                    "UPDATE investigations SET meta_json = ? WHERE investigation_id = ?",
                    (json.dumps(meta, ensure_ascii=False), investigation_id))
                return {"closed": False, "unassessed": int(un)}
            conn.execute(
                """UPDATE investigations
                   SET status = 'closed', closed_at = ?, summary = ?, constraints = ?,
                       revision = revision + 1
                   WHERE investigation_id = ? AND status = 'open'""",
                (time.time(), str(summary), str(constraints), investigation_id))
            return {"closed": True, "unassessed": 0}
        return self._execute_write(_do, patience_s=self._TRANSCRIPT_WRITE_PATIENCE_S)

    def close_diagnostic(self, investigation_id: str) -> dict | None:
        """The last refused-close diagnostic, if any (for the render/hook)."""
        inv = self.get_investigation(investigation_id)
        if not inv:
            return None
        try:
            meta = json.loads(inv.get("meta_json") or "{}")
        except (ValueError, TypeError):
            return None
        diag = meta.get(_CLOSE_DIAGNOSTIC_KEY)
        return diag if isinstance(diag, dict) else None

    # ------------------------------------------------------ restart settle

    def settle_unresolved_probes(self, session_id: str | None = None) -> int:
        """Boot recovery: probes dispatched before a crash whose result never
        arrived get status ``unknown_outcome`` — the outcome's source is
        unknown, so the core NEVER re-runs them (a re-run could double a
        side-effect). Returns the number of probes settled. Open investigations
        are left open; waking the owner is the boot/auto-resume hook's job
        (spec § Restart)."""
        where = "AND i.session_id = ?" if session_id else ""
        params: tuple = (session_id,) if session_id else ()
        def _do(conn: sqlite3.Connection) -> int:
            rows = conn.execute(
                f"""SELECT p.probe_id FROM investigation_probes p
                    JOIN investigations i ON i.investigation_id = p.investigation_id
                    WHERE i.status = 'open' AND p.status = 'dispatched'
                      AND NOT EXISTS (SELECT 1 FROM investigation_observations o
                                      WHERE o.probe_id = p.probe_id)
                      {where}""", params).fetchall()
            for r in rows:
                conn.execute(
                    "UPDATE investigation_probes SET status = 'unknown_outcome' WHERE probe_id = ?",
                    (r["probe_id"],))
            return len(rows)
        rc = self._execute_write(_do, patience_s=self._TRANSCRIPT_WRITE_PATIENCE_S)
        return int(rc) if isinstance(rc, int) else 0

    # --------------------------------------------------------------- render

    def render_active_investigation(self, session_id: str) -> str | None:
        """The short service record the pre-request hook injects (hook 1):
        question, hypotheses with their recorded status words, probes with
        predictions and observation accounting. Pure formatting of stored
        rows — no interpretation, no model calls."""
        inv = self.get_open_investigation(session_id)
        if inv is None:
            return None
        lines = [f"[investigation {inv['investigation_id']} rev{inv['revision']}] "
                 f"Q: {inv['question']}"]
        for h in self.list_hypotheses(inv["investigation_id"]):
            lines.append(f"  H {h['hypothesis_id']} [{h['status']}]: {h['statement']}")
        for p in self.list_probes(inv["investigation_id"]):
            preds = self.list_predictions(p["probe_id"])
            suffix = f" branch={p['branch_id']}" if p.get("branch_id") else ""
            lines.append(f"  P {p['probe_id']} [{p['status']}]{suffix}: {p['description']}")
            for pr in preds:
                lines.append(f"      if {pr['hypothesis_id']} -> {pr['expected']}")
        un = self.unassessed_count(inv["investigation_id"])
        if un:
            obs_ids = ", ".join(o["observation_id"] for o in
                                self.list_observations(inv["investigation_id"], assessed=False))
            lines.append(f"  UNASSESSED observations ({un}): {obs_ids} — call assess before finishing")
        diag = self.close_diagnostic(inv["investigation_id"])
        if diag:
            lines.append(f"  close pending: {diag['unassessed']} observation(s) unassessed")
        return "\n".join(lines)
