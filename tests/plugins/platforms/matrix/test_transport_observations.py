"""Matrix adapter transport-observations write path (agency-model.md §16.9).

Flag-gated, off by default: with the flag on, the adapter records a raw
transport observation (transport_observations + event_observations) on the event
receive path BEFORE any semantic gating or journaling — including own events as
channel='local_echo' — and runs a bounded periodic reconciliation pass against
the same homeserver. Behavior invariance is the point: with the flag off nothing
touches the transport DB, and with it on the recording never alters what the
receive path does with the event.
"""
from collections import deque
from types import SimpleNamespace

import hashlib
import json

import pytest

from controller import model
from plugins.platforms.matrix.adapter import MatrixAdapter

MY_MXID = "@bot:example.org"
OTHER = "@alice:example.org"


def _adapter(*, transport=False, conn=None, client=None, batch=""):
    a = MatrixAdapter.__new__(MatrixAdapter)  # field injection only; no transport needed
    a._user_id = MY_MXID
    a._device_id = "DEV1"
    a._client = client
    a._closing = False
    a._startup_ts = 0.0
    a._ignored_user_patterns = []
    a._allowed_room_ids = set()
    a._processed_events = deque(maxlen=1000)
    a._processed_events_set = set()
    a._process_notices = True
    a._transport_obs = transport
    a._transport_conn = conn
    a._transport_reconcile_seconds = 300.0
    a._last_transport_reconcile = 0.0
    a._current_sync_batch = batch
    return a


def _event(event_id="$ev1", sender=OTHER, msgtype="m.text", body="hi",
           room="!r:example.org", type_="m.room.message"):
    return SimpleNamespace(
        room_id=room, sender=sender, event_id=event_id, type=type_,
        content={"msgtype": msgtype, "body": body}, timestamp=0)


class _Calls:
    def __init__(self):
        self.text_calls = []
        self.media_calls = []

    async def text(self, *args, **kwargs):
        self.text_calls.append(args)

    async def media(self, *args, **kwargs):
        self.media_calls.append(args)


def _wire_semantic_stubs(a, calls):
    a._handle_text_message = calls.text
    a._handle_media_message = calls.media


class FakeSyncStore:
    def __init__(self):
        self.stored = []

    async def put_next_batch(self, nb):
        self.stored.append(nb)


class FakeSyncClient(FakeSyncStore):
    def __init__(self, fetch_event=None, fetch_error=None):
        super().__init__()
        self._fetch_event, self._fetch_error = fetch_event, fetch_error
        self.fetch_calls = 0

    @property
    def sync_store(self):
        return self

    def handle_sync(self, sync_data):
        return None

    async def get_event(self, room_id, event_id):
        self.fetch_calls += 1
        if self._fetch_error is not None:
            raise self._fetch_error
        return self._fetch_event


def _db(tmp_path):
    return model.connect(tmp_path / "state.db")


@pytest.mark.asyncio
async def test_flag_off_default_records_nothing(tmp_path):
    """Default (flag off): the receive path behaves exactly as before and no
    transport connection is even opened."""
    a = _adapter(transport=False)
    calls = _Calls()
    _wire_semantic_stubs(a, calls)
    await a._on_room_message(_event())
    assert len(calls.text_calls) == 1  # semantic path untouched
    assert a._transport_conn is None  # never opened


@pytest.mark.asyncio
async def test_flag_on_records_observation_before_journal(tmp_path):
    """Flag on: one transport_observation + one event_observation land BEFORE the
    semantic handler runs, with the sync batch token and full event identity."""
    conn = _db(tmp_path)
    try:
        a = _adapter(transport=True, conn=conn, batch="batch-1")
        calls = _Calls()
        _wire_semantic_stubs(a, calls)
        body_event = _event(body="hello world")
        await a._on_room_message(body_event)

        obs = conn.execute("SELECT * FROM transport_observations").fetchone()
        assert obs is not None
        assert obs["connector"] == "matrix"
        assert obs["channel"] == "sync"
        assert obs["account"] == MY_MXID and obs["device"] == "DEV1"
        assert obs["room_id"] == "!r:example.org"
        assert obs["external_event_id"] == "$ev1"
        assert obs["event_type"] == "m.room.message"
        assert obs["sync_batch_id"] == "batch-1"
        assert obs["status"] == "unverified_transport_observation"
        expected_ref = hashlib.sha256(
            json.dumps(
                {"msgtype": "m.text", "body": "hello world"},
                sort_keys=True, default=str).encode("utf-8")).hexdigest()
        assert obs["raw_payload_ref"] == expected_ref

        ev = conn.execute("SELECT * FROM event_observations").fetchone()
        assert ev["event_id"] == "$ev1" and ev["sender"] == OTHER
        assert ev["content_hash"] == expected_ref
        assert ev["decryption_status"] == "plain"
        for col in ("identity_consistency", "server_retrievability", "redaction_status",
                    "timeline_placement", "normalization_consistency"):
            assert ev[col] == "pending"
        # the semantic handler still ran exactly once, with the same payload
        assert len(calls.text_calls) == 1
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_own_event_is_local_echo_channel(tmp_path):
    """Own events are recorded too, as channel='local_echo' — echo detection belongs
    to the transport layer, and the record lands even though the event is then
    dropped by the self-sender filter."""
    conn = _db(tmp_path)
    try:
        a = _adapter(transport=True, conn=conn)
        calls = _Calls()
        _wire_semantic_stubs(a, calls)
        await a._on_room_message(_event(sender=MY_MXID))
        obs = conn.execute("SELECT channel FROM transport_observations").fetchone()
        assert obs["channel"] == "local_echo"
        assert calls.text_calls == []  # self-sender filter still drops it from the semantic path
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_absorb_sync_plumbs_batch_token(tmp_path):
    """_absorb_sync stamps the current next_batch onto the adapter so every event of
    that response carries its sync batch id."""
    a = _adapter(transport=False, client=FakeSyncClient())
    sync_data = {"next_batch": "nb-42", "rooms": {"join": {}, "invite": {}}}
    nb = await a._absorb_sync(a._client, sync_data)
    assert nb == "nb-42"
    assert a._current_sync_batch == "nb-42"
    assert a._client.stored == ["nb-42"]


