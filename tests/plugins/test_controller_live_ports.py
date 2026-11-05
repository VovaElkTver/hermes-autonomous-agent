"""Live wiring of the donor ports (controller.live_ports + Ticker.drain).

Pins the pump-side contract: producers are idempotent per failed RUN, delivery
refusals count toward FAILURES_BEFORE_ERROR so the ordering barrier lifts, the
pump tick drains under its lock and never dies on transport trouble.
"""
import pytest


@pytest.fixture(autouse=True)
def no_real_send(monkeypatch):
    """Guard: a test must never shell out to `hermes send`."""
    from controller import alerts

    def _boom(*a, **k):
        raise AssertionError("tests must not reach the real delivery path")
    monkeypatch.setattr(alerts, "deliver", _boom)
    yield


@pytest.fixture()
def conn(tmp_path):
    from controller import model
    c = model.connect(tmp_path / "state.db")
    yield c
    c.close()


@pytest.fixture()
def target(monkeypatch):
    monkeypatch.setattr("controller.live_ports.alert_target_from_cfg",
                        lambda: "Home")
    return "Home"


def test_notify_task_failed_idempotent_per_run(conn, target):
    from controller import live_ports
    ok1 = live_ports.notify_task_failed(
        conn, task_id="task-a", run_id="run-1", goal="починить шлюз",
        reason="worker died")
    assert ok1 is True
    ok2 = live_ports.notify_task_failed(
        conn, task_id="task-a", run_id="run-1", goal="починить шлюз", reason="again")
    assert ok2 is False  # same run → same msg_id → absorbed
    ok3 = live_ports.notify_task_failed(
        conn, task_id="task-a", run_id="run-2", goal="починить шлюз", reason="retry failed")
    assert ok3 is True  # a new run episode notifies again
    rows = [dict(r) for r in conn.execute("SELECT * FROM ctl_outbox")]
    assert len(rows) == 2 and all(r["status"] == "pending" for r in rows)


def test_no_target_produces_silence(conn, monkeypatch):
    from controller import live_ports
    monkeypatch.setattr("controller.live_ports.alert_target_from_cfg", lambda: "")
    ok = live_ports.notify_task_failed(conn, task_id="t", run_id="r", goal="g")
    assert ok is False
    assert conn.execute("SELECT COUNT(*) FROM ctl_outbox").fetchone()[0] == 0


def test_drain_delivers_and_refusal_counts_attempts(conn, target):
    from controller import live_ports
    sent_texts = []
    state = {"fails": 1}

    def flaky(receipt):
        if state["fails"] > 0:
            state["fails"] -= 1
            raise RuntimeError("transport down")
        sent_texts.append(receipt["text"])
        return "sent"

    ok = live_ports.notify_task_failed(conn, task_id="task-b", run_id="run-x",
                                       goal="g", reason="drop me")
    assert ok
    # producer dispatches immediately once; make BOTH its retry and our next
    # pass runnable now (tests share the wall clock)
    conn.execute("UPDATE ctl_effect SET due=0 WHERE kind='outbox_reply'")
    effects, outbox = live_ports.build(conn, flaky)
    # one pass: attempt books failure (attempts=1, due=now+5)
    done = effects.run_pending("outbox_reply")
    assert done == 1
    status = outbox.status("ctl-task-task-b-run-x-failed")
    assert status == "pending"  # retryable, NOT gone
    err = conn.execute("SELECT attempts, error FROM ctl_effect"
                       " WHERE kind='outbox_reply'").fetchone()
    assert err["attempts"] == 1 and "transport down" in err["error"]
    # force due back and drain: message goes out on the next pass
    conn.execute("UPDATE ctl_effect SET due=0 WHERE kind='outbox_reply'")
    effects.run_pending("outbox_reply")
    assert len(sent_texts) == 1
    assert outbox.status("ctl-task-task-b-run-x-failed") == "sent"


def test_ordering_barrier_lifts_after_failures(conn, target):
    """Donor parity: an earlier failing message unblocks later ones once it has
    FAILURES_BEFORE_ERROR recorded attempts — and stays pending/problem-visible."""
    from controller import live_ports

    def refuse(receipt):
        raise RuntimeError("no route")

    effects, outbox = live_ports.build(conn, refuse)
    m1 = "ctl-task-t-r1-failed"
    m2 = "ctl-task-t-r2-failed"
    outbox.send(m1, "first", conv_key="target:Home", task_id="t")
    outbox.send(m2, "second", conv_key="target:Home", task_id="t")

    def fail_times():
        row = conn.execute("SELECT attempts FROM ctl_effect WHERE kind='outbox_reply'"
                           " AND key=?", (m1,)).fetchone()
        return int(row["attempts"]) if row else 0

    for _ in range(3):  # m1 fails thrice, growing its counter
        conn.execute("UPDATE ctl_effect SET due=0 WHERE key=?", (m1,))
        effects.run_pending("outbox_reply")
    assert fail_times() >= 3
    # barrier lifted: m2 now dispatches (and fails again, counting its own attempts)
    conn.execute("UPDATE ctl_effect SET due=0 WHERE key=?", (m2,))
    effects.run_pending("outbox_reply")
    row2 = conn.execute("SELECT attempts FROM ctl_effect WHERE key=? AND kind='outbox_reply'",
                        (m2,)).fetchone()
    assert int(row2["attempts"]) == 1
    assert outbox.status(m1) == "pending" and outbox.status(m2) == "pending"


def test_ticker_drain_uses_injected_channel(conn, target):
    """run_pump's helper drains through the gateway-captured deliver_fn."""
    from controller import live_ports
    from controller.ticker import Ticker
    captured = []

    def _capture_and_ok(line: str) -> bool:
        captured.append(line)
        return True

    t = Ticker(alert_target="Home", deliver_fn=_capture_and_ok)
    ok = live_ports.notify_task_failed(conn, task_id="task-c", run_id="run-9",
                                       goal="g", reason="hello")
    assert ok
    assert captured == []  # producer path has no channel injected: row waits
    # make the queued notice runnable and hand it to the ticker's channel
    conn.execute("UPDATE ctl_effect SET due=0 WHERE kind='outbox_reply'")
    n = t._drain_live_ports(conn)
    assert n == 1
    assert "задача task-c провалилась" in captured[0]
    row = conn.execute("SELECT status FROM ctl_outbox WHERE msg_id LIKE 'ctl-task-task-c%'"
                       " AND conv_key='target:Home'").fetchone()
    assert row["status"] == "sent"
