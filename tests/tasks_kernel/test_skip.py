"""v0.1 /skip semantics: postpone the head to the tail, never done (07.11 spec)."""
from __future__ import annotations

import pytest

import tasks_kernel as tk

CHAT = "!room:host"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_STATE_DB", str(tmp_path / "state.db"))
    return str(tmp_path / "state.db")


@pytest.fixture()
def sent(monkeypatch):
    lines: list[str] = []

    def _fake(task: dict) -> bool:
        lines.append(f"{task['seq']}:{task['title']}")
        return True

    monkeypatch.setattr(tk, "deliver_fn", _fake)
    return lines


def _seed(db, titles):
    tk.create_batch(CHAT, [{"title": t} for t in titles], db_path=db)
    tk.start(CHAT, db_path=db)


def _states(db):
    return {t["title"]: (t["state"], t["seq"])
            for t in tk.status(CHAT, db_path=db)["tasks"]}


def test_skip_requeues_head_at_tail_and_starts_next(db, sent):
    _seed(db, ["a", "b", "c"])          # a=wait_human, b,c=ready
    out = tk.skip(CHAT, db_path=db)
    assert out is not None and out["skipped"]["title"] == "a"
    st = _states(db)
    assert st["b"][0] == tk.WAIT_HUMAN  # next started immediately
    assert st["a"][0] == tk.READY       # postponed, not done
    assert st["a"][1] > st["c"][1]      # requeued at the tail
    assert len(sent) == 2               # a then b


def test_skip_single_task_comes_around(db, sent):
    _seed(db, ["only"])
    out = tk.skip(CHAT, db_path=db)
    assert out is not None and out["delivered"]["title"] == "only"   # redelivered
    st = _states(db)
    assert st["only"][0] == tk.WAIT_HUMAN


def test_skip_never_marks_done(db, sent):
    _seed(db, ["x", "y"])
    before = tk.status(CHAT, db_path=db)["remaining"]
    out = tk.skip(CHAT, db_path=db)
    after = tk.status(CHAT, db_path=db)["remaining"]
    assert before == after == 2
    assert all(t["state"] != tk.DONE for t in tk.status(CHAT, db_path=db)["tasks"])


def test_skip_without_head_is_noop(db, sent):
    assert tk.skip(CHAT, db_path=db) is None


def test_tool_action_skip_real_semantics(db, sent, monkeypatch):
    import tools.task_tools as tt
    _seed(db, ["p", "q"])
    monkeypatch.setenv("HERMES_STATE_DB", db)
    res = tt.task_queue({"action": "skip", "chat_id": CHAT})
    assert res["ok"] and res["skipped_id"]
    assert res["delivered"] == "q"
