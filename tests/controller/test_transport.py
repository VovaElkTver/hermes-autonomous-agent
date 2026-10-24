"""Transport-observation layer (agency-model §16.9, task-transobs01).

Schema contract (three tables in state.db next to ctl_*), observation
recording with event-level upsert, and reconciliation: six characteristics
instead of a single `verified` flag; a failed fetch is a check WITH an error
code, never a verdict; a re-check verifies the interface, not truth.
"""
import time

import pytest

from controller import model, transport as T


@pytest.fixture()
def conn(tmp_path):
    c = model.connect(tmp_path / "t.db")
    yield c
    c.close()


def _columns(c, table):
    return [r["name"] for r in c.execute(f"PRAGMA table_info({table})")]


def _rec(conn, **kw) -> str:
    """record_observation, asserting a row was written (external event given)."""
    oid = T.record_observation(conn, **kw)
    assert oid is not None
    return oid


# ---- schema -------------------------------------------------------------------

def test_schema_creates_the_three_tables(conn):
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"transport_observations", "event_observations", "reconciliation_checks"} <= tables


def test_no_single_verified_flag_and_six_check_characteristics(conn):
    """§16.9: no single `verified` flag anywhere in the layer; a check carries
    the six characteristics from which a verdict is always derived."""
    for table in ("transport_observations", "event_observations", "reconciliation_checks"):
        assert "verified" not in _columns(conn, table)
    assert set(T.CHECK_FIELDS) <= set(_columns(conn, "reconciliation_checks"))
    assert len(T.CHECK_FIELDS) == 6


def test_unknown_channel_rejected(conn):
    with pytest.raises(ValueError):
        T.record_observation(conn, connector="matrix", account="@a:ex",
                             channel="bogus", room_id="!r:ex")


def test_payload_ref_is_stable_identity_not_payload(conn):
    p = {"body": "secret content", "msgtype": "m.text"}
    ref = T.payload_ref(p)
    assert ref is not None
    assert ref == T.payload_ref(p)
    assert "secret content" not in ref
    assert T.payload_ref(None) is None


# ---- observation recording ------------------------------------------------------

def test_record_observation_upserts_event_level_row(conn):
    o1 = _rec(conn, connector="matrix", account="@bot:ex", channel="sync",
              room_id="!r:ex", external_event_id="$e1", raw_payload_ref="sha256:x")
    o2 = T.record_observation(conn, connector="matrix", account="@bot:ex",
                              channel="event_fetch", room_id="!r:ex",
                              external_event_id="$e1")
    assert o1 == o2  # same external event -> one event-level row
    assert conn.execute("SELECT COUNT(*) n FROM transport_observations").fetchone()["n"] == 2
    assert conn.execute("SELECT COUNT(*) n FROM event_observations").fetchone()["n"] == 1
    ev = T.event_obs_for(conn, "matrix", "$e1")
    assert ev is not None
    assert ev["status"] == T.UNVERIFIED
    assert ev["journal_ref"] is None  # not a room fact, no journal link yet


def test_observation_without_external_event_id_has_no_event_row(conn):
    assert T.record_observation(conn, connector="matrix", account="@a:ex",
                                channel="sync", room_id="!r:ex") is None
    assert conn.execute("SELECT COUNT(*) n FROM event_observations").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) n FROM transport_observations").fetchone()["n"] == 1


def test_message_time_used_when_provided(conn):
    T.record_observation(conn, connector="matrix", account="@a:ex", channel="sync",
                         room_id="!r:ex", external_event_id="$ts", observed_at=1000.0)
    row = conn.execute("SELECT observed_at FROM transport_observations").fetchone()
    assert row is not None
    assert row["observed_at"] == 1000.0


# ---- checks: the six characteristics --------------------------------------------

def test_failed_fetch_is_a_check_with_error_code_and_no_verdict(conn):
    obs = _rec(conn, connector="matrix", account="@a:ex", channel="sync",
               room_id="!r:ex", external_event_id="$e2")
    T.record_check(conn, obs, method="event_refetch", against_source="https://mx",
                   result="fetch_failed", error_code="M_FORBIDDEN")
    ev = T.event_obs_for(conn, "matrix", "$e2")
    assert ev is not None
    assert ev["status"] == T.UNVERIFIED  # a failed check proves nothing
    row = conn.execute("SELECT * FROM reconciliation_checks WHERE obs_id=?", (obs,)).fetchone()
    assert row is not None
    assert row["error_code"] == "M_FORBIDDEN"
    assert T.is_verified(conn, obs) is False


def test_fetch_failed_requires_an_error_code(conn):
    obs = _rec(conn, connector="matrix", account="@a:ex", channel="sync",
               room_id="!r:ex", external_event_id="$e3")
    with pytest.raises(ValueError):
        T.record_check(conn, obs, method="event_refetch", against_source="https://mx",
                       result="fetch_failed")


def test_matched_check_confirms_via_interface(conn):
    obs = _rec(conn, connector="matrix", account="@a:ex", channel="sync",
               room_id="!r:ex", external_event_id="$e4")
    T.record_check(conn, obs, method="event_refetch", against_source="https://mx",
                   result="matched", evidence_ref="sha256:z")
    ev = T.event_obs_for(conn, "matrix", "$e4")
    assert ev is not None
    assert ev["status"] == T.CONFIRMED
    assert T.is_verified(conn, obs) is True


def test_match_without_recorded_source_proves_nothing(conn):
    """against_source is a characteristic, not decoration: a check that
    recorded no interface is not a verification."""
    obs = _rec(conn, connector="matrix", account="@a:ex", channel="sync",
               room_id="!r:ex", external_event_id="$e5")
    T.record_check(conn, obs, method="event_refetch", against_source="", result="matched")
    assert T.is_verified(conn, obs) is False


