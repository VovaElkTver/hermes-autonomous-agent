"""Transport-observations layer (agency-model.md §16.9, hermes-agency-003 §7).

The layer between platform adapters and the semantic journal: transport_observations
(+ event_observations with SIX independent characteristics — no single `verified`
flag), reconciliation_checks. Unverifiable inbounds stay `unverified_transport_observation`;
a failed refetch is a check result with an error code, not "the event never existed";
an event re-delivered in a later sync batch is stamped `synthetic_redelivery`.
"""
import pytest

from controller import transport
from controller import model
from hermes_state_common import SCHEMA_SQL
from hermes_state_schema import reconcile_state_schema

TABLES = ("transport_observations", "event_observations", "reconciliation_checks")

CHARACTERISTIC_COLS = {
    "identity_consistency", "server_retrievability", "decryption_status",
    "redaction_status", "timeline_placement", "normalization_consistency",
}


@pytest.fixture()
def conn(tmp_path):
    c = model.connect(tmp_path / "state.db")  # raw SCHEMA_SQL temp DB (controller precedent)
    yield c
    c.close()


def _table_cols(c, table):
    return {r[1] for r in c.execute(f"PRAGMA table_info({table})")}


# ---- migration -------------------------------------------------------------------

def test_fresh_db_has_transport_tables(conn):
    """SCHEMA_SQL (the single DDL authority) carries the three tables next to ctl_*."""
    for table in TABLES:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                            (table,)).fetchall()
        assert rows, f"{table} missing from fresh SCHEMA_SQL DB"


def test_reconcile_migrates_pre_transport_db(tmp_path):
    """A state.db created by the PREVIOUS release (same canonical DDL, minus the
    transport block) gains all three tables on the next reconcile_state_schema —
    the migration path for live profiles."""
    import sqlite3
    start = SCHEMA_SQL.index("-- ---- transport observations")
    # the section is the tail of the canonical DDL (its last index trails the
    # end marker); dropping it reproduces the pre-transport release exactly
    legacy_sql = SCHEMA_SQL[:start]
    db = tmp_path / "legacy.db"
    conn2 = sqlite3.connect(str(db))
    conn2.row_factory = sqlite3.Row
    conn2.executescript(legacy_sql)
    conn2.commit()
    for table in TABLES:
        assert conn2.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,)).fetchone() is None, f"legacy simulation already has {table}"
    reconcile_state_schema(conn2)
    conn2.commit()
    for table in TABLES:
        assert conn2.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    # and the six characteristic columns ship in the migration, not just the table shell
    assert CHARACTERISTIC_COLS <= _table_cols(conn2, "event_observations")
    conn2.close()


# ---- record_observation -------------------------------------------------------------

def test_record_observation_creates_both_rows_unverified(conn):
    r = transport.record_observation(
        conn, connector="matrix", channel="sync", room_id="!r:ex.org",
        event_id="$ev1", event_type="m.room.message", content_hash="sha-1",
        sender="@alice:ex.org", account="@bot:ex.org", device="DEVID1",
        sync_batch_id="sbatch1")
    assert r["status"] == transport.UNVERIFIED
    row = conn.execute("SELECT * FROM transport_observations WHERE observation_id=?",
                       (r["observation_id"],)).fetchone()
    assert (row["connector"], row["account"], row["device"], row["channel"]) == \
        ("matrix", "@bot:ex.org", "DEVID1", "sync")
    assert (row["room_id"], row["external_event_id"], row["event_type"]) == \
        ("!r:ex.org", "$ev1", "m.room.message")
    assert row["raw_payload_ref"] == "sha-1" and row["sync_batch_id"] == "sbatch1"
    ev = conn.execute("SELECT * FROM event_observations WHERE event_obs_id=?",
                      (r["event_obs_id"],)).fetchone()
    assert ev["observation_id"] == r["observation_id"]
    assert ev["sender"] == "@alice:ex.org" and ev["content_hash"] == "sha-1"
    # no single verified flag: six characteristics, pending except decryption (settled at sight)
    assert CHARACTERISTIC_COLS <= set(ev.keys())
    assert ev["decryption_status"] == "plain"
    for col in CHARACTERISTIC_COLS - {"decryption_status"}:
        assert ev[col] == "pending"
    # relation metadata is stored as JSON
    assert conn.execute("SELECT relation FROM event_observations WHERE event_obs_id=?",
                        (r["event_obs_id"],)).fetchone()["relation"] == "{}"


def test_record_observation_rejects_unknown_channel(conn):
    with pytest.raises(ValueError):
        transport.record_observation(conn, connector="matrix", channel="carrier_pigeon",
                                     room_id="!r", event_id="$e")


