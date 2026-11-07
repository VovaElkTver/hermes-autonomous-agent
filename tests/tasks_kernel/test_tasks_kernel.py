"""tasks_kernel v0: durable cyclic task queue driven by owner-message events."""
from __future__ import annotations

import sqlite3

import pytest

import tasks_kernel as tk

CHAT = "!room:host"


@pytest.fixture()
def db(tmp_path):
    return str(tmp_path / "state.db")


@pytest.fixture()
def sent(monkeypatch):
    """Capture deliveries instead of spawning hermes send."""
    lines: list[str] = []

    def _fake_deliver(task: dict) -> bool:
        lines.append(f"{task['seq']}:{task['title']}")
        return True

    monkeypatch.setattr(tk, "deliver_fn", _fake_deliver)
    return lines


def test_create_and_start_delivers_head(db, sent):
    ids = tk.create_batch(CHAT, [{"title": "a", "body": "A"}, {"title": "b"}],
                          deliver_target="matrix:!room:host", db_path=db)
    assert len(ids) == 2
    st = tk.status(CHAT, db_path=db)
    assert st["remaining"] == 2
    started = tk.start(CHAT, db_path=db)
    assert started["title"] == "a"
    st = tk.status(CHAT, db_path=db)
    states = {t["title"]: t["state"] for t in st["tasks"]}
    assert states["a"] == tk.WAIT_HUMAN   # running→delivered→parked
    assert states["b"] == tk.READY
    assert sent == ["1:a"]


def test_owner_message_advances_one_step(db, sent):
    tk.create_batch(CHAT, [{"title": "a"}, {"title": "b"}, {"title": "c"}], db_path=db)
    tk.start(CHAT, db_path=db)
    t2 = tk.on_owner_message(CHAT, db_path=db)
    assert t2["title"] == "b"
    t3 = tk.on_owner_message(CHAT, db_path=db)
    assert t3["title"] == "c"
    st = tk.status(CHAT, db_path=db)
    states = {t["title"]: t["state"] for t in st["tasks"]}
    assert states["a"] == tk.DONE
    assert states["b"] == tk.DONE
    assert states["c"] == tk.WAIT_HUMAN
    assert sent == ["1:a", "2:b", "3:c"]
    # queue exhausted: further owner messages do not re-deliver the last item
    tk.on_owner_message(CHAT, db_path=db)
    assert len(sent) == 3


def test_cyclic_wait_event_resume(db, sent):
    tk.create_batch(CHAT, [{"title": "only"}], db_path=db)
    task = tk.start(CHAT, db_path=db)
    tid = task["task_id"]
    conn = sqlite3.connect(db)
    conn.execute("UPDATE tk_task SET wait_event=? WHERE task_id=?", ("go", tid))
    conn.commit()
    conn.close()

    resumed = tk.emit("go", db_path=db)
    assert resumed == 1
    st = tk.status(CHAT, db_path=db)
    assert st["tasks"][0]["state"] == tk.WAIT_HUMAN      # ran again, parked again
    assert len(sent) == 2                                # initial + resumed delivery


def test_cancel_and_bridge_probe(db, sent):
    tk.create_batch(CHAT, [{"title": "x"}, {"title": "y"}], db_path=db)
    tk.start(CHAT, db_path=db)
    assert tk.waiting_head_for_bridge(CHAT, db_path=db) is True
    head = tk.status(CHAT, db_path=db)["tasks"][0]
    assert tk.cancel(head["task_id"], db_path=db) is True
    # after cancelling head the queue still has open work (next is ready)
    assert tk.waiting_head_for_bridge(CHAT, db_path=db) is True


def test_persistence_across_restart(db, sent):
    tk.create_batch(CHAT, [{"title": "a"}, {"title": "b"}], db_path=db)
    tk.start(CHAT, db_path=db)
    # simulate gateway restart: fresh connections against the same file
    t2 = tk.on_owner_message(CHAT, db_path=db)
    assert t2["title"] == "b"
    assert len(sent) == 2


def test_bridge_disabled_is_silent_noop(monkeypatch, db, sent):
    monkeypatch.setenv("HERMES_TASK_BRIDGE", "0")
    import gateway.task_bridge as bridge
    monkeypatch.setattr(bridge, "_flag_cache", None)
    assert bridge.on_owner_message(CHAT) is None


def test_bridge_failure_is_fail_open(monkeypatch, db, sent):
    """Kernel raises ValueError on failed delivery; bridge logs & never raises."""
    tk.create_batch(CHAT, [{"title": "a"}], db_path=db)
    tk.start(CHAT, db_path=db)

    def _boom(task):
        raise ValueError("boom")

    import gateway.task_bridge as bridge
    monkeypatch.setattr(tk, "deliver_fn", _boom)
    # owner message: head completes (done), no next ready task → clean None
    assert bridge.on_owner_message(CHAT) is None

    # direct resume path: emit() must swallow the ValueError and leave task RUNNING
    conn = sqlite3.connect(db)
    conn.execute("UPDATE tk_task SET state=?, wait_event=? WHERE title='a'",
                 (tk.WAIT_HUMAN, "evt"))
    conn.commit()
    conn.close()
    assert tk.emit("evt", db_path=db) == 0          # error swallowed, count 0
    st = tk.status(CHAT, db_path=db)
    assert st["tasks"][0]["state"] == tk.RUNNING    # not lost, stays for retry


def test_tool_handler_actions(tmp_path, monkeypatch, sent, db):
    monkeypatch.setenv("HERMES_STATE_DB", db)
    from tools.task_tools import task_queue

    r = task_queue({"action": "create_batch", "chat_id": CHAT,
                    "items": [{"title": "one"}, {"title": "two"}]})
    assert r["ok"] and r["started"]["title"] == "one"
    r = task_queue({"action": "skip", "chat_id": CHAT})
    assert r["ok"] and r["delivered"] == "two"      # one postponed to the tail
    r = task_queue({"action": "status", "chat_id": CHAT})
    assert r["ok"] and r["remaining"] == 2          # skipped ≠ done: both still open