@pytest.mark.asyncio
async def test_reconcile_runs_when_due_and_verifies(tmp_path):
    """Periodic pass (flag on, interval elapsed): pending observations are refetched
    through the room event endpoint and the consistent one verifies end-to-end."""
    from controller import transport
    conn = _db(tmp_path)
    try:
        r = transport.record_observation(
            conn, connector="matrix", channel="sync", room_id="!r:example.org",
            event_id="$ev1", event_type="m.room.message", sender=OTHER,
            account=MY_MXID, device="DEV1", sync_batch_id="batch-1")
        fetched = SimpleNamespace(
            type="m.room.message", sender=OTHER,
            content={"msgtype": "m.text", "body": "hi"})
        client = FakeSyncClient(fetch_event=fetched)
        a = _adapter(transport=True, conn=conn, client=client)
        a._last_transport_reconcile = 0.0  # interval elapsed
        await a._maybe_reconcile_transport()
        assert client.fetch_calls == 1
        checks = conn.execute("SELECT * FROM reconciliation_checks").fetchall()
        assert len(checks) == 1
        assert checks[0]["result"] == "verified"
        assert checks[0]["method"] == "refetch_same_homeserver"
        assert checks[0]["error_code"] == ""
        assert conn.execute(
            "SELECT status FROM transport_observations WHERE observation_id=?",
            (r["observation_id"],)).fetchone()["status"] == "verified"
        # the pass is bounded: right after a run, the interval gate holds
        await a._maybe_reconcile_transport()
        assert client.fetch_calls == 1
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_reconcile_interval_gate(tmp_path):
    """Inside the interval nothing is fetched — the sync loop never pays twice."""
    conn = _db(tmp_path)
    try:
        from controller import transport
        transport.record_observation(
            conn, connector="matrix", channel="sync", room_id="!r", event_id="$e",
            event_type="m.room.message", sender=OTHER, account=MY_MXID,
            sync_batch_id="b1")
        client = FakeSyncClient(fetch_event=SimpleNamespace(
            type="m.room.message", sender=OTHER, content={"body": "x"}))
        a = _adapter(transport=True, conn=conn, client=client)
        a._last_transport_reconcile = 1e18  # "just ran"
        await a._maybe_reconcile_transport()
        assert client.fetch_calls == 0
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_fetch_failure_stored_with_error_code(tmp_path):
    """A failed room-event fetch is stored as a reconciliation RESULT with the
    homeserver error code — not dropped, not 'the event was not there'."""
    from controller import transport
    conn = _db(tmp_path)
    try:
        class _Err(Exception):
            errcode = "M_NOT_FOUND"
        r = transport.record_observation(
            conn, connector="matrix", channel="sync", room_id="!r", event_id="$gone",
            event_type="m.room.message", sender=OTHER, account=MY_MXID,
            sync_batch_id="b1")
        client = FakeSyncClient(fetch_error=_Err())
        a = _adapter(transport=True, conn=conn, client=client)
        a._last_transport_reconcile = 0.0
        await a._maybe_reconcile_transport()
        check = conn.execute(
            "SELECT * FROM reconciliation_checks WHERE observation_id=?",
            (r["observation_id"],)).fetchone()
        assert check["result"] == "fetch_failed"
        assert check["error_code"] == "M_NOT_FOUND"
        # the observation is still unverified and still pending — NOT "never existed"
        assert conn.execute(
            "SELECT status FROM transport_observations WHERE observation_id=?",
            (r["observation_id"],)).fetchone()["status"] == "unverified_transport_observation"
        assert conn.execute(
            "SELECT server_retrievability FROM event_observations WHERE event_obs_id="
            "(SELECT event_obs_id FROM event_observations WHERE observation_id=?)",
            (r["observation_id"],)).fetchone()["server_retrievability"] == "pending"
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_flag_off_never_reconciles(tmp_path):
    conn = _db(tmp_path)
    try:
        from controller import transport
        transport.record_observation(
            conn, connector="matrix", channel="sync", room_id="!r", event_id="$e",
            event_type="m.room.message", sender=OTHER, account=MY_MXID,
            sync_batch_id="b1")
        client = FakeSyncClient(fetch_event=SimpleNamespace(
            type="m.room.message", sender=OTHER, content={"body": "x"}))
        a = _adapter(transport=False, conn=conn, client=client)
        a._last_transport_reconcile = 0.0
        await a._maybe_reconcile_transport()
        assert client.fetch_calls == 0
        assert conn.execute("SELECT COUNT(*) FROM reconciliation_checks").fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_disconnect_closes_transport_conn(tmp_path):
    conn = _db(tmp_path)

    class _ConnSpy:
        """Close-recording wrapper: sqlite3.Connection exposes no portable
        'closed' attribute across the interpreters this suite runs on."""

        def __init__(self, conn):
            self._conn = conn
            self.close_calls = 0

        def close(self):
            self._conn.close()
            self.close_calls += 1

    spy = _ConnSpy(conn)
    a = _adapter(transport=True, conn=spy, client=None)
    a._media_batches = {}
    a._sync_task = None
    a._invite_join_tasks = {}
    a._reaction_redaction_tasks = set()
    a._crypto_db = None
    await a.disconnect()
    assert spy.close_calls == 1
    assert a._transport_conn is None
