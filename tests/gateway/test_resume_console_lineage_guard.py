"""stage001 п.3: the room<->console decision lives in the PLATFORM LAYER.

The interim patch 787938b895 answered "/resume main from a fork child" inside the core guard
by importing ``branch_prefix._lineage_ids`` — a Matrix-room leak in a generic mechanism. It is
withdrawn: the guard now ASKS the caller's platform layer (``owns_session`` on the live adapter
or ``owns_session_fn`` declared at ``register()``), and the answer stays true because the
session store keeps a NON-EVICTABLE origin ledger — registered at session creation, never
dropped by switch_session/fork/resume rotation (the eviction that made 787938b895 necessary).

IDOR meaning is preserved: a stranger from another room is still blocked, and a target with no
recorded origin still fails closed. The branch-label lineage helper itself (``_lineage_ids``)
is NOT withdrawn — labeling still uses it, and its invariants are pinned below.
"""
from __future__ import annotations

import inspect
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.i18n import t
from gateway.branch_prefix import _lineage_ids
from gateway.config import GatewayConfig, Platform
from gateway.session import SessionEntry, SessionSource, SessionStore, build_session_key
from hermes_state import SessionDB


# ---------------------------------------------------------------------------
# branch_prefix lineage helper — kept (console labels use it), pinned as-is
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    instance = SessionDB(db_path=tmp_path / "state.db")
    yield instance
    instance.close()


@pytest.mark.asyncio
async def test_lineage_includes_fork_parent(db):
    """`/resume main` resolves to the parent through the lineage chain."""
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
    """An unrelated session is NOT in the lineage."""
    db.ensure_session("parent", source="matrix", session_key="k-room")
    db.append_messages_batch("parent", [dict(role="user", content="seed")])
    db.ensure_session("child", source="matrix", session_key="k-room",
                      parent_session_id="parent")
    db.ensure_session("stranger", source="matrix", session_key="k-other")
    assert "stranger" not in await _lineage_ids(db, "child")


# ---------------------------------------------------------------------------
# origin ledger — non-evictable across switch_session/fork/resume rotation
# ---------------------------------------------------------------------------

ROOM_A = SessionSource(platform=Platform.MATRIX, chat_id="!room-a:hs", chat_name="Room A",
                       chat_type="group", user_id="@alice:hs")
ROOM_B = SessionSource(platform=Platform.MATRIX, chat_id="!room-b:hs", chat_name="Room B",
                       chat_type="group", user_id="@bob:hs")


def _entry(source: SessionSource, session_id: str) -> SessionEntry:
    now = datetime.now()
    return SessionEntry(session_key=build_session_key(source), session_id=session_id,
                        created_at=now, updated_at=now, origin=source,
                        platform=Platform.MATRIX, chat_type="group")


def _make_store(tmp_path, meta: dict):
    """SessionStore with the deep storage plumbing stubbed; routing-store meta emulated by *meta*."""
    with patch("gateway.session.SessionStore._ensure_loaded"):
        store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    store._loaded = True
    store._save = lambda: None
    store._promote_session_reset = lambda *a, **k: None
    store._reopen_session_row = lambda *a, **k: None
    store._record_gateway_session_peer = lambda *a, **k: None
    store._row_settled = lambda *a, **k: None
    def _db_for_key(session_key):
        return True

    store._db_for_key = _db_for_key

    def _method(name):
        if name == "set_meta":
            return lambda key, value: meta.__setitem__(key, value)
        if name == "get_meta":
            return lambda key: meta.get(key)
        return None

    store._routing_db_method = _method
    return store


