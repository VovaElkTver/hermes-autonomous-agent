"""Ports from dsh-superagent-prototype @6337fc8: behavioral contract tests.

Each test pins one donor mechanism against our SQLite core (audit 2026-11-01,
~/projects/hermes-autonomous/reports/deepseek-ai--dsh-superagent-prototype.md):
coalescing effects with generation guard and Later(), outbox idempotence +
ordering barrier + settle-after-withdraw, mandate issue/revalidate on the task
state machine, event dispositions with handback ownership, input read-receipts
and the interrupted-run classification.
"""
import pytest


def _clock():
    t = [1000.0]

    def now():
        return t[0]
    now.tick = lambda d: t.__setitem__(0, t[0] + d)
    return now


@pytest.fixture()
def conn(tmp_path):
    from controller import model
    c = model.connect(tmp_path / "state.db")
    yield c
    c.close()


# ---- effects ----------------------------------------------------------------

def test_effect_coalesces_same_key_and_bumps_generation(conn):
    from controller import effects as fx_mod
    fx = fx_mod.Effects(conn)
    fx.enqueue("reply", "k1", {"v": 1})
    fx.enqueue("reply", "k1", {"v": 2})
    row = conn.execute("SELECT * FROM ctl_effect WHERE kind='reply'").fetchone()
    assert row["value"] == '{"v": 2}' and int(row["generation"]) == 1
    assert fx_mod._unjson_or_text(row["value"]) == {"v": 2}


def test_effect_later_retries_without_failure_count(conn):
    from controller import effects as fx_mod
    clock = _clock()
    fx = fx_mod.Effects(conn, now=clock)
    seen = []
    calls = []

    def handler(payload, key):
        calls.append(key)
        if len(calls) == 1:
            raise fx_mod.Later("busy")
        seen.append(payload)

    fx.handle("k", handler, retry=(5.0, 60.0))
    fx.enqueue("k", "a", {"n": 1})
    assert fx.run_pending() == 1          # executed once, raised Later
    row = conn.execute("SELECT * FROM ctl_effect").fetchone()
    assert int(row["attempts"]) == 0      # Later never counts a failure
    assert not fx.status("k")["failing"]
    clock.tick(6.0)                       # past the initial retry delay
    assert fx.run_pending() == 1
    assert seen == [{"n": 1}]
    # and success removes the row
    assert conn.execute("SELECT COUNT(*) c FROM ctl_effect").fetchone()["c"] == 0


def test_effect_failure_backoff_grows_and_surfaces_after_three(conn):
    from controller import effects as fx_mod
    clock = _clock()
    fx = fx_mod.Effects(conn, now=clock)

    def boom(payload, key):
        raise RuntimeError("smtp down")

    fx.handle("k", boom, retry=(1.0, 4.0))
    fx.enqueue("k", "a")
    dues = []
    for i in range(3):
        fx.run_pending()
        row = conn.execute("SELECT * FROM ctl_effect").fetchone()
        dues.append(row["due"])
        clock.tick(5.0)
    assert dues[1] - dues[0] >= 1.9       # exponential growth beyond the initial delay
    st = fx.status("k")
    assert st["failing"] and "smtp down" in st["error"]


def test_effect_replace_while_running_keeps_newer_payload(conn):
    """Generation guard (donor effects.ts upsert): a payload re-enqueued while an
    attempt is mid-flight must survive that attempt's finish-up."""
    from controller import effects as fx_mod
    fx = fx_mod.Effects(conn)
    got = []

    def handler(payload, key):
        if payload == {"v": 1}:
            fx.enqueue("k", key, {"v": 2})   # newer payload arrives mid-run
        got.append(payload)

    fx.handle("k", handler)
    fx.enqueue("k", "a", {"v": 1})
    fx.run_pending()                      # runs v1; v2 was queued under its feet
    fx.run_pending()                      # must run v2, not vanish
    assert got == [{"v": 1}, {"v": 2}]


