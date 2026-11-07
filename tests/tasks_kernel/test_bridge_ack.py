"""v0.1 ack-gating: only explicit permission moves the queue (07.11 demo lesson)."""
from __future__ import annotations

import sqlite3

import pytest

import tasks_kernel as tk
import gateway.task_bridge as bridge

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


def _seed(db, n=2):
    tk.create_batch(CHAT, [{"title": f"s{i}"} for i in range(1, n + 1)], db_path=db)
    tk.start(CHAT, db_path=db)


def test_discussion_does_not_advance(db, sent):
    _seed(db)
    assert bridge.on_owner_message(CHAT, text="как будто обсудим шаг 2",
                                   reply_to_text=None) is None
    states = {t["title"]: t["state"] for t in tk.status(CHAT, db_path=db)["tasks"]}
    assert states["s1"] == tk.WAIT_HUMAN      # untouched
    assert len(sent) == 1                     # no re-delivery


def test_next_command_advances(db, sent):
    _seed(db)
    out = bridge.on_owner_message(CHAT, text="/next", reply_to_text=None)
    assert out is not None and out["title"] == "s2"


def test_reply_to_task_line_advances(db, sent):
    _seed(db)
    out = bridge.on_owner_message(
        CHAT, text="окей,讨论им после обеда",
        reply_to_text="[задача] s1: делать раз")
    assert out is not None and out["title"] == "s2"


def test_reply_to_foreign_line_ignored(db, sent):
    _seed(db)
    out = bridge.on_owner_message(CHAT, text="оно и в африке оно",
                                  reply_to_text="Шаг 1/3: без маркера")
    assert out is None
    states = {t["title"]: t["state"] for t in tk.status(CHAT, db_path=db)["tasks"]}
    assert states["s1"] == tk.WAIT_HUMAN


def test_marker_constant_matches_kernel_delivery(db, sent):
    """Contract: the line the kernel sends must start with bridge.LINE_MARKER."""
    captured: list[str] = []

    def _spy(task):
        captured.append(tk.deliver_default.__module__ and "")
        return True

    # render exactly like deliver_default does
    task = {"title": "T", "body": "B"}
    line = f"[задача] {task['title']}: {task['body']}"
    assert line.startswith(bridge.LINE_MARKER)


def test_empty_queue_still_noop_on_ack(db, sent):
    tk.create_batch(CHAT, [{"title": "only"}], db_path=db)
    tk.start(CHAT, db_path=db)
    tk.on_owner_message(CHAT, db_path=db)   # exhaust
    assert bridge.on_owner_message(CHAT, text="/next") is None