@pytest.mark.asyncio
async def test_origin_survives_switch_session_eviction(tmp_path):
    """(c) origin survives fork/resume/switch rotation: a repoint evicts the parent's routing
    entry — the exact eviction that made 787938b895 necessary — and the ledger keeps answering."""
    store = _make_store(tmp_path, {})
    store._entries[build_session_key(ROOM_A)] = _entry(ROOM_A, "parent-sid")
    store._record_session_origin("parent-sid", ROOM_A)  # as done at creation
    assert store.lookup_by_session_id("parent-sid") is not None
    store.switch_session(build_session_key(ROOM_A), "child-sid")
    assert store.lookup_by_session_id("parent-sid") is None  # routing entry repointed away
    assert store.lookup_session_origin("parent-sid") == ROOM_A  # origin NOT evicted
    assert store.lookup_session_origin("child-sid") == ROOM_A  # fork child registered in turn


@pytest.mark.asyncio
async def test_first_origin_wins_repoint_cannot_forge(tmp_path):
    """A cross-room /resume must not re-register the target's provenance under the new room."""
    store = _make_store(tmp_path, {})
    store._record_session_origin("sid-a", ROOM_A)
    store._record_session_origin("sid-a", ROOM_B)  # later repoint under another room
    assert store.lookup_session_origin("sid-a") == ROOM_A


@pytest.mark.asyncio
async def test_origin_ledger_persists_across_restart(tmp_path):
    """The ledger is readable from the routing-store meta after the in-process map is gone."""
    meta: dict = {}
    store = _make_store(tmp_path, meta)
    store._record_session_origin("sid-a", ROOM_A)
    assert meta == {SessionStore._ORIGIN_META_PREFIX + "sid-a": json.dumps(ROOM_A.to_dict())}
    store._origin_ledger.clear()  # simulate restart: memory gone, meta survived
    assert store.lookup_session_origin("sid-a") == ROOM_A


def test_unknown_session_has_no_origin(tmp_path):
    """Fail-closed: a session this routing layer never saw proves nothing."""
    store = _make_store(tmp_path, {})
    assert store.lookup_session_origin("never-routed") is None


# ---------------------------------------------------------------------------
# the layer hook itself (Matrix adapter / registry declaration)
# ---------------------------------------------------------------------------

def _matrix_adapter():
    from plugins.platforms.matrix.adapter import MatrixAdapter
    return object.__new__(MatrixAdapter)  # owns_session reads no instance state


def test_matrix_hook_same_room_allows():
    assert _matrix_adapter().owns_session(ROOM_A, "sid", origin=ROOM_A, name="x") is True


def test_matrix_hook_other_room_blocks_with_room_name():
    verdict = _matrix_adapter().owns_session(ROOM_A, "sid", origin=ROOM_B, name="x")
    assert isinstance(verdict, str) and verdict == t(
        "gateway.resume.matrix_blocked_other_room", name="x", room="Room B")


def test_matrix_hook_no_origin_blocks_fail_closed():
    verdict = _matrix_adapter().owns_session(ROOM_A, "sid", origin=None, name="x")
    assert isinstance(verdict, str) and verdict == t(
        "gateway.resume.matrix_blocked_no_origin", name="x")


def test_matrix_hook_thread_of_same_room_is_other_session():
    caller = SessionSource(platform=Platform.MATRIX, chat_id="!room-a:hs", chat_type="group",
                           user_id="@alice:hs", thread_id="$t1")
    origin = SessionSource(platform=Platform.MATRIX, chat_id="!room-a:hs", chat_type="group",
                           user_id="@alice:hs", thread_id="$t2")
    verdict = _matrix_adapter().owns_session(caller, "sid", origin=origin, name="x")
    assert isinstance(verdict, str)  # thread_id is part of the key: another thread = other session


def test_matrix_hook_cross_room_flag_is_human_override():
    assert _matrix_adapter().owns_session(ROOM_A, "sid", origin=ROOM_B, name="x",
                                          allow_cross_room=True) is True


def test_layer_declares_contract_at_register():
    """stage001 п.2 merge plan: the origin-registry contract is declared at the existing layer
    declaration point — register()."""
    from plugins.platforms.matrix import adapter as matrix_adapter

    class _Ctx:
        kwargs: dict = {}

        def register_platform(self, **kwargs):
            _Ctx.kwargs = kwargs

    matrix_adapter.register(_Ctx())
    assert _Ctx.kwargs.get("owns_session_fn") is matrix_adapter._proves_same_origin
    assert callable(matrix_adapter.MatrixAdapter.owns_session)