def test_ordered_kind_runs_one_row_per_pass(conn):
    from controller import effects as fx_mod
    fx = fx_mod.Effects(conn)
    done = []
    fx.handle("seq", lambda p, k: done.append(k), limit=lambda: 10, ordered=True)
    for k in ("a", "b"):
        fx.enqueue("seq", k)
    assert fx.run_pending() == 1
    assert done == ["a"]
    fx.run_pending()
    assert done == ["a", "b"]


# ---- outbox -------------------------------------------------------------------

def test_outbox_send_is_idempotent_per_message_id(conn):
    from controller import effects as fx_mod
    from controller.outbox import Outbox
    fx = fx_mod.Effects(conn)
    sent = []
    box = Outbox(conn, lambda r: sent.append(r["msg_id"]) or "sent", effects=fx)
    assert box.send("m1", "hello", conv_key="chat/1") is True
    assert box.send("m1", "hello again", conv_key="chat/1") is False  # absorbed
    rows = conn.execute("SELECT * FROM ctl_outbox").fetchall()
    assert len(rows) == 1 and rows[0]["text"] == "hello"
    fx.drain()
    assert sent == ["m1"]
    assert box.status("m1") == "sent"


def test_outbox_orders_later_messages_behind_a_failing_earlier_one(conn):
    from controller import effects as fx_mod
    from controller.outbox import FAILURES_BEFORE_ERROR, Outbox
    clock = _clock()
    fx = fx_mod.Effects(conn, now=clock)
    order = []

    def deliver(r):
        order.append(r["msg_id"])
        if r["msg_id"] == "m1":
            raise RuntimeError("still down")
        return "sent"

    box = Outbox(conn, deliver, effects=fx, now=clock)
    box.send("m1", "first", conv_key="c")
    box.send("m2", "second", conv_key="c")

    # While m1 has fewer than the threshold failures behind it, m2 holds.
    for i in range(FAILURES_BEFORE_ERROR - 1):
        fx.drain()
        clock.tick(300.1)
        assert box.status("m2") != "sent"
        assert "m2" not in order

    fx.drain()                            # m1 crosses the threshold...
    clock.tick(300.1)
    assert conn.execute(
        "SELECT attempts FROM ctl_effect WHERE key='m1'").fetchone()["attempts"] \
        >= FAILURES_BEFORE_ERROR
    fx.drain()                            # ...and m2 goes even though m1 still fails
    clock.tick(300.1)
    fx.drain()
    assert box.status("m2") == "sent" or "m2" in order


def test_outbox_gone_drops_permanently_and_never_queues_again(conn):
    from controller import effects as fx_mod
    from controller.outbox import Outbox
    fx = fx_mod.Effects(conn)
    box = Outbox(conn, lambda r: "gone", effects=fx)
    box.send("m1", "hi", conv_key="c")
    fx.drain()
    assert box.status("m1") == "dropped"
    # re-recording the same id after a permanent drop stays a no-op
    assert box.send("m1", "hi", conv_key="c") is False
    assert fx.pending("outbox_reply", "m1") is False


def test_outbox_settle_after_withdraw_cannot_requeue(conn):
    from controller import effects as fx_mod
    from controller.outbox import Outbox
    fx = fx_mod.Effects(conn)
    box = Outbox(conn, lambda r: "sent", effects=fx)
    box.send("m1", "hi", conv_key="c")
    receipt = box.get("m1")
    # withdrawn while being sent: settle's UPDATE misses a pending row ...
    assert box.withdraw("m1") is True
    box._settle(receipt, sent=True)
    # ... yet the id is recorded settled forever
    assert box.status("m1") == "sent"
    assert box.send("m1", "hi", conv_key="c") is False


# ---- mandate (wired into transitions) -----------------------------------------

def _enable_autonomy(conn, scope="/work"):
    from controller import model
    return model.set_permission(conn, scope, enabled=True,
                                granted_via="user-command")


def _agree(conn, task_id):
    conn.execute("UPDATE ctl_contract_revision SET agreed_at=1 WHERE task_id=?",
                 (task_id,))
    conn.commit()


def _make_ready_task(conn, workdir="/work"):
    from controller import model, transitions
    tid = model.create_task(conn, owner="tester", workdir=workdir, goal="g")
    _agree(conn, tid)
    transitions.transition(conn, tid, "ready", basis="test plan")
    return tid