def test_redelivery_in_later_batch_is_synthetic(conn):
    first = transport.record_observation(
        conn, connector="matrix", channel="sync", room_id="!r", event_id="$dup",
        event_type="m.room.message", account="@bot:ex.org", sync_batch_id="b1")
    assert first["status"] == transport.UNVERIFIED
    again = transport.record_observation(
        conn, connector="matrix", channel="sync", room_id="!r", event_id="$dup",
        event_type="m.room.message", account="@bot:ex.org", sync_batch_id="b2")
    assert again["status"] == transport.SYNTHETIC  # phantom-inbound signature
    same_batch = transport.record_observation(
        conn, connector="matrix", channel="sync", room_id="!r", event_id="$dup",
        event_type="m.room.message", account="@bot:ex.org", sync_batch_id="b1")
    # an event observed across two batches is a redelivery by definition — even the
    # re-observation "back" in the first batch, which is exactly what a session
    # restore does (re-inject the history, new batch token).
    assert same_batch["status"] == transport.SYNTHETIC
    # a DIFFERENT account seeing the same event is not this account's redelivery
    other = transport.record_observation(
        conn, connector="matrix", channel="sync", room_id="!r", event_id="$dup",
        event_type="m.room.message", account="@bot2:ex.org", sync_batch_id="b3")
    assert other["status"] == transport.UNVERIFIED


def test_encrypted_observation_is_undecryptable(conn):
    r = transport.record_observation(
        conn, connector="matrix", channel="sync", room_id="!r", event_id="$enc",
        event_type="m.room.encrypted", account="@bot:ex.org", sync_batch_id="b1")
    ev = conn.execute("SELECT * FROM event_observations WHERE event_obs_id=?",
                      (r["event_obs_id"],)).fetchone()
    assert ev["decryption_status"] == "undecryptable"


# ---- reconcile ----------------------------------------------------------------------

async def _ok_fetch(room_id, event_id):
    return {"type": "m.room.message", "sender": "@alice:ex.org",
            "content": {"msgtype": "m.text", "body": "hi"}, "redacted": False}


@pytest.mark.asyncio
async def test_reconcile_verifies_consistent_event(conn):
    r = transport.record_observation(
        conn, connector="matrix", channel="sync", room_id="!r:ex.org", event_id="$ev1",
        event_type="m.room.message", sender="@alice:ex.org",
        relation={"m.thread": {"event_id": "$root"}},
        account="@bot:ex.org", sync_batch_id="b1")
    checks = await transport.reconcile(conn, fetch_event=_ok_fetch)
    assert len(checks) == 1 and checks[0]["result"] == transport.VERIFIED
    assert checks[0]["error_code"] == ""
    ev = conn.execute("SELECT * FROM event_observations WHERE event_obs_id=?",
                      (r["event_obs_id"],)).fetchone()
    assert ev["identity_consistency"] == "ok"
    assert ev["server_retrievability"] == "ok"
    assert ev["redaction_status"] == "ok"
    assert ev["timeline_placement"] == "thread_timeline"  # from the stored relation
    # normalization_consistency stays pending until a journal ref exists (stage 3)
    assert ev["normalization_consistency"] == "pending"
    obs = conn.execute("SELECT status FROM transport_observations WHERE observation_id=?",
                       (r["observation_id"],)).fetchone()
    assert obs["status"] == transport.VERIFIED
    # a verified observation drops out of the next pass
    assert await transport.reconcile(conn, fetch_event=_ok_fetch) == []


@pytest.mark.asyncio
async def test_reconcile_failed_fetch_is_result_with_error_code(conn):
    r = transport.record_observation(
        conn, connector="matrix", channel="sync", room_id="!r:ex.org", event_id="$gone",
        event_type="m.room.message", sender="@alice:ex.org",
        account="@bot:ex.org", sync_batch_id="b1")

    async def failing(room_id, event_id):
        return {"error_code": "M_NOT_FOUND"}

    checks = await transport.reconcile(conn, fetch_event=failing)
    assert len(checks) == 1
    assert checks[0]["result"] == transport.CHECK_FETCH_FAILED
    assert checks[0]["error_code"] == "M_NOT_FOUND"  # the code is stored, not swallowed
    ev = conn.execute("SELECT * FROM event_observations WHERE event_obs_id=?",
                      (r["event_obs_id"],)).fetchone()
    assert ev["server_retrievability"] == "pending"  # unknown stays unknown
    obs = conn.execute("SELECT status FROM transport_observations WHERE observation_id=?",
                       (r["observation_id"],)).fetchone()
    assert obs["status"] == transport.UNVERIFIED  # not a room fact, not "never existed"


@pytest.mark.asyncio
async def test_reconcile_mismatch_identity_stays_unverified(conn):
    r = transport.record_observation(
        conn, connector="matrix", channel="sync", room_id="!r:ex.org", event_id="$m",
        event_type="m.room.message", sender="@alice:ex.org",
        account="@bot:ex.org", sync_batch_id="b1")

    async def lying_fetch(room_id, event_id):
        return {"type": "m.room.message", "sender": "@IMPERSONATOR:ex.org",
                "content": {"msgtype": "m.text", "body": "x"}, "redacted": False}

    checks = await transport.reconcile(conn, fetch_event=lying_fetch)
    assert checks[0]["result"] == transport.UNVERIFIED
    ev = conn.execute("SELECT * FROM event_observations WHERE event_obs_id=?",
                      (r["event_obs_id"],)).fetchone()
    assert ev["identity_consistency"] == "mismatch"
    assert ev["server_retrievability"] == "ok"