# ---------------------------------------------------------------------------
# the core guard: asks the layer, never answers the room question itself
# ---------------------------------------------------------------------------

def _make_guard_runner(store, adapter=None):
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(platforms={}, group_sessions_per_user=True,
                                    thread_sessions_per_user=False, multiplex_profiles=False)
    runner.adapters = {Platform.MATRIX: adapter} if adapter is not None else {}
    runner.session_store = store
    runner._session_db = None
    return runner


def test_resume_guard_has_no_platform_branch():
    """Axiom 2: the generic guard no longer knows what a Matrix room is, and the withdrawn
    787938b895 lineage bypass is gone from it (the helper itself stays in branch_prefix)."""
    from gateway.slash_commands_session import GatewaySessionCommandsMixin
    src = inspect.getsource(GatewaySessionCommandsMixin._resume_access_denied_reply)
    assert "Platform.MATRIX" not in src
    assert "_lineage_ids" not in src
    sig = inspect.signature(GatewaySessionCommandsMixin._resume_access_denied_reply)
    assert "current_session_id" not in sig.parameters  # interim-patch signature fully withdrawn


@pytest.mark.asyncio
async def test_fork_child_resumes_root_console_after_route_eviction(tmp_path):
    """(a) the live-bug regression, now without the core-side lineage bypass: the fork switch
    evicted the parent's routing entry; the LAYER answers "this console is ours" from the
    non-evictable origin, so /resume main from the fork child is allowed."""
    store = _make_store(tmp_path, {})
    key = build_session_key(ROOM_A)
    store._entries[key] = _entry(ROOM_A, "parent-sid")
    store._record_session_origin("parent-sid", ROOM_A)
    store.switch_session(key, "child-sid")  # the fork: parent's route is gone
    assert store.lookup_by_session_id("parent-sid") is None
    runner = _make_guard_runner(store, _matrix_adapter())
    assert await runner._resume_access_denied_reply(
        ROOM_A, "parent-sid", "main", False, False) is None


@pytest.mark.asyncio
async def test_cross_room_stranger_still_blocked(tmp_path):
    """(b) IDOR meaning preserved: a caller from another room cannot resume a foreign handle."""
    store = _make_store(tmp_path, {})
    store._entries[build_session_key(ROOM_B)] = _entry(ROOM_B, "victim-sid")
    store._record_session_origin("victim-sid", ROOM_B)
    runner = _make_guard_runner(store, _matrix_adapter())
    denied = await runner._resume_access_denied_reply(ROOM_A, "victim-sid", "victim", False, False)
    assert denied == t("gateway.resume.matrix_blocked_other_room", name="victim", room="Room B")
    # An entirely unknown handle fails closed too (no origin to prove anything).
    denied = await runner._resume_access_denied_reply(ROOM_A, "ghost-sid", "ghost", False, False)
    assert denied == t("gateway.resume.matrix_blocked_no_origin", name="ghost")


@pytest.mark.asyncio
async def test_no_layer_hook_falls_through_to_generic_owner_check(tmp_path):
    """Telegram/Discord & co are no-op-compatible: no owns_session anywhere → the
    platform-agnostic IDOR check (_resume_target_allowed) decides exactly as before."""
    store = _make_store(tmp_path, {})
    runner = _make_guard_runner(store)  # empty adapters map, no registry entry under test
    called = {}

    async def _allowed(source, target_id, allow_override=False):
        called["args"] = (source, target_id, allow_override)
        return True

    runner._resume_target_allowed = _allowed
    src = SessionSource(platform=Platform.TELEGRAM, chat_id="42", chat_type="dm", user_id="42")
    assert await runner._resume_access_denied_reply(src, "sid", "x", False, False) is None
    assert called["args"][1] == "sid"

    async def _denied(source, target_id, allow_override=False):
        return False

    runner._resume_target_allowed = _denied
    assert await runner._resume_access_denied_reply(src, "sid", "x", False, False) == t(
        "gateway.resume.blocked_not_owner", name="x")


