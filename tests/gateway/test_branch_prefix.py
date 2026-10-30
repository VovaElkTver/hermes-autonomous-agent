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
async def test_fork_name_survives_auto_titler(db):
    db.ensure_session("parent", source="test", session_key="k-parent")
    db.append_messages_batch("parent", [dict(role="user", content="seed")])
    db.ensure_session("child", source="test", session_key="k-parent@b1",
                      parent_session_id="parent")
    bid = db.new_branch_id()
    # /fork stores the console name immutably in the branch meta...
    db.create_branch_row(bid, "parent", "child", db.get_last_message_uid("parent"),
                         title="test")
    # ...while the auto-titler rewrites the SESSION title from the first message
    db.set_session_title("child", "проверяем линию форк")
    assert await resolve_branch_label(db, "child") == "test"
    assert await decorate_response(db, "child", "ok") == "\u2387 test: ok"


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


# ---------------------------------------------------------------------------
# Option A: decoration at the adapter send boundary (install_outbound_labels).
# ---------------------------------------------------------------------------

from gateway.branch_prefix import install_outbound_labels

GLYPH = "\u2387 "  # "⎇ "


class _RecordingAdapter:
    """Stand-in for a platform adapter: records exactly what reaches the wire."""
    name = "fake"

    def __init__(self):
        self.sent = []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(("send", chat_id, content))
        return {"ok": True}

    async def edit_message(self, chat_id, message_id, content, *, finalize=False):
        self.sent.append(("edit", chat_id, content))
        return {"ok": True}

    async def send_draft(self, chat_id, draft_id, content, **kw):
        self.sent.append(("draft", chat_id, content))
        return {"ok": True}


class _FakeOrigin:
    def __init__(self, chat_id):
        self.chat_id = chat_id


class _FakeEntry:
    def __init__(self, session_id, chat_id, updated_at):
        self.session_id = session_id
        self.origin = _FakeOrigin(chat_id)
        self.updated_at = updated_at


class _FakeStore:
    def __init__(self, entries):
        self._e = entries

    def list_sessions(self, active_minutes=None):
        return list(self._e)


class _FakeGateway:
    def __init__(self, db, store=None):
        self._session_db = db
        self.session_store = store


def _ctx(session_id, chat_id="!room:example"):
    from gateway.session_context import set_session_vars
    return set_session_vars(platform="matrix", chat_id=chat_id, session_id=session_id)


@pytest.mark.asyncio
async def test_boundary_labels_interim_progress_and_edit_sends(db):
    # The tape audit (2026-10-30) found only finalize-decorated finals carried
    # the label; every other dispatch point must gain it at the send boundary.
    from gateway.session_context import clear_session_vars
    _sessions_with_fork(db, title="audit")
    adapter = _RecordingAdapter()
    install_outbound_labels(_FakeGateway(db), adapter)

    tokens = _ctx("child")
    try:
        await adapter.send("!room:example", "interim reply")
        await adapter.send(chat_id="!room:example", content="kwarg interim")
        await adapter.edit_message("!room:example", "$ev1", "progress bubble")
        await adapter.send_draft("!room:example", 7, "draft bubble")
    finally:
        clear_session_vars(tokens)

    assert adapter.sent == [
        ("send", "!room:example", GLYPH + "audit: interim reply"),
        ("send", "!room:example", GLYPH + "audit: kwarg interim"),
        ("edit", "!room:example", GLYPH + "audit: progress bubble"),
        ("draft", "!room:example", GLYPH + "audit: draft bubble"),
    ]


@pytest.mark.asyncio
async def test_boundary_idempotent_plain_sessions_and_empties(db):
    from gateway.session_context import clear_session_vars
    _sessions_with_fork(db, title="audit")
    db.ensure_session("solo", source="test", session_key="k-solo")
    adapter = _RecordingAdapter()
    install_outbound_labels(_FakeGateway(db), adapter)

    tokens = _ctx("child")
    try:
        already = GLYPH + "audit: final answer"  # finalize-decorated final
        await adapter.send("!room:example", already)
        tokens2 = _ctx("solo")
        await adapter.send("!room:example", "plain hello")
        clear_session_vars(tokens2)
        await adapter.send("!room:example", "")  # redaction-artifact drafts
    finally:
        clear_session_vars(tokens)

    assert adapter.sent[0][2] == already  # untouched, not re-prefixed
    assert adapter.sent[1][2] == "plain hello"
    assert adapter.sent[2][2] == ""


@pytest.mark.asyncio
async def test_boundary_chat_fallback_without_turn_context(db):
    # Slash dispatch outside a bound turn resolves the chat's active session
    # through the routing store (most recently updated entry for the chat).
    from datetime import UTC, datetime, timedelta
    from gateway.session_context import clear_session_vars
    _sessions_with_fork(db, title="audit")
    now = datetime.now(UTC)
    store = _FakeStore([
        _FakeEntry("parent", "!room:example", now - timedelta(hours=1)),
        _FakeEntry("child", "!room:example", now),
    ])
    adapter = _RecordingAdapter()
    install_outbound_labels(_FakeGateway(db, store=store), adapter)

    clear_session_vars([])
    await adapter.send("!room:example", "slash answer")
    assert adapter.sent[0][2] == GLYPH + "audit: slash answer"


@pytest.mark.asyncio
async def test_boundary_main_console_labels_parent_sends(db):
    from gateway.session_context import clear_session_vars
    _sessions_with_fork(db, title="audit")
    adapter = _RecordingAdapter()
    install_outbound_labels(_FakeGateway(db), adapter)
    tokens = _ctx("parent")
    try:
        await adapter.send("!room:example", "main interim")
    finally:
        clear_session_vars(tokens)
    assert adapter.sent[0][2] == GLYPH + "main: main interim"


@pytest.mark.asyncio
async def test_boundary_survives_broken_resolution(db):
    # A failing label lookup sends the message unchanged, never drops it.
    from gateway.session_context import clear_session_vars, set_session_vars

    class _Boom:
        async def get_branch_for_child(self, sid):
            raise RuntimeError("db exploded")

        def get_session(self, sid):
            raise RuntimeError("db exploded")

    adapter = _RecordingAdapter()
    install_outbound_labels(_FakeGateway(_Boom()), adapter)
    tokens = set_session_vars(platform="matrix", chat_id="!r", session_id="x")
    try:
        await adapter.send("!r", "must still arrive")
    finally:
        clear_session_vars(tokens)
    assert adapter.sent == [("send", "!r", "must still arrive")]


@pytest.mark.asyncio
async def test_boundary_install_is_idempotent_per_adapter(db):
    from gateway.session_context import clear_session_vars
    _sessions_with_fork(db, title="audit")
    adapter = _RecordingAdapter()
    install_outbound_labels(_FakeGateway(db), adapter)
    install_outbound_labels(_FakeGateway(db), adapter)  # reconnect, same instance
    tokens = _ctx("child")
    try:
        await adapter.send("!room:example", "one label only")
    finally:
        clear_session_vars(tokens)
    assert adapter.sent[0][2].count(GLYPH) == 1
    assert adapter.sent[0][2] == GLYPH + "audit: one label only"