@pytest.mark.asyncio
async def test_reconcile_redacted_event_is_not_verified(conn):
    r = transport.record_observation(
        conn, connector="matrix", channel="sync", room_id="!r:ex.org", event_id="$red",
        event_type="m.room.message", sender="@alice:ex.org",
        account="@bot:ex.org", sync_batch_id="b1")

    async def redacted_fetch(room_id, event_id):
        return {"type": "m.room.message", "sender": "@alice:ex.org",
                "content": None, "redacted": True}

    checks = await transport.reconcile(conn, fetch_event=redacted_fetch)
    assert checks[0]["result"] == transport.UNVERIFIED
    ev = conn.execute("SELECT * FROM event_observations WHERE event_obs_id=?",
                      (r["event_obs_id"],)).fetchone()
    assert ev["redaction_status"] == "redacted"


@pytest.mark.asyncio
async def test_reconcile_undecryptable_never_verifies(conn):
    transport.record_observation(
        conn, connector="matrix", channel="sync", room_id="!r:ex.org", event_id="$enc",
        event_type="m.room.encrypted", sender="@alice:ex.org",
        account="@bot:ex.org", sync_batch_id="b1")

    async def fetch(room_id, event_id):
        return {"type": "m.room.encrypted", "sender": "@alice:ex.org",
                "content": {"ciphertext": "xx"}, "redacted": False}

    checks = await transport.reconcile(conn, fetch_event=fetch)
    assert checks[0]["result"] == transport.UNVERIFIED


@pytest.mark.asyncio
async def test_reconcile_cut_offs_synthetic_redelivery_without_fetch(conn):
    transport.record_observation(
        conn, connector="matrix", channel="sync", room_id="!r", event_id="$phantom",
        event_type="m.room.message", sender="@alice:ex.org",
        account="@bot:ex.org", sync_batch_id="b1")
    second = transport.record_observation(
        conn, connector="matrix", channel="sync", room_id="!r", event_id="$phantom",
        event_type="m.room.message", sender="@alice:ex.org",
        account="@bot:ex.org", sync_batch_id="b2")  # later batch -> synthetic_redelivery

    calls = []

    async def fetch(room_id, event_id):
        calls.append((room_id, event_id))
        return {"type": "m.room.message", "sender": "@alice:ex.org",
                "content": {"body": "x"}, "redacted": False}

    checks = await transport.reconcile(conn, fetch_event=fetch)
    synthetic = [c for c in checks if c["observation_id"] == second["observation_id"]]
    assert len(synthetic) == 1
    assert synthetic[0]["result"] == transport.SYNTHETIC
    assert len(calls) == 1  # ONLY the original observation was chased; the synthetic one was not
    obs = conn.execute("SELECT status FROM transport_observations WHERE observation_id=?",
                       (second["observation_id"],)).fetchone()
    assert obs["status"] == transport.SYNTHETIC


@pytest.mark.asyncio
async def test_reconcile_retries_then_exhausts_fetch_failures(conn):
    transport.record_observation(
        conn, connector="matrix", channel="sync", room_id="!r:ex.org", event_id="$flaky",
        event_type="m.room.message", sender="@alice:ex.org",
        account="@bot:ex.org", sync_batch_id="b1")

    async def failing(room_id, event_id):
        return {"error_code": "M_FORBIDDEN"}

    # FETCH_RETRY_LIMIT failed passes are recorded individually...
    for _ in range(transport.FETCH_RETRY_LIMIT):
        checks = await transport.reconcile(conn, fetch_event=failing)
        assert [c["result"] for c in checks] == [transport.CHECK_FETCH_FAILED]
    # ...the next pass stops fetching and leaves the observation unverified terminally
    checks = await transport.reconcile(conn, fetch_event=failing)
    assert len(checks) == 1
    assert checks[0]["result"] == transport.UNVERIFIED
    assert checks[0]["error_code"] == "retry_exhausted"
    assert await transport.reconcile(conn, fetch_event=failing) == []


@pytest.mark.asyncio
async def test_reconcile_scopes_to_account(conn):
    mine = transport.record_observation(
        conn, connector="matrix", channel="sync", room_id="!r", event_id="$a",
        event_type="m.room.message", sender="@u", account="@bot:ex.org", sync_batch_id="b1")
    transport.record_observation(
        conn, connector="matrix", channel="sync", room_id="!r", event_id="$b",
        event_type="m.room.message", sender="@u", account="@other:ex.org", sync_batch_id="b1")

    async def fetch(room_id, event_id):
        return {"type": "m.room.message", "sender": "@u",
                "content": {"body": "x"}, "redacted": False}

    checks = await transport.reconcile(conn, fetch_event=fetch, account="@bot:ex.org")
    assert [c["observation_id"] for c in checks] == [mine["observation_id"]]