def test_mandate_issued_on_running_and_withdrawn_on_cancelled(conn):
    from controller import mandate, transitions
    perm = _enable_autonomy(conn)
    tid = _make_ready_task(conn)
    transitions.transition(conn, tid, "running", basis="test",
                           permission={"enabled": 1,
                                       "execution_epoch": perm["execution_epoch"]})
    m = mandate.get(conn, tid)
    assert m["policy_version"] == mandate.compute_policy_version(conn)
    assert m["actor"] == "tester" and m["scope"].endswith("/work")
    transitions.transition(conn, tid, "cancelled", basis="test stop")
    assert mandate.get(conn, tid)["withdrawn_at"] is not None
    assert not mandate.revalidate(conn, tid)


def test_resume_refuses_when_policy_changed_after_acceptance(conn):
    from controller import mandate, transitions
    perm = _enable_autonomy(conn)
    tid = _make_ready_task(conn)
    transitions.transition(conn, tid, "running", basis="test",
                           permission={"enabled": 1,
                                       "execution_epoch": perm["execution_epoch"]})
    transitions.transition(conn, tid, "blocked", basis="stalled")
    # policy change: any authorization-bearing row alters the version
    conn.execute("INSERT INTO ctl_autonomy_permission"
                 " (scope,enabled,execution_epoch,granted_via,updated_at)"
                 " VALUES ('other',1,1,'user-command',1)")
    conn.commit()
    with pytest.raises(ValueError, match="mandate"):
        transitions.transition(conn, tid, "ready", basis="resume?")
    # restoring the exact previous state restores the version → resume allowed
    conn.execute("DELETE FROM ctl_autonomy_permission WHERE scope='other'")
    conn.commit()
    transitions.transition(conn, tid, "ready", basis="resume!")
    assert not mandate.stale_reason(conn, tid)


def test_legacy_task_without_mandate_resumes_normally(conn):
    from controller import mandate
    assert mandate.revalidate(conn, "no-such-task")   # absent mandate allows
    assert mandate.stale_reason(conn, "no-such-task") is None


# ---- dispositions --------------------------------------------------------------

def test_disposition_roundtrip_and_followup_ownership(conn):
    from controller import disposition as disp
    disp.record(conn, "e1", fanout=["t1", "t2", "t3"], context=["t3"])
    disp.handback(conn, "e1", "t1", "not my ticket")
    assert disp.handback(conn, "e1", "t1", "again") is False   # once per task
    owners = disp.followup_owners(conn, "e1", human=True,
                                  live_task_ids=["t1", "t2", "t3"])
    assert owners == ["t2"]
    # bots and CI own nothing; requeued events go back to assessment
    assert disp.followup_owners(conn, "e1", human=False,
                                live_task_ids=["t2"]) == []
    disp.record(conn, "e1", fanout=["t2"], requeued=True)
    assert disp.followup_owners(conn, "e1", human=True,
                                live_task_ids=["t2"]) == []


# ---- receipts --------------------------------------------------------------------

def test_receipts_requeue_only_what_no_step_took(conn):
    from controller import receipts as rec
    rec.delivered(conn, "run-1", ["e1", "e2"])
    rec.read(conn, "run-1", ["e1"])
    moved = rec.requeue_untaken(conn, "run-1", "run-2")
    assert moved == ["e2"]                 # e1 was consumed and stays on run-1
    assert rec.unread(conn, "run-1") == []
    assert rec.unread(conn, "run-2") == ["e2"]
    # transfer is idempotent: nothing left to move
    assert rec.requeue_untaken(conn, "run-1", "run-2") == []


def test_interrupted_classification_is_donor_predicate():
    from controller.receipts import classify_interrupted
    assert classify_interrupted(last_report_at=None, last_delivered_at=None)
    assert classify_interrupted(last_report_at=10, last_delivered_at=None)
    assert not classify_interrupted(last_report_at=10, last_delivered_at=9)   # settled
    assert classify_interrupted(last_report_at=5, last_delivered_at=7)        # busy since
