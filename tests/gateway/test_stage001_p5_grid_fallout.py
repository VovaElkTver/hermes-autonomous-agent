"""stage001 п.5: grid fallout pinned for the fork lifecycle.

Three kernel defects the offline grid harness (scratch/p5_grid_test.py) exposed;
each is pinned here with the same real-store pattern as test_fork_join_handlers:

1. ``/join <name> --append`` died on AttributeError — the AsyncSessionDB facade
   had no public ``move_branch_rows_to_parent`` (only the ``_…_sync`` impl).
2. ``/resume main`` from a fork child hijacked onto the active lazy-fork
   descendant (it has messages and the newest started_at) instead of landing on
   the parent root — ``resolve_resume_session_id`` must skip ``_forked_from``
   children like it already skips branch/delegate/reset ones.
3. ``_summarize_branch`` constructed ``ContextCompressor(config=…)`` — a kwarg
   the class never had, so EVERY note-join degraded to the tail fallback; and
   ``compress()`` returns the compressed message list, not a string: the note
   must carry the SUMMARY_PREFIX block when the summarizer runs.
"""

from __future__ import annotations

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource, SessionStore
from hermes_state import AsyncSessionDB


@pytest.fixture()
def store(tmp_path, monkeypatch):
    import hermes_state

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    config = GatewayConfig()
    return SessionStore(sessions_dir=tmp_path, config=config)


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.MATRIX, chat_id="!grid:mx.test", chat_name="grid",
        chat_type="group", user_id="@u:mx.test",
    )


def _make_event(text: str, source: SessionSource) -> MessageEvent:
    return MessageEvent(text=text, source=source, message_id="m1")


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


class TestJoinAppendPublicFacade:
    """Fix 1: AsyncSessionDB must expose the name join_branch awaits."""

    def test_facade_method_exists(self, store):
        # AsyncSessionDB delegates via __getattr__: the sync facade must carry the
        # public name join_branch awaits (it used to only have the _…_sync impl).
        facade = AsyncSessionDB(store._db)
        assert callable(getattr(facade, "move_branch_rows_to_parent", None))
        assert callable(getattr(store._db, "move_branch_rows_to_parent", None))

    @pytest.mark.asyncio
    async def test_append_join_moves_raw_rows(self, store):
        source = _make_source()
        parent = store.get_or_create_session(source)
        db = store._db
        db.append_message(parent.session_id, role="user", content="base turn")
        runner = _make_runner(store)
        await runner._handle_fork_command(_make_event("/fork raw", source))
        child = store.get_or_create_session(source).session_id
        db.append_message(child, role="user", content="raw body line")
        db.append_message(child, role="assistant", content="raw answer line")
        # back to the parent console, join with --append
        key = store._generate_session_key(source)
        store.switch_session(key, parent.session_id)
        reply = await runner._handle_join_command(_make_event("/join raw --append", source))
        assert "Joined branch" in reply or "join" in reply.lower()
        branch = db.list_active_branches(parent.session_id)
        assert not branch  # closed
        parent_contents = [str(m.get("content", ""))
                           for m in db.get_messages_as_conversation(parent.session_id)]
        assert any("raw body line" in c for c in parent_contents)
        assert not db.get_messages_as_conversation(child)  # child drained


class TestResumeMainSkipsForkChildren:
    """Fix 2: resolve_resume_session_id must not hijack onto a lazy-fork child."""

    def test_resume_main_lands_on_parent(self, store):
        source = _make_source()
        db = store._db
        parent = store.get_or_create_session(source)
        db.append_message(parent.session_id, role="user", content="main line")
        child = parent.session_id + "-fork"
        db.ensure_session(child, source="matrix", session_key="k-fork-p5",
                          parent_session_id=parent.session_id,
                          model_config={"_forked_from": parent.session_id})
        # the fork child is NEWER and HAS messages — the forward walk would take it
        db.append_message(child, role="user", content="side-channel output")
        assert db.resolve_resume_session_id(parent.session_id) == parent.session_id

    def test_compression_child_still_followed(self, store):
        """The legitimate continuation child (_compressed_from) must still win."""
        source = _make_source()
        db = store._db
        parent = store.get_or_create_session(source)
        child = parent.session_id + "-comp"
        db.ensure_session(child, source="matrix", session_key="k-comp-p5",
                          parent_session_id=parent.session_id,
                          model_config={"_compressed_from": parent.session_id})
        db.append_message(child, role="user", content="post-rotation turn")
        got = db.resolve_resume_session_id(parent.session_id)
        assert got == child


class TestSummarizeBranchCanonicalConstruction:
    """Fix 3: no ``config=`` kwarg; summary extracted from the compressed list."""

    @pytest.mark.asyncio
    async def test_constructor_called_canonically(self, store, monkeypatch):
        import agent.context_compressor as cc

        captured = {}

        class FakeCompressor(cc.ContextCompressor):
            def __init__(self, model, **kwargs):
                captured["model"] = model
                captured["kwargs"] = kwargs

            def compress(self, messages, **kw):
                return [{"role": "system",
                         "content": cc.SUMMARY_PREFIX + "  ## Decisions\nbranch summary body"}]

        source = _make_source()
        parent = store.get_or_create_session(source)
        db = store._db
        db.append_message(parent.session_id, role="user", content="base")
        runner = _make_runner(store)
        await runner._handle_fork_command(_make_event("/fork sum", source))
        child = store.get_or_create_session(source).session_id
        db.append_message(child, role="user", content="finding")
        monkeypatch.setattr(cc, "ContextCompressor", FakeCompressor)
        text = await runner._summarize_branch(child)
        assert "branch summary body" in text
        assert "config" not in captured["kwargs"]  # canonical signature, no config= kwarg
        assert captured["kwargs"].get("quiet_mode") is True

    @pytest.mark.asyncio
    async def test_short_transcript_falls_back_to_tail(self, store, monkeypatch):
        """Below the compress window (no SUMMARY block) the raw tail is the note."""
        import agent.context_compressor as cc

        class NoOpCompressor(cc.ContextCompressor):
            def __init__(self, model, **kwargs):
                pass

            def compress(self, messages, **kw):
                return messages  # structural no-op returns the transcript itself

        source = _make_source()
        parent = store.get_or_create_session(source)
        db = store._db
        db.append_message(parent.session_id, role="user", content="base")
        runner = _make_runner(store)
        await runner._handle_fork_command(_make_event("/fork tail", source))
        child = store.get_or_create_session(source).session_id
        db.append_message(child, role="user", content="tail finding")
        monkeypatch.setattr(cc, "ContextCompressor", NoOpCompressor)
        text = await runner._summarize_branch(child)
        assert "tail finding" in text