def test_newest_check_wins(conn):
    obs = _rec(conn, connector="matrix", account="@a:ex", channel="sync",
               room_id="!r:ex", external_event_id="$e6")
    t0 = time.time()
    T.record_check(conn, obs, method="event_refetch", against_source="https://mx",
                   result="matched", checked_at=t0)
    T.record_check(conn, obs, method="event_refetch", against_source="https://mx",
                   result="diverged", evidence_ref="sha256:y", checked_at=t0 + 1)
    ev = T.event_obs_for(conn, "matrix", "$e6")
    assert ev is not None
    assert ev["status"] == T.DIVERGENT
    assert T.is_verified(conn, obs) is False


def test_link_journal(conn):
    _rec(conn, connector="matrix", account="@a:ex", channel="sync",
         room_id="!r:ex", external_event_id="$j")
    T.link_journal(conn, "matrix", "$j", "msg-42")
    ev = T.event_obs_for(conn, "matrix", "$j")
    assert ev is not None
    assert ev["journal_ref"] == "msg-42"


# ---- reconciliation ----------------------------------------------------------------

def test_reconcile_classifies_all_four_outcomes(conn):
    obs_a = _rec(conn, connector="matrix", account="@a:ex", channel="sync",
                 room_id="!r", external_event_id="$a", raw_payload_ref="sha256:same")
    _rec(conn, connector="matrix", account="@a:ex", channel="sync",
         room_id="!r", external_event_id="$b", raw_payload_ref="sha256:same")
    _rec(conn, connector="matrix", account="@a:ex", channel="sync",
         room_id="!r", external_event_id="$c", raw_payload_ref="sha256:same")
    obs_d = _rec(conn, connector="matrix", account="@a:ex", channel="sync",
                 room_id="!r", external_event_id="$d", raw_payload_ref="sha256:same")

    def fetch(room_id, event_id):
        if event_id == "$a":
            return ("sha256:same", None)   # matched
        if event_id == "$b":
            return ("sha256:other", None)  # diverged
        if event_id == "$c":
            return (None, None)            # not_found
        return (None, "http_503")          # fetch_failed

    s = T.reconcile(conn, fetch, source_label="https://mx:8448")
    assert s == {"checked": 4, "matched": 1, "diverged": 1, "not_found": 1, "fetch_failed": 1}
    assert T.event_obs_for(conn, "matrix", "$a")["status"] == T.CONFIRMED
    assert T.event_obs_for(conn, "matrix", "$b")["status"] == T.DIVERGENT
    assert T.event_obs_for(conn, "matrix", "$c")["status"] == T.NOT_FOUND
    assert T.event_obs_for(conn, "matrix", "$d")["status"] == T.UNVERIFIED
    # the failed fetch carries its machine code
    row = conn.execute("SELECT error_code FROM reconciliation_checks WHERE obs_id=?",
                       (obs_d,)).fetchone()
    assert row is not None
    assert row["error_code"] == "http_503"


def test_reconcile_captures_fetch_exceptions_as_error_code(conn):
    obs = _rec(conn, connector="matrix", account="@a:ex", channel="sync",
               room_id="!r", external_event_id="$x", raw_payload_ref="sha256:s")

    class _FakeReqError(Exception):
        errcode = "M_NOT_FOUND"
        http_status = 404

    def fetch(room_id, event_id):
        raise _FakeReqError()

    s = T.reconcile(conn, fetch, source_label="https://mx")
    assert s["fetch_failed"] == 1 and s["checked"] == 1
    row = conn.execute("SELECT error_code FROM reconciliation_checks WHERE obs_id=?", (obs,)).fetchone()
    assert row is not None
    assert row["error_code"] == "M_NOT_FOUND"
    ev = T.event_obs_for(conn, "matrix", "$x")
    assert ev is not None
    assert ev["status"] == T.UNVERIFIED


def test_error_code_of_shapes(conn):
    class _Err(Exception):
        errcode = "M_FORBIDDEN"
        http_status = 403

    class _Plain(Exception):
        http_status = 503

    assert T.error_code_of(_Err()) == "M_FORBIDDEN"
    assert T.error_code_of(_Plain()) == "http_503"
    assert T.error_code_of(TimeoutError()) == "timeout"
    assert T.error_code_of(RuntimeError()) == "unknown"


def test_pending_throttles_recent_checks(conn):
    obs = _rec(conn, connector="matrix", account="@a:ex", channel="sync",
               room_id="!r", external_event_id="$t")
    assert [r["obs_id"] for r in T.pending(conn)] == [obs]
    T.record_check(conn, obs, method="event_refetch", against_source="https://mx",
                   result="fetch_failed", error_code="timeout")
    assert T.pending(conn) == []  # last check is too fresh to retry
    now = time.time()
    due = T.pending(conn, now=now + 400, min_gap_s=300.0)
    assert [r["obs_id"] for r in due] == [obs]


def test_pending_excludes_verdicts_and_kept_fetch_failed_only(conn):
    """matched / diverged / not_found reached a verdict and leave the pending
    set; a fetch_failed event stays unverified and comes back after the gap."""
    o_m = _rec(conn, connector="matrix", account="@a:ex", channel="sync",
               room_id="!r", external_event_id="$m")
    o_f = _rec(conn, connector="matrix", account="@a:ex", channel="sync",
               room_id="!r", external_event_id="$f")
    T.record_check(conn, o_m, method="event_refetch", against_source="https://mx", result="matched")
    T.record_check(conn, o_f, method="event_refetch", against_source="https://mx",
                   result="fetch_failed", error_code="timeout", checked_at=time.time() - 400)
    due = {r["obs_id"] for r in T.pending(conn)}
    assert due == {o_f}
