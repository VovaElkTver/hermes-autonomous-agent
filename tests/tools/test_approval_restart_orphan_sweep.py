"""Restart-orphan approval sweep (П4, gateway-deafness contour 15.11).

A gateway restart kills armed approval waiters silently; the boot sweep finds
the rows the dead process left behind, routes a notice to the original chat,
and flips only what was actually delivered (undelivered rows retry next boot).
"""

import sqlite3
import time

import pytest

import tools.approval_ledger as L


@pytest.fixture
def ledger_on(monkeypatch):
    monkeypatch.setattr(L, "_enabled", lambda: True)
    L._connect().close()  # ensure the schema exists in the hermetic home
    yield


def _insert(approval_id, session_key, *, state="pending", created_at=None,
            resolution=None, command="rm -rf build"):
    from hermes_constants import get_hermes_home

    conn = sqlite3.connect(get_hermes_home() / "state.db")
    now = created_at if created_at is not None else time.time()
    with conn:
        conn.execute(
            "INSERT INTO pending_approvals (approval_id, session_key, created_at,"
            " expires_at, state, resolution, command) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (approval_id, session_key, now, now + 1800, state, resolution, command),
        )
    conn.close()


def _state(approval_id):
    from hermes_constants import get_hermes_home

    conn = sqlite3.connect(get_hermes_home() / "state.db")
    row = conn.execute(
        "SELECT state, resolution FROM pending_approvals WHERE approval_id=?",
        (approval_id,),
    ).fetchone()
    conn.close()
    return row


KEY = "agent:main:matrix:group:!ROOMABC:mx.elk.mobi:@user:mx.elk.mobi"


def test_orphan_rows_are_swept_and_routed(ledger_on):
    _insert("ap-old", KEY, created_at=time.time() - 3600)
    _insert("ap-expired", KEY, state="expired", created_at=time.time() - 7200)
    notices = L.sweep_restart_orphans(time.time())
    assert len(notices) == 1  # grouped per chat
    n = notices[0]
    assert n["platform"] == "matrix"
    assert n["chat_id"] == "!ROOMABC:mx.elk.mobi"
    assert sorted(n["approval_ids"]) == ["ap-expired", "ap-old"]
    assert "NOT executed" in n["text"]
    # sweep does not flip: delivery decides
    assert _state("ap-old")[0] == "pending"


def test_fresh_rows_and_resolved_rows_are_left_alone(ledger_on):
    _insert("ap-fresh", KEY, created_at=time.time())
    _insert("ap-resolved", KEY, state="approved", created_at=time.time() - 3600,
            resolution="approved")
    assert L.sweep_restart_orphans(time.time() - 60) == []
    assert _state("ap-fresh")[0] == "pending"


def test_mark_restart_lost_flips_and_dedups(ledger_on):
    _insert("ap-a", KEY, created_at=time.time() - 3600)
    boot = time.time()
    assert L.sweep_restart_orphans(boot)
    L.mark_restart_lost(["ap-a"])
    assert _state("ap-a") == ("lost_notice", "restart-lost")
    assert L.sweep_restart_orphans(boot) == []


def test_mark_restart_lost_leaves_resolved_rows(ledger_on):
    _insert("ap-b", KEY, state="approved", created_at=time.time() - 3600,
            resolution="approved")
    L.mark_restart_lost(["ap-b"])
    assert _state("ap-b") == ("approved", "approved")


def test_unroutable_session_gets_no_notice_but_stays_for_log(ledger_on):
    _insert("ap-x", "agent:main:local:cli", created_at=time.time() - 3600)
    assert L.sweep_restart_orphans(time.time()) == []
    assert _state("ap-x")[0] == "pending"  # nothing delivered, nothing flipped


def test_flag_off_is_inert(monkeypatch):
    monkeypatch.setattr(L, "_enabled", lambda: False)
    assert L.sweep_restart_orphans(time.time() - 10_000) == []


@pytest.mark.parametrize("key,expected", [
    (KEY, ("matrix", "!ROOMABC:mx.elk.mobi")),
    ("agent:main:matrix:dm:@bob:mx.elk.mobi", ("matrix", "@bob:mx.elk.mobi")),
    ("agent:main:telegram:group:12345", ("telegram", "12345")),
    ("agent:main:local:cli", None),
    ("short:key", None),
])
def test_chat_target_parsing(key, expected):
    assert L._chat_target(key) == expected
