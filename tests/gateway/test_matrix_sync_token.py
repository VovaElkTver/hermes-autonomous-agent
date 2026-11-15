"""R3 (gateway-deafness report): the Matrix sync token must survive restarts, so the
outage-window backlog is replayed instead of dying in the startup grace filter.

The 14.11 rtx5090 incident: 41 minutes of DMs arrived while the gateway was down; the
restart rebuilt a fresh MemorySyncStore, the full initial sync's timeline chunk was
dropped by the 5s grace filter, and the reconnect looked healthy while the backlog sat
unread. sync_token_store.py persists the next_batch token and lowers the grace floor to
its save moment; these tests pin the contract, including the M_UNKNOWN_TOKEN fallback.
"""
import asyncio
import json
import time

from plugins.platforms.matrix.adapter import MatrixAdapter, _is_permanent_matrix_auth_error


def _bare_adapter(tmp_path, *, max_age=86400.0):
    a = object.__new__(MatrixAdapter)
    a._store_dir = tmp_path
    a._sync_token_max_age = max_age
    a._startup_ts = time.time()
    a._joined_rooms = set()
    return a


class _FakeSyncStore:
    def __init__(self):
        self.batches = []

    async def put_next_batch(self, batch):
        self.batches.append(batch)

    async def get_next_batch(self):
        return self.batches[-1] if self.batches else ""


def _unknown_token_error():
    exc = Exception("M_UNKNOWN_TOKEN: Bad token")
    exc.errcode = "M_UNKNOWN_TOKEN"
    exc.http_status = 401
    return exc


class TestTokenFileRoundtrip:
    def test_persist_then_load(self, tmp_path):
        a = _bare_adapter(tmp_path)
        a._persist_batch("s72_batch")
        loaded = a._load_persisted_batch()
        assert loaded is not None and loaded[0] == "s72_batch"
        assert abs(loaded[1] - time.time()) < 5

    def test_missing_file_loads_none(self, tmp_path):
        assert _bare_adapter(tmp_path)._load_persisted_batch() is None

    def test_corrupt_file_loads_none(self, tmp_path):
        (tmp_path / "sync_token.json").write_text("{not json", encoding="utf-8")
        assert _bare_adapter(tmp_path)._load_persisted_batch() is None

    def test_stale_token_loads_none(self, tmp_path):
        (tmp_path / "sync_token.json").write_text(
            json.dumps({"next_batch": "old", "ts": time.time() - 3 * 86400}), encoding="utf-8")
        assert _bare_adapter(tmp_path)._load_persisted_batch() is None

    def test_max_age_zero_disables_persistence(self, tmp_path):
        a = _bare_adapter(tmp_path, max_age=0.0)
        a._persist_batch("s72")
        assert not (tmp_path / "sync_token.json").exists()
        assert a._load_persisted_batch() is None

    def test_clear_removes_file(self, tmp_path):
        a = _bare_adapter(tmp_path)
        a._persist_batch("s72")
        a._clear_persisted_batch()
        assert a._load_persisted_batch() is None


class TestResumePreparation:
    def test_resume_seeds_store_and_lowers_floor(self, tmp_path):
        a = _bare_adapter(tmp_path)
        saved = time.time() - 2400  # 40-minute outage, like the rtx5090 incident
        (tmp_path / "sync_token.json").write_text(
            json.dumps({"next_batch": "s_outage", "ts": saved}), encoding="utf-8")
        store = _FakeSyncStore()
        since = asyncio.run(a._prepare_sync_resume(store))
        assert since == "s_outage"
        assert store.batches == ["s_outage"]
        assert a._sync_resumed is True
        # The outage backlog (ts ≈ saved) must now clear the grace filter:
        assert a._event_older_than_grace(saved + 10) is False
        # ...while genuinely older events still drop:
        assert a._event_older_than_grace(saved - 600) is True

    def test_no_token_keeps_pre_r3_behaviour(self, tmp_path):
        a = _bare_adapter(tmp_path)
        store = _FakeSyncStore()
        assert asyncio.run(a._prepare_sync_resume(store)) == ""
        assert store.batches == []
        assert a._sync_resumed is False
        # Without resume, the classic startup−5s floor applies unchanged:
        assert a._event_older_than_grace(a._startup_ts - 60) is True
        assert a._event_older_than_grace(a._startup_ts) is False

    def test_resume_forgives_clock_jitter(self, tmp_path):
        # A token saved with ts slightly in the future (NTP nudge) is still trusted.
        (tmp_path / "sync_token.json").write_text(
            json.dumps({"next_batch": "j", "ts": time.time() + 120}), encoding="utf-8")
        assert _bare_adapter(tmp_path)._load_persisted_batch() is not None


