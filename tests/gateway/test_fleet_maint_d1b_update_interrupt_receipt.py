"""Д1b: update-pause receipt + second-encounter dismiss (incident 28.11 Win, sticky
unknown_execution pauses after a force-killed update)."""
import asyncio
import time
from contextlib import contextmanager

import pytest

from gateway.update_interrupt_receipt import (
    RECEIPT_TTL_S, active_turn_sessions, take_verdict, write_receipt)


class _FakeDB:
    def __init__(self, rows):
        import sqlite3
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE session_turn_leases (conversation_id, holder, "
                          "acquired_at, expires_at)")
        self.conn.executemany("INSERT INTO session_turn_leases VALUES (?,?,?,?)", rows)

    @contextmanager
    def _read_ctx(self):
        yield self.conn


def test_receipt_roundtrip_single_use(tmp_path):
    write_receipt(tmp_path, ["20261128_023546_7295be57", "other"])
    assert take_verdict(tmp_path, "20261128_023546_7295be57") is not None
    assert take_verdict(tmp_path, "20261128_023546_7295be57") is None  # consumed once
    assert take_verdict(tmp_path, "other") is not None
    assert not (tmp_path / "state" / "update_interrupt_receipt.json").exists()  # fully drained


def test_receipt_merge_keeps_unpaid_entries(tmp_path):
    write_receipt(tmp_path, ["a"])
    write_receipt(tmp_path, ["b"], now=time.time() + 1)
    assert take_verdict(tmp_path, "a") is not None
    assert take_verdict(tmp_path, "b") is not None


def test_receipt_ttl_expired_is_no_verdict(tmp_path):
    write_receipt(tmp_path, ["s1"], now=time.time() - RECEIPT_TTL_S - 5)
    assert take_verdict(tmp_path, "s1") is None


def test_active_turn_sessions_reads_leases():
    db = _FakeDB([("conv-a", "h1", 1.0, 2.0), ("conv-b", "h2", 1.0, 2.0)])
    assert sorted(active_turn_sessions(db)) == ["conv-a", "conv-b"]


def _person_row(admission_id):
    return {"status": "unknown", "admission_id": admission_id, "generation": 3,
            "principal_id": "person:vovak", "payload": {"native_text_v1": {"text": "hi"}}}


class _StubAuthority:
    def __init__(self):
        self.settled = []

    async def resolve_unknown_as_owner(self, ref, admission_id, generation, *, notice=None):
        self.settled.append((admission_id, notice))


def test_second_encounter_dismisses_person_unknown():
    from gateway.session_authority import SessionAuthority
    stub = _StubAuthority()
    rows = [_person_row("a1")]
    first = asyncio.run(SessionAuthority._dismiss_repeated_unknown(stub, None, rows))
    assert first is False  # episode recorded, session still bounces (one notice)
    second = asyncio.run(SessionAuthority._dismiss_repeated_unknown(stub, None, rows))
    assert second is True
    assert len(stub.settled) == 1 and stub.settled[0][0] == "a1"
    assert stub.settled[0][1]  # a user-facing notice accompanies the settle


def test_unattended_rows_not_dismissed_here():
    from gateway.session_authority import SessionAuthority
    stub = _StubAuthority()
    row = {"status": "unknown", "admission_id": "a2", "generation": 1,
           "principal_id": "automation:cron", "payload": {}}
    assert asyncio.run(SessionAuthority._dismiss_repeated_unknown(stub, None, [row])) is False
    assert asyncio.run(SessionAuthority._dismiss_repeated_unknown(stub, None, [row])) is False
    assert stub.settled == []