@pytest.mark.asyncio
async def test_registry_declared_hook_used_when_adapter_unreachable(tmp_path):
    """The register() declaration is the fallback resolution path when no live adapter answers."""
    from gateway.platform_registry import PlatformEntry
    store = _make_store(tmp_path, {})
    runner = _make_guard_runner(store)
    runner.adapters = {Platform.MATRIX: SimpleNamespace()}  # adapter without owns_session
    entry = PlatformEntry(name="matrix", label="Matrix", adapter_factory=lambda cfg: None,
                          check_fn=lambda: True,
                          owns_session_fn=lambda source, sid, **kw: True)
    with patch("gateway.platform_registry.platform_registry.get", return_value=entry):
        assert await runner._resume_access_denied_reply(ROOM_A, "sid", "x", False, False) is None


@pytest.mark.asyncio
async def test_untyped_hook_answer_is_no_answer(tmp_path):
    """A layer returning garbage (a bare MagicMock in fixtures) is treated as "no answer",
    never as authority — the guard falls to the generic check, not to a truthy mock."""
    store = _make_store(tmp_path, {})
    runner = _make_guard_runner(store, SimpleNamespace(owns_session=MagicMock()))

    async def _allowed(source, target_id, allow_override=False):
        return True

    runner._resume_target_allowed = _allowed
    assert await runner._resume_access_denied_reply(ROOM_A, "sid", "x", False, False) is None


# ---------------------------------------------------------------------------
# pre-ledger provenance (hotfix): a parent born before the ledger existed and
# already rotated away by a fork switch has no ledger entry; the persisted
# birth row (sessions.origin_json) still proves its room.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pre_ledger_parent_resumable_from_birth_row(tmp_path):
    from hermes_state import SessionDB
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.ensure_session("parent-pre-ledger", source="matrix", chat_id=ROOM_A.chat_id,
                          user_id=ROOM_A.user_id, chat_type="group",
                          origin_json=json.dumps(ROOM_A.to_dict()))
        store = _make_store(tmp_path, {})
        runner = _make_guard_runner(store, _matrix_adapter())
        runner._session_db = db
        assert runner._gateway_session_origin_for_id("parent-pre-ledger") == ROOM_A
        assert await runner._resume_access_denied_reply(
            ROOM_A, "parent-pre-ledger", "main", False, False) is None
        denied = await runner._resume_access_denied_reply(
            ROOM_B, "parent-pre-ledger", "main", False, False)
        assert denied == t("gateway.resume.matrix_blocked_other_room", name="main", room="Room A")
        assert runner._gateway_session_origin_for_id("never-seen") is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_birth_row_beats_repointed_gateway_routing(tmp_path):
    """Kernel independence: the durable birth row (sessions.origin_json) outranks mutable
    gateway routing state. A fork switch repointed the live route to another room and the
    ledger learned that repoint; /resume proof must still say the session is from its BIRTH
    room, not from whatever the routing currently claims."""
    from hermes_state import SessionDB
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.ensure_session("born-in-a", source="matrix", chat_id=ROOM_A.chat_id,
                          user_id=ROOM_A.user_id, chat_type="group",
                          origin_json=json.dumps(ROOM_A.to_dict()))
        store = _make_store(tmp_path, {"k": ROOM_B})          # route now points at Room B
        store._record_session_origin("born-in-a", ROOM_B)      # ledger agrees with the route
        runner = _make_guard_runner(store, _matrix_adapter())
        runner._session_db = db
        assert runner._gateway_session_origin_for_id("born-in-a") == ROOM_A
        # Room A caller: birth proves it theirs, even though the route says B.
        assert await runner._resume_access_denied_reply(
            ROOM_A, "born-in-a", "main", False, False) is None
    finally:
        db.close()
