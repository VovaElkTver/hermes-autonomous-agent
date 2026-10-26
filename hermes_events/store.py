"""Separate SQLite store for the event registry (vovak 26.10: "отдельная db").

The queue state lives in its OWN database file — ``events.db`` at the hermes
home root, beside approval.db / kanban.db / response_store.db — never inside
state.db. Rationale of the split: the processor ticks on wall-clock time and
rewrites ``t`` on every rearm; that write pattern must not ride along with the
multi-GB session store or its FTS/rebuild machinery.

Handlers are not pickled: a row keeps an importable reference
(``module:qualname``) resolved at load time. A registration whose handler is a
closure/lambda must supply an explicit ``handler_ref`` or it is refused —
silently unrestorable events are worse than a loud registration error.

Restart semantics are at-least-once: a row left ``in_flight`` by a crash is
re-armed as due on load, never dropped and never duplicated.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS events (
  id          TEXT PRIMARY KEY,
  kind        TEXT NOT NULL,
  t           REAL NOT NULL,
  period_s    REAL,
  handler_ref TEXT NOT NULL,
  meta        TEXT NOT NULL DEFAULT '{}',
  in_flight   INTEGER NOT NULL DEFAULT 0,
  wait_again  INTEGER NOT NULL DEFAULT 0
);
"""


def default_db_path() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "events.db"


class EventStore:
    """One table, one file, one owner: the Registry (via write-through).

    Connections are short-lived per operation — the ``sqlite_util`` fd-leak
    discipline (#69567); the tick rate is seconds, not nanoseconds, so the
    open/close cost is irrelevant next to correctness.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else default_db_path()
        from hermes_cli.sqlite_util import transaction

        with transaction(self._connect()) as conn:
            conn.executescript(SCHEMA_SQL)

    def _connect(self) -> sqlite3.Connection:
        from hermes_cli.sqlite_util import open_db

        def initialize(conn: sqlite3.Connection) -> None:
            conn.executescript(SCHEMA_SQL)

        return open_db(
            self.path, db_label="events.db", busy_timeout_ms=10_000,
            initialize=initialize,
        )

    # -- writes ---------------------------------------------------------
    def upsert(
        self, *, id: str, kind: str, t: float, period_s: float | None,
        handler_ref: str, meta: dict[str, Any], in_flight: bool,
        wait_again: bool,
    ) -> None:
        from hermes_cli.sqlite_util import transaction

        with transaction(self._connect()) as conn:
            conn.execute(
                """INSERT INTO events
                       (id, kind, t, period_s, handler_ref, meta,
                        in_flight, wait_again)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                       kind=excluded.kind, t=excluded.t,
                       period_s=excluded.period_s,
                       handler_ref=excluded.handler_ref,
                       meta=excluded.meta, in_flight=excluded.in_flight,
                       wait_again=excluded.wait_again""",
                (id, kind, t, period_s, handler_ref,
                 json.dumps(meta, ensure_ascii=False),
                 int(in_flight), int(wait_again)),
            )

    def delete(self, id: str) -> None:
        from hermes_cli.sqlite_util import transaction

        with transaction(self._connect()) as conn:
            conn.execute("DELETE FROM events WHERE id=?", (id,))

    # -- reads ----------------------------------------------------------
    def rows(self) -> Iterable[sqlite3.Row]:
        conn = self._connect()
        try:
            return list(conn.execute("SELECT * FROM events ORDER BY t"))
        finally:
            conn.close()

    def count(self) -> int:
        conn = self._connect()
        try:
            return int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        finally:
            conn.close()
