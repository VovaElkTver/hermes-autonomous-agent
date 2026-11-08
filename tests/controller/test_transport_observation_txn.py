"""Regression: a failed transport-observation write must never leave an open
transaction on the caller's long-lived connection (dev wedge 2026-11-08 —
MatrixAdapter._transport_conn stuck in `in_transaction` with the WAL writer slot
held; every other state.db writer BUSYed until process restart)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from controller.model import connect as model_connect
from controller.transport import record_observation, SYNTHETIC, UNVERIFIED


class FlakyConn:
    """Delegating wrapper with injectable failure points (sqlite3.Connection attrs are read-only)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.fail_on_execute: str | None = None
        self.fail_commit_left = 0

    def execute(self, sql, *args):
        if self.fail_on_execute and self.fail_on_execute in sql:
            raise sqlite3.OperationalError("database is locked")
        return self._conn.execute(sql, *args)

    def commit(self):
        if self.fail_commit_left > 0:
            self.fail_commit_left -= 1
            raise sqlite3.OperationalError("database is locked")
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    @property
    def in_transaction(self):
        return self._conn.in_transaction

    @property
    def total_changes(self):
        return self._conn.total_changes

    def close(self):
        self._conn.close()


@pytest.fixture()
def db(tmp_path: Path):
    conn = model_connect(tmp_path / "state.db")
    yield FlakyConn(conn)
    conn.close()


def _record(conn) -> dict:
    return record_observation(
        conn, connector="matrix", channel="sync", room_id="!r:x", event_id="$e1",
        event_type="m.room.message", content_hash="h", sender="@u:x",
        relation={}, account="@a:x", device="DEV", sync_batch_id="b1")


def test_record_observation_leaves_no_open_txn_on_success(db: FlakyConn) -> None:
    _record(db)
    assert not db.in_transaction


def test_record_observation_rolls_back_and_reraises_on_failure(db: FlakyConn) -> None:
    _record(db)
    db.fail_on_execute = "INSERT INTO event_observations"
    with pytest.raises(sqlite3.OperationalError):
        _record(db)
    assert not db.in_transaction, "failed write left an implicit transaction open"
    # The partially-written observation must not be durable: only the first
    # successful record exists in the table.
    rows = db._conn.execute("SELECT COUNT(*) FROM transport_observations").fetchone()
    assert rows[0] == 1, "failed write left uncommitted/partial rows behind"


def test_record_observation_survives_commit_busy_then_succeeds(db: FlakyConn) -> None:
    db.fail_commit_left = 1
    with pytest.raises(sqlite3.OperationalError):
        _record(db)
    assert not db.in_transaction, "failed commit left an open transaction"
    # After rollback the same record can be written cleanly.
    result = _record(db)
    assert result["status"] == UNVERIFIED


def test_record_observation_validates_channel_without_txn(db: FlakyConn) -> None:
    with pytest.raises(ValueError):
        record_observation(
            db, connector="matrix", channel="nope", room_id="!r:x", event_id="$e",
            sender="@u:x", sync_batch_id="b")
    assert not db.in_transaction


def test_record_observation_is_idempotent_per_event(db: FlakyConn) -> None:
    first = _record(db)
    again = record_observation(
        db, connector="matrix", channel="sync", room_id="!r:x", event_id="$e1",
        event_type="m.room.message", content_hash="h", sender="@u:x",
        relation={}, account="@a:x", device="DEV", sync_batch_id="b2")
    assert not db.in_transaction
    assert {first["status"], again["status"]} <= {UNVERIFIED, SYNTHETIC}
