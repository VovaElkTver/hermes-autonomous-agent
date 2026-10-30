"""Regression: /resume main from a fork child must not trip the Matrix origin guard.

The fork switch evicts the parent's routing entry, so origin lookup returns None
for the room's OWN root console and /resume main was blocked as if foreign.
A target inside the current session's lineage is same-room by construction.
"""
from __future__ import annotations

import pytest

from gateway.branch_prefix import _lineage_ids
from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    instance = SessionDB(db_path=tmp_path / "state.db")
    yield instance
    instance.close()


@pytest.mark.asyncio
async def test_lineage_includes_fork_parent(db):
    """`/resume main` resolves to the parent; the access guard allows exactly
    because the parent is in the child's lineage (live bug 2026-10-30: the
    guard had no lineage awareness and replied matrix_blocked_no_origin)."""
    db.ensure_session("parent", source="matrix", session_key="k-room")
    db.append_messages_batch("parent", [dict(role="user", content="seed")])
    base = db.get_last_message_uid("parent")
    db.ensure_session("child", source="matrix", session_key="k-room",
                      parent_session_id="parent")
    db.create_branch_row(db.new_branch_id(), "parent", "child", base, title="test2")
    ids = await _lineage_ids(db, "child")
    assert "parent" in ids and "child" in ids


@pytest.mark.asyncio
async def test_lineage_stops_at_fork_family(db):
    """An unrelated session is NOT in the lineage — the guard keeps its teeth
    for genuinely foreign targets."""
    db.ensure_session("parent", source="matrix", session_key="k-room")
    db.append_messages_batch("parent", [dict(role="user", content="seed")])
    db.ensure_session("child", source="matrix", session_key="k-room",
                      parent_session_id="parent")
    db.ensure_session("stranger", source="matrix", session_key="k-other")
    assert "stranger" not in await _lineage_ids(db, "child")


def test_guard_signature_accepts_current_session():
    """The IDOR guard takes current_session_id (lineage allowance hook)."""
    import inspect
    from gateway.slash_commands_session import GatewaySessionCommandsMixin
    sig = inspect.signature(GatewaySessionCommandsMixin._resume_access_denied_reply)
    assert "current_session_id" in sig.parameters
