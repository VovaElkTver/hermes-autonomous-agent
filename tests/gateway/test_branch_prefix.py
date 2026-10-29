"""Multi-console branch labels (gateway.branch_prefix) — unit tests.

Uses the real SessionDB (sync methods exercise _maybe_await's direct path) plus
one awaitable facade (async gateway wrapper path).
"""
from __future__ import annotations

import pytest

from gateway.branch_prefix import (
    MAIN_LABEL, decorate_response, format_branch_prefix, resolve_branch_label,
)
from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    instance = SessionDB(db_path=tmp_path / "state.db")
    yield instance
    instance.close()


def _sessions_with_fork(db: SessionDB, title: str = "side"):
    db.ensure_session("parent", source="test", session_key="k-parent")
    db.append_messages_batch("parent", [dict(role="user", content="seed")])
    base_uid = db.get_last_message_uid("parent")
    db.ensure_session("child", source="test", session_key="k-parent@b1",
                      parent_session_id="parent")
    bid = db.new_branch_id()
    db.create_branch_row(bid, "parent", "child", base_uid)
    db.set_session_title("child", title)
    return bid


@pytest.mark.asyncio
async def test_plain_session_unlabeled(db):
    db.ensure_session("solo", source="test", session_key="k-solo")
    out = await decorate_response(db, "solo", "hello")
    assert out == "hello"


@pytest.mark.asyncio
async def test_main_prefixed_only_with_active_forks(db):
    bid = _sessions_with_fork(db)
    assert await decorate_response(db, "parent", "hello") == f"\u2387 {MAIN_LABEL}: hello"
    db.set_branch_status(bid, "joined")
    assert await decorate_response(db, "parent", "hello") == "hello"


@pytest.mark.asyncio
async def test_fork_child_carries_fork_name(db):
    _sessions_with_fork(db, title="audit")
    out = await decorate_response(db, "child", "result text")
    assert out == "\u2387 audit: result text"


@pytest.mark.asyncio
async def test_nested_child_speaks_own_name_not_main(db):
    bid = _sessions_with_fork(db, title="audit")          # parent -> audit
    db.append_messages_batch("child", [dict(role="user", content="c1")])
    base = db.get_last_message_uid("child")
    db.ensure_session("grand", source="test", session_key="k@b2", parent_session_id="child")
    db.create_branch_row(db.new_branch_id(), "child", "grand", base)
    db.set_session_title("grand", "deep")
    # audit itself has an active fork: still speaks as "audit", never "main"
    assert await decorate_response(db, "child", "x") == "\u2387 audit: x"
    assert await decorate_response(db, "grand", "y") == "\u2387 deep: y"
    assert bid  # keep linter honest


@pytest.mark.asyncio
async def test_joined_child_untagged(db):
    bid = _sessions_with_fork(db)
    db.set_branch_status(bid, "abandoned")
    assert await decorate_response(db, "child", "hello") == "hello"
    assert await decorate_response(db, "parent", "hello") == "hello"


@pytest.mark.asyncio
async def test_idempotent_and_empty_and_none_db():
    assert await decorate_response(None, "x", "hello") == "hello"
    assert await decorate_response(None, None, "") == ""
    one = format_branch_prefix("side")
    assert await decorate_response(AsyncFakeDB(label="side"), "child", "hi") == one + "hi"
    # already-prefixed text is never double-tagged
    assert await decorate_response(AsyncFakeDB(label="side"), "child", one + "hi") == one + "hi"


class AsyncFakeDB:
    """Awaitable facade like the gateway's async session db."""

    def __init__(self, label):
        self.label = label

    async def get_branch_for_child(self, session_id):
        return {"status": "active", "branch_id": "b123"}

    async def get_session_title(self, session_id):
        return self.label

    async def has_active_branches(self, session_id):
        return False


@pytest.mark.asyncio
async def test_unnamed_fork_falls_back_to_branch_id(db):
    db.ensure_session("parent", source="test", session_key="k-parent")
    db.append_messages_batch("parent", [dict(role="user", content="seed")])
    db.ensure_session("child", source="test", session_key="k@b", parent_session_id="parent")
    bid = db.new_branch_id()
    db.create_branch_row(bid, "parent", "child", db.get_last_message_uid("parent"))
    # no title set: label = branch id, never empty prefix
    assert await resolve_branch_label(db, "child") == bid


@pytest.mark.asyncio
async def test_labels_survive_compression_rotation(db):
    """A rotation mints a fresh id; the label must follow the lineage, not
    die with the bare id (this exact gap silently dropped ⎇ main: live)."""
    _sessions_with_fork(db, title="audit")
    db.ensure_session("parent-v2", source="test", session_key="k-parent",
                      parent_session_id="parent")
    db.ensure_session("child-v2", source="test", session_key="k-parent@b1",
                      parent_session_id="child")
    assert await decorate_response(db, "parent-v2", "hello") == "\u2387 main: hello"
    # child-side rotation: name comes from the BRANCH row's child title, so it
    # holds even when the rotated row itself never had a title
    assert await resolve_branch_label(db, "child-v2") == "audit"
    # rotation two hops up still resolves
    db.ensure_session("parent-v3", source="test", session_key="k-parent",
                      parent_session_id="parent-v2")
    assert await decorate_response(db, "parent-v3", "x") == "\u2387 main: x"


@pytest.mark.asyncio
async def test_resume_main_resolves_root_console(db):
    _sessions_with_fork(db)
    from gateway.branch_prefix import resolve_console_session
    assert await resolve_console_session(db, "child", MAIN_LABEL) == "parent"


@pytest.mark.asyncio
async def test_resume_main_survives_compression_rotation(db):
    _sessions_with_fork(db)
    db.ensure_session("rotated", source="test", session_key="k-parent@b1",
                      parent_session_id="child")
    from gateway.branch_prefix import resolve_console_session
    assert await resolve_console_session(db, "rotated", MAIN_LABEL) == "parent"


@pytest.mark.asyncio
async def test_resume_by_branch_name(db):
    _sessions_with_fork(db, title="side")
    from gateway.branch_prefix import resolve_console_session
    assert await resolve_console_session(db, "parent", "side") == "child"


@pytest.mark.asyncio
async def test_resume_unknown_console_name(db):
    _sessions_with_fork(db)
    from gateway.branch_prefix import resolve_console_session
    assert await resolve_console_session(db, "parent", "nope") is None
