"""`/return` and `/leave` — walk the branch you stand on back to its parent.

Write-dialog doctrine (Вл 30.10): while a `wd:` branch negotiates, the parent
must not move — the branch consumes the console.  Closing must therefore be
possible FROM the branch, not only from the parent via `/join <id>`:
`/return` merges the standing branch into its parent (default work-note) and
switches the console back; `/leave` switches back leaving the branch open.

Monkeypatches SessionDB._lineage_ids (the async helper) — the handlers reach
the branch through it, mirroring how compression rotates child ids.
"""
from __future__ import annotations

import pytest

import hermes_state


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    return SessionStore(sessions_dir=tmp_path, config=GatewayConfig())


def _make_source(thread_id="544520"):
    from gateway.config import Platform
    from gateway.session import SessionSource
    return SessionSource(platform=Platform.TELEGRAM, user_id="1", chat_id="1",
                         chat_type="dm", thread_id=thread_id)


def _make_event(text, source):
    from gateway.platforms.event import MessageEvent
    return MessageEvent(text=text, source=source, message_id="m1")


def _make_runner(store):
    from gateway.run import GatewayRunner
    from hermes_state import AsyncSessionDB
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


def _forked(store, runner, source):
    """Parent seeded + forked in-place via the real handler; returns parent id."""
    from gateway.session import build_session_key
    parent = store.get_or_create_session(source)
    db = store._db
    db.append_message(parent.session_id, role="user", content="hello")
    db.append_message(parent.session_id, role="assistant", content="world")
    store.switch_session(build_session_key(source), parent.session_id)
    return parent.session_id


class TestReturnLeave:
    @pytest.mark.asyncio
    async def test_return_joins_current_branch_and_switches_parent(self, store):
        source = _make_source()
        runner = _make_runner(store)
        parent_id = _forked(store, runner, source)
        from gateway.session import build_session_key
        # enter the branch through the real /fork handler
        await runner._handle_fork_command(_make_event("/fork notes", source))
        child = store.get_or_create_session(source)
        assert child.session_id != parent_id
        store._db.append_message(child.session_id, role="user", content="branch finding")

        reply = await runner._handle_return_command(_make_event("/return", source))
        assert "notes" in reply.lower() or "joined" in reply.lower()
        # branch closed with a work-note into the parent
        assert not store._db.has_active_branches(parent_id)
        hp = store._db.get_messages_as_conversation(parent_id)
        assert any("work-note" in str(m.get("content", "")) for m in hp)
        # console back on the parent
        back = store.get_or_create_session(source)
        assert back.session_id == parent_id

    @pytest.mark.asyncio
    async def test_return_discard_flag(self, store):
        source = _make_source("t2")
        runner = _make_runner(store)
        parent_id = _forked(store, runner, source)
        await runner._handle_fork_command(_make_event("/fork tmp", source))
        reply = await runner._handle_return_command(_make_event("/return --discard", source))
        assert "discard" in reply.lower()
        assert not store._db.has_active_branches(parent_id)
        hp = store._db.get_messages_as_conversation(parent_id)
        assert not any("work-note" in str(m.get("content", "")) for m in hp)
        assert store.get_or_create_session(source).session_id == parent_id

    @pytest.mark.asyncio
    async def test_return_on_parent_is_noop(self, store):
        source = _make_source("t3")
        runner = _make_runner(store)
        parent_id = _forked(store, runner, source)
        reply = await runner._handle_return_command(_make_event("/return", source))
        assert "not standing" in reply.lower()
        assert store.get_or_create_session(source).session_id == parent_id

    @pytest.mark.asyncio
    async def test_leave_returns_without_closing(self, store):
        source = _make_source("t4")
        runner = _make_runner(store)
        parent_id = _forked(store, runner, source)
        await runner._handle_fork_command(_make_event("/fork side", source))
        reply = await runner._handle_leave_command(_make_event("/leave", source))
        assert "side" in reply.lower()
        # branch still open, console moved up
        assert store._db.has_active_branches(parent_id)
        assert store.get_or_create_session(source).session_id == parent_id
        # and /return later still works from the parent side via /join
        reply = await runner._handle_join_command(_make_event("/join side", source))
        assert "joined" in reply.lower()
        assert not store._db.has_active_branches(parent_id)

    def test_return_leave_in_idle_dispatch_table(self):
        from gateway.run import GatewayRunner
        table = GatewayRunner._command_handler_table(
            GatewayRunner, GatewayRunner._IDLE_COMMANDS)
        assert table["return"].__name__ == "_handle_return_command"
        assert table["leave"].__name__ == "_handle_leave_command"
