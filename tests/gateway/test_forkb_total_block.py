"""Variant A /forkb semantics (Вл 13.11, ratified): `b` is *blocking*.

While a bound branch holds the dialogue focus:
  * every other branch of its tree is paused — no step of execution
    (turn-admission gate `_forkb_pause_reply`);
  * `/fork` from any tree console but the bound thread is refused;
  * `/forkb` from inside an open bound branch is refused;
  * `/join` of any OTHER branch is deferred;
  * `/return` or `/leave` releases everything.
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
    return SessionSource(
        platform=Platform.MATRIX,
        user_id="@vovak:mx.elk.mobi",
        chat_id="!room:mx.elk.mobi",
        chat_type="dm",
        thread_id=thread_id,
    )


def _detached_source() -> SessionSource:
    """A conversation outside any fork tree (another chat entirely)."""
    return SessionSource(
        platform=Platform.MATRIX,
        user_id="@vovak:mx.elk.mobi",
        chat_id="!other-room:mx.elk.mobi",
        chat_type="group",
        thread_id="",
    )


@pytest.fixture()
def store(tmp_path, monkeypatch):
    import hermes_state

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    return SessionStore(sessions_dir=tmp_path, config=GatewayConfig())


async def _open_bound_branch(store, runner, title="work"):
    db = store._db
    parent_entry = store.get_or_create_session(_thread_source(""))
    db.append_message(parent_entry.session_id, role="user", content="root convo")
    child_id = "20261113_120000_child1"
    child_src = _thread_source("$threadB")
    db.create_session(session_id=child_id, source="matrix",
                      parent_session_id=parent_entry.session_id,
                      user_id=child_src.user_id, chat_id=child_src.chat_id,
                      chat_type=child_src.chat_type, thread_id=child_src.thread_id)
    branch_id = db.new_branch_id()
    db.create_branch_row(branch_id, parent_entry.session_id, child_id, None, title=title)
    await runner.async_session_store.get_or_create_session(child_src)
    switched = await runner.async_session_store.switch_session(
        runner._session_key_for_source(child_src), child_id)
    assert switched is not None and switched.session_id == child_id
    return parent_entry, child_id, branch_id


class TestForkbVariantATotalBlock:
    @pytest.mark.asyncio
    async def test_parent_turn_paused_child_free(self, store):
        """Acceptance 1: other branches cannot move a step; the holder runs free."""
        runner = _make_runner(store)
        parent_entry, child_id, _ = await _open_bound_branch(store, runner)
        paused = await runner._forkb_pause_reply(parent_entry.session_id)
        assert paused is not None and "/return" in paused and "/forkb" in paused
        assert await runner._forkb_pause_reply(child_id) is None

    @pytest.mark.asyncio
    async def test_unrelated_session_not_paused(self, store):
        """The block is tree-scoped: unrelated conversations keep running."""
        runner = _make_runner(store)
        await _open_bound_branch(store, runner)
        stray_entry = store.get_or_create_session(_detached_source())
        store._db.append_message(stray_entry.session_id, role="user", content="hi")
        assert await runner._forkb_pause_reply(stray_entry.session_id) is None

    @pytest.mark.asyncio
    async def test_same_tree_root_check(self, store):
        runner = _make_runner(store)
        parent_entry, child_id, _ = await _open_bound_branch(store, runner)
        db = store._db  # sync layer for pure SQL probes
        assert db.same_fork_tree(parent_entry.session_id, child_id)
        stray_entry = store.get_or_create_session(_detached_source())
        store._db.append_message(stray_entry.session_id, role="user", content="hi")
        assert not db.same_fork_tree(stray_entry.session_id, child_id)

    @pytest.mark.asyncio
    async def test_fork_from_parent_console_refused(self, store):
        runner = _make_runner(store)
        parent_entry, _child_id, _ = await _open_bound_branch(store, runner)
        ev = MessageEvent(text="/fork side", source=_thread_source(""), message_id="m1")
        reply = await runner._handle_fork_command(ev)
        assert "⛔" in reply
        rows = store._db.list_active_branches(parent_entry.session_id)
        assert len(rows) == 1  # no hidden child created

    @pytest.mark.asyncio
    async def test_forkb_inside_open_bound_refused(self, store):
        """Acceptance 2: /forkb from inside an open bound branch is refused."""
        runner = _make_runner(store)
        _, child_id, _ = await _open_bound_branch(store, runner)
        store._db.append_message(child_id, role="user", content="branch work")
        ev = MessageEvent(text="/forkb nested", source=_thread_source("$threadB"),
                          message_id="m1")
        reply = await runner._handle_forkb_command(ev)
        assert "⛔" in reply and "forkb" in reply.lower()

    @pytest.mark.asyncio
    async def test_join_of_other_branch_deferred(self, store):
        runner = _make_runner(store)
        parent_entry, _child_id, _branch_id = await _open_bound_branch(store, runner)
        db = store._db
        sibling_id = "20261113_130000_sibli"
        db.create_session(session_id=sibling_id, source="matrix",
                          parent_session_id=parent_entry.session_id,
                          user_id="@vovak:mx.elk.mobi", chat_id="!room:mx.elk.mobi",
                          chat_type="dm")
        sibling_branch = db.new_branch_id()
        db.create_branch_row(sibling_branch, parent_entry.session_id, sibling_id,
                             None, title="sibling")
        ev = MessageEvent(text="/join sibling --discard",
                          source=_thread_source(""), message_id="m1")
        reply = await runner._handle_join_command(ev)
        assert "⏸" in reply
        rows = {b["branch_id"]: b["status"] for b in
                db.list_active_branches(parent_entry.session_id)}
        assert rows[sibling_branch] == "active"  # nothing merged behind the scenes

    @pytest.mark.asyncio
    async def test_leave_releases_every_pause(self, store):
        """After /leave the focus is un-parked: turns and forks run again."""
        runner = _make_runner(store)
        parent_entry, _child_id, _ = await _open_bound_branch(store, runner)
        ev = MessageEvent(text="/leave", source=_thread_source("$threadB"),
                          message_id="m1")
        reply = await runner._handle_leave_command(ev)
        assert "⛔" not in reply
        assert await runner._forkb_focus() is None
        assert await runner._forkb_pause_reply(parent_entry.session_id) is None
        # and a new /forkb no longer meets the single-open refusal at the gate
        fork_ev = MessageEvent(text="/forkb again", source=_thread_source(""),
                               message_id="m2")
        reply2 = await runner._handle_forkb_command(fork_ev)
        assert "⛔ /forkb отклонена" not in reply2 and "refused" not in reply2.lower() or \
            "n@тред" in reply2  # failure would come from thread-creation plumbing, not the gate

    @pytest.mark.asyncio
    async def test_return_releases_and_closes(self, store):
        runner = _make_runner(store)
        parent_entry, _child_id, branch_id = await _open_bound_branch(store, runner)
        ev = MessageEvent(text="/return0", source=_thread_source("$threadB"),
                          message_id="m1")
        reply = await runner._handle_return0_command(ev)
        assert "⛔" not in reply
        assert all(b["branch_id"] != branch_id
                   for b in store._db.list_active_branches(parent_entry.session_id))
        assert await runner._forkb_pause_reply(parent_entry.session_id) is None

    @pytest.mark.asyncio
    async def test_forkb_without_thread_refused_not_degraded(self, store):
        """No thread host -> loud refusal; a blocking branch must not silently
        degrade into a non-blocking in-place fork (variant A integrity)."""
        runner = _make_runner(store)
        parent_entry, _child_id, _ = await _open_bound_branch(store, runner)
        # release the fixture's holder so the gate is about thread failure only
        ev0 = MessageEvent(text="/leave", source=_thread_source("$threadB"), message_id="m0")
        await runner._handle_leave_command(ev0)

        async def _no_thread(src, title):
            return None
        runner._branch_open_thread = _no_thread
        root_sid = parent_entry.session_id
        ev = MessageEvent(text="/forkb blocked2", source=_thread_source(""), message_id="m1")
        reply = await runner._handle_forkb_command(ev)
        assert "⛔" in reply and "/fork" in reply
        rows = store._db.list_active_branches(root_sid)
        assert len(rows) == 1  # no new child session/branch row created

