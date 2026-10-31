"""Sticky /forkb thread (Вл 31.10): entering a branch created by /forkb means the thread's
console cannot switch streams by ANY route except closing the branch — /return (into parent,
closing) or /leave (step out). /resume <name>, /new, and in-place /fork against a bound
branch are refused; the auto async-delegation completion pin is gated at its own site
(run_notifications_ownership) on the same DB predicate.
"""

from __future__ import annotations

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource, SessionStore
from hermes_state import AsyncSessionDB


def _make_runner(store: SessionStore):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner.config = {}
    runner._background_tasks = set()
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._busy_ack_ts = {}
    runner._pending_approvals = {}
    runner._update_prompt_pending = {}
    runner._agent_cache_lock = None
    runner.session_store = store
    runner._session_db = AsyncSessionDB(store._db)
    runner._pending_skills_reload_notes = {}
    runner._evict_cached_agent = lambda key: None
    runner._clear_session_boundary_security_state = lambda key: None
    return runner


def _thread_source(thread_id: str) -> SessionSource:
    # Matrix DM room with a bound branch thread — /forkb's shape.
    return SessionSource(
        platform=Platform.MATRIX,
        user_id="@vovak:mx.elk.mobi",
        chat_id="!room:mx.elk.mobi",
        chat_type="dm",
        thread_id=thread_id,
    )


@pytest.fixture()
def store(tmp_path, monkeypatch):
    import hermes_state

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    return SessionStore(sessions_dir=tmp_path, config=GatewayConfig())


async def _open_bound_branch(store, runner, title="work"):
    """Parent conversation + simulated /forkb: branch row + thread-bound child route."""
    db = store._db
    parent_entry = store.get_or_create_session(_thread_source(""))
    db.append_message(parent_entry.session_id, role="user", content="root convo")
    child_id = "20261031_120000_child1"
    child_src = _thread_source("$threadB")
    db.create_session(session_id=child_id, source="matrix",
                      parent_session_id=parent_entry.session_id,
                      user_id=child_src.user_id, chat_id=child_src.chat_id,
                      chat_type=child_src.chat_type, thread_id=child_src.thread_id)
    branch_id = db.new_branch_id()
    db.create_branch_row(branch_id, parent_entry.session_id, child_id, None, title=title)
    await runner.async_session_store.get_or_create_session(child_src)  # route row for the thread
    switched = await runner.async_session_store.switch_session(
        runner._session_key_for_source(child_src), child_id)
    assert switched is not None and switched.session_id == child_id
    return parent_entry, child_id, branch_id


class TestForkbStickyThread:
    @pytest.mark.asyncio
    async def test_predicate_binds_only_thread_children(self, store):
        runner = _make_runner(store)
        parent_entry, child_id, branch_id = await _open_bound_branch(store, runner)
        lock = await runner._bound_branch_lock(child_id)
        assert lock is not None and lock["status"] == "active"
        assert await runner._bound_branch_lock(parent_entry.session_id) is None
        # closing releases the predicate
        store._db.set_branch_status(branch_id, "joined")
        assert await runner._bound_branch_lock(child_id) is None

    @pytest.mark.asyncio
    async def test_resume_name_refused_inside_bound_thread(self, store):
        runner = _make_runner(store)
        _, child_id, _ = await _open_bound_branch(store, runner)
        ev = MessageEvent(text="/resume main", source=_thread_source("$threadB"), message_id="m1")
        reply = await runner._handle_resume_command(ev)
        assert "⛔" in reply and "/forkb" in reply
        entry = await runner.async_session_store.get_or_create_session(_thread_source("$threadB"))
        assert entry.session_id == child_id  # route untouched

    @pytest.mark.asyncio
    async def test_bare_resume_listing_still_works(self, store):
        runner = _make_runner(store)
        await _open_bound_branch(store, runner)
        ev = MessageEvent(text="/resume", source=_thread_source("$threadB"), message_id="m1")
        reply = await runner._handle_resume_command(ev)
        assert "⛔" not in reply  # listing is read-only — allowed inside the thread

    @pytest.mark.asyncio
    async def test_in_place_fork_refused_inside_bound_thread(self, store):
        runner = _make_runner(store)
        _, child_id, _ = await _open_bound_branch(store, runner)
        db = store._db
        db.append_message(child_id, role="user", content="branch work")
        ev = MessageEvent(text="/fork side", source=_thread_source("$threadB"), message_id="m1")
        reply = await runner._handle_fork_command(ev)
        assert "⛔" in reply and "/fork" in reply
        entry = await runner.async_session_store.get_or_create_session(_thread_source("$threadB"))
        assert entry.session_id == child_id  # route untouched
        assert db.list_active_branches(child_id) == []  # no hidden child created

    @pytest.mark.asyncio
    async def test_new_refused_inside_bound_thread(self, store):
        runner = _make_runner(store)
        _, child_id, _ = await _open_bound_branch(store, runner)
        key = runner._session_key_for_source(_thread_source("$threadB"))
        assert store._entries[key].session_id == child_id
        ev = MessageEvent(text="/new", source=_thread_source("$threadB"), message_id="m1")
        reply = await runner._handle_reset_command(ev)
        assert "⛔" in reply
        assert store._entries[key].session_id == child_id

    @pytest.mark.asyncio
    async def test_return_closes_branch_and_releases_thread(self, store):
        runner = _make_runner(store)
        parent_entry, child_id, branch_id = await _open_bound_branch(store, runner)
        store._db.append_message(child_id, role="user", content="branch finding")
        ev = MessageEvent(text="/return", source=_thread_source("$threadB"), message_id="m1")
        reply = await runner._handle_return_command(ev)
        assert "⛔" not in reply
        rows = store._db.list_active_branches(parent_entry.session_id)
        assert all(b["branch_id"] != branch_id for b in rows)  # closed
        entry = await runner.async_session_store.get_or_create_session(_thread_source("$threadB"))
        assert entry.session_id == parent_entry.session_id  # console back to parent
        # afterwards /resume works from this thread again
        ev2 = MessageEvent(text="/resume main", source=_thread_source("$threadB"),
                           message_id="m2")
        assert "⛔" not in await runner._handle_resume_command(ev2)

    @pytest.mark.asyncio
    async def test_leave_steps_out_leaving_branch_open(self, store):
        runner = _make_runner(store)
        parent_entry, _child_id, branch_id = await _open_bound_branch(store, runner)
        ev = MessageEvent(text="/leave", source=_thread_source("$threadB"), message_id="m1")
        reply = await runner._handle_leave_command(ev)
        assert "⛔" not in reply
        branches = store._db.list_active_branches(parent_entry.session_id)
        assert any(b["branch_id"] == branch_id for b in branches)  # still open
        entry = await runner.async_session_store.get_or_create_session(_thread_source("$threadB"))
        assert entry.session_id == parent_entry.session_id

    @pytest.mark.asyncio
    async def test_parent_room_console_unaffected(self, store):
        """The lock lives on the THREAD route only — the room's main console keeps normal power."""
        runner = _make_runner(store)
        _parent_entry, _child_id, _ = await _open_bound_branch(store, runner)
        ev = MessageEvent(text="/resume main", source=_thread_source(""), message_id="m1")
        reply = await runner._handle_resume_command(ev)
        assert "⛔" not in reply