class TestAbsorbPersistsAfterDispatch:
    def _wire(self, a, order):
        class _C:
            sync_store = _FakeSyncStore()
            crypto = None
        a._warn_encrypted_drops = lambda rooms, client: None
        a._invalidate_room_identities = lambda: None
        a._schedule_pending_invite_joins = lambda sync_data: None
        a._refresh_dm_cache = _rec(order, "dispatch")
        a._maybe_reconcile_transport = _rec(order, "reconcile")

        async def _dispatch(sync_data):
            order.append("dispatch")
        a._dispatch_sync = _dispatch
        return _C()

    def test_token_written_only_after_handlers_ran(self, tmp_path):
        a = _bare_adapter(tmp_path)
        order = []
        client = self._wire(a, order)
        nb = asyncio.run(a._absorb_sync(client, {"next_batch": "b1", "rooms": {}}))
        assert nb == "b1"
        assert order == ["dispatch", "reconcile"]
        assert json.loads((tmp_path / "sync_token.json").read_text())["next_batch"] == "b1"


def _rec(order, label):
    async def _fn(*args, **kwargs):
        order.append(label)
    return _fn


class TestUnknownTokenFallback:
    def test_initial_sync_falls_back_to_full(self, tmp_path):
        a = _bare_adapter(tmp_path)
        a._persist_batch("dead_token")
        calls = []

        class _C:
            sync_store = _FakeSyncStore()

            async def sync(self, **kw):
                calls.append(kw)
                if kw.get("since"):
                    raise _unknown_token_error()
                return {"next_batch": "fresh"}
        a._joined_rooms = set()
        absorbed = []

        async def _absorb(client, sync_data, initial=False):
            absorbed.append((sync_data, initial))
            return sync_data.get("next_batch")
        a._absorb_sync = _absorb
        a._seed_joined_rooms_after_resume = _noop_async
        asyncio.run(a._connect_initial_sync(_C(), since="dead_token"))
        assert [c.get("since") for c in calls] == ["dead_token", None]
        assert calls[1]["full_state"] is True
        assert absorbed[0][0] == {"next_batch": "fresh"}
        assert a._sync_resumed is False
        assert a._load_persisted_batch() is None

    def test_loop_carveout_resyncs_before_parking(self, tmp_path):
        a = _bare_adapter(tmp_path)
        a._closing = False
        a._sync_resumed = True
        a._persist_batch("dead_token")
        a._proxy_url = None
        a._homeserver = "https://mx.example"
        state = {"n": 0}

        class _C:
            sync_store = _FakeSyncStore()

            async def sync(self, **kw):
                state["n"] += 1
                if kw.get("since") == "seeded":
                    raise _unknown_token_error()
                a._closing = True  # end the loop after the recovery sync
                return {"next_batch": "b_next"}
        assert _is_permanent_matrix_auth_error(_unknown_token_error())  # premise: 401-class

        async def _absorb(client, sync_data, initial=False):
            return sync_data.get("next_batch")
        a._absorb_sync = _absorb
        a._warn_encrypted_drops = lambda rooms, client: None
        a._invalidate_room_identities = lambda: None
        a._schedule_pending_invite_joins = lambda sd: None

        # Mirror the loop's entry (token from the store) without duplicating its body:
        async def _seed_then_loop():
            a._client = _C()
            await a._client.sync_store.put_next_batch("seeded")
            await MatrixAdapter._sync_loop(a)
        asyncio.run(_seed_then_loop())
        assert state["n"] == 2  # raised once on the stale since, then recovered with since=""
        assert a._load_persisted_batch() is None  # stale token cleared, loop not parked


async def _noop_async(*args, **kwargs):
    return None


class TestJoinedRoomsSeed:
    def test_updates_set_from_response_object(self, tmp_path):
        a = _bare_adapter(tmp_path)
        invalidated = []

        class _Resp:
            joined_rooms = ["!a:x", "!b:x"]
        class _C:
            async def get_joined_rooms(self):
                return _Resp()
        a._invalidate_room_identities = lambda: invalidated.append(1)
        asyncio.run(a._seed_joined_rooms_after_resume(_C()))
        assert a._joined_rooms == {"!a:x", "!b:x"}
        assert invalidated == [1]

    def test_failure_is_non_fatal(self, tmp_path):
        a = _bare_adapter(tmp_path)
        class _C:
            async def get_joined_rooms(self):
                raise RuntimeError("homeserver down")
        a._invalidate_room_identities = lambda: None
        asyncio.run(a._seed_joined_rooms_after_resume(_C()))
        assert a._joined_rooms == set()
